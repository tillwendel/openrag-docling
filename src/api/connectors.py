from typing import Any, List, Optional

from fastapi import Depends, Request
from pydantic import BaseModel
from fastapi.responses import JSONResponse, PlainTextResponse
from connectors.sharepoint.utils import is_valid_sharepoint_url
from config.settings import get_index_name
from utils.logging_config import get_logger
from utils.telemetry import TelemetryClient, Category, MessageId
from dependencies import get_connector_service, get_session_manager, get_current_user
from session_manager import User

logger = get_logger(__name__)


async def get_synced_file_ids_for_connector(
    connector_type: str,
    user_id: str,
    session_manager,
    jwt_token: str = None,
) -> tuple:
    """
    Query OpenSearch for unique document_id values where connector_type matches.
    Returns tuple of (file_ids, filenames) - use file_ids if available, else filenames as fallback.
    
    Note: Langflow-ingested files may not have document_id stored. In that case,
    filenames are returned for filename-based filtering during sync.
    """
    try:
        opensearch_client = session_manager.get_user_opensearch_client(user_id, jwt_token)
        
        # Query for both document_id and filename aggregations
        query_body = {
            "size": 0,
            "query": {
                "term": {
                    "connector_type": connector_type
                }
            },
            "aggs": {
                "unique_document_ids": {
                    "terms": {
                        "field": "document_id",
                        "size": 10000
                    }
                },
                "unique_filenames": {
                    "terms": {
                        "field": "filename",
                        "size": 10000
                    }
                }
            }
        }
        
        result = await opensearch_client.search(
            index=get_index_name(),
            body=query_body
        )
        
        # Get document_ids (preferred - these are the actual connector file IDs)
        doc_id_buckets = result.get("aggregations", {}).get("unique_document_ids", {}).get("buckets", [])
        file_ids = [bucket["key"] for bucket in doc_id_buckets if bucket["key"]]
        
        # Get filenames as fallback
        filename_buckets = result.get("aggregations", {}).get("unique_filenames", {}).get("buckets", [])
        filenames = [bucket["key"] for bucket in filename_buckets if bucket["key"]]
        
        logger.debug(
            "Found synced files for connector",
            connector_type=connector_type,
            file_ids_count=len(file_ids),
            filenames_count=len(filenames),
        )
        
        return file_ids, filenames
        
    except Exception as e:
        logger.error(
            "Failed to get synced file IDs",
            connector_type=connector_type,
            error=str(e),
        )
        return [], []



class ConnectorSyncBody(BaseModel):
    max_files: Optional[int] = None
    selected_files: Optional[List[Any]] = None
    # When True, ingest ALL files from the connector (bypasses the existing-files gate).
    # Used by direct-sync providers like IBM COS on initial ingest.
    sync_all: bool = False
    # When set, only ingest files from these buckets (IBM COS specific).
    bucket_filter: Optional[List[str]] = None


async def list_connectors(
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """List available connector types with metadata"""
    try:
        connector_types = (
            connector_service.connection_manager.get_available_connector_types()
        )
        return JSONResponse({"connectors": connector_types})
    except Exception as e:
        logger.info("Error listing connectors", error=str(e))
        return JSONResponse({"connectors": []})


async def connector_sync(
    connector_type: str,
    body: ConnectorSyncBody,
    connector_service=Depends(get_connector_service),
    session_manager=Depends(get_session_manager),
    user: User = Depends(get_current_user),
):
    """Sync files from all active connections of a connector type"""
    max_files = body.max_files
    selected_files_raw = body.selected_files
    selected_files = None
    file_infos = None
    if selected_files_raw:
        if isinstance(selected_files_raw[0], str):
            # Legacy format: just IDs
            selected_files = selected_files_raw
        else:
            # New format: file objects with metadata
            selected_files = [f.get("id") for f in selected_files_raw if f.get("id")]
            file_infos = selected_files_raw

    try:
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_SYNC_START)
        logger.debug(
            "Starting connector sync",
            connector_type=connector_type,
            max_files=max_files,
        )
        jwt_token = user.jwt_token

        # Get all active connections for this connector type and user
        connections = await connector_service.connection_manager.list_connections(
            user_id=user.user_id, connector_type=connector_type
        )

        active_connections = [conn for conn in connections if conn.is_active]
        if not active_connections:
            return JSONResponse(
                {"error": f"No active {connector_type} connections found"},
                status_code=404,
            )

        # Find the first connection that actually works
        working_connection = None
        for connection in active_connections:
            logger.debug(
                "Testing connection authentication",
                connection_id=connection.connection_id,
            )
            try:
                # Get the connector instance and test authentication
                connector = await connector_service.get_connector(connection.connection_id)
                if connector and await connector.authenticate():
                    working_connection = connection
                    logger.debug(
                        "Found working connection",
                        connection_id=connection.connection_id,
                    )
                    break
                else:
                    logger.debug(
                        "Connection authentication failed",
                        connection_id=connection.connection_id,
                    )
            except Exception as e:
                logger.debug(
                    "Connection validation failed",
                    connection_id=connection.connection_id,
                    error=str(e),
                )
                continue

        if not working_connection:
            return JSONResponse(
                {"error": f"No working {connector_type} connections found"},
                status_code=404,
            )

        # Use the working connection
        logger.debug(
            "Starting sync with working connection",
            connection_id=working_connection.connection_id,
        )
        
        if selected_files:
            # Explicit files selected (e.g., from file picker) - sync those specific files
            from .documents import _ensure_index_exists
            await _ensure_index_exists()
            task_id = await connector_service.sync_specific_files(
                working_connection.connection_id,
                user.user_id,
                selected_files,
                jwt_token=jwt_token,
                file_infos=file_infos,
            )
        elif body.sync_all or body.bucket_filter:
            # Full ingest: discover and ingest all files (or files from specific buckets).
            # Used by direct-sync providers (IBM COS) on initial ingest or per-bucket sync.
            logger.info(
                "Full connector ingest requested",
                connector_type=connector_type,
                bucket_filter=body.bucket_filter,
            )
            connector = await connector_service.get_connector(working_connection.connection_id)
            if body.bucket_filter:
                # List only files from the requested buckets, then sync_specific_files
                original_buckets = connector.bucket_names
                connector.bucket_names = body.bucket_filter
                try:
                    all_file_ids = []
                    page_token = None
                    while True:
                        result = await connector.list_files(page_token=page_token)
                        for f in result.get("files", []):
                            all_file_ids.append(f["id"])
                        page_token = result.get("next_page_token")
                        if not page_token:
                            break
                finally:
                    connector.bucket_names = original_buckets

                if not all_file_ids:
                    return JSONResponse(
                        {"status": "no_files", "message": "No files found in the selected buckets."},
                        status_code=200,
                    )
                task_id = await connector_service.sync_specific_files(
                    working_connection.connection_id,
                    user.user_id,
                    all_file_ids,
                    jwt_token=jwt_token,
                )
            else:
                # sync_all: ingest everything the connector can see
                task_id = await connector_service.sync_connector_files(
                    working_connection.connection_id,
                    user.user_id,
                    max_files=max_files,
                    jwt_token=jwt_token,
                )
        else:
            # No files specified - sync only files already in OpenSearch for this connector
            # This ensures deleted files stay deleted
            existing_file_ids, existing_filenames = await get_synced_file_ids_for_connector(
                connector_type=connector_type,
                user_id=user.user_id,
                session_manager=session_manager,
                jwt_token=jwt_token,
            )

            if not existing_file_ids and not existing_filenames:
                return JSONResponse(
                    {
                        "status": "no_files",
                        "message": f"No {connector_type} files to sync. Add files from the connector first.",
                    },
                    status_code=200,
                )

            # If we have document_ids (connector file IDs), use sync_specific_files
            # Otherwise, use filename filtering with sync_connector_files
            if existing_file_ids:
                logger.info(
                    "Syncing specific files by document_id",
                    connector_type=connector_type,
                    file_count=len(existing_file_ids),
                )
                task_id = await connector_service.sync_specific_files(
                    working_connection.connection_id,
                    user.user_id,
                    existing_file_ids,
                    jwt_token=jwt_token,
                )
            else:
                # Fallback: use filename filtering (for Langflow-ingested files without document_id)
                logger.info(
                    "Syncing files by filename filter (document_id not available)",
                    connector_type=connector_type,
                    filename_count=len(existing_filenames),
                )
                task_id = await connector_service.sync_connector_files(
                    working_connection.connection_id,
                    user.user_id,
                    max_files=None,
                    jwt_token=jwt_token,
                    filename_filter=set(existing_filenames),
                )
        task_ids = [task_id]
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_SYNC_COMPLETE)
        return JSONResponse(
            {
                "task_ids": task_ids,
                "status": "sync_started",
                "message": f"Started syncing files from {len(active_connections)} {connector_type} connection(s)",
                "connections_synced": len(active_connections),
            },
            status_code=201,
        )

    except Exception as e:
        logger.error("Connector sync failed", error=str(e))
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_SYNC_FAILED)
        return JSONResponse({"error": f"Sync failed: {str(e)}"}, status_code=500)


async def connector_status(
    connector_type: str,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Get connector status for authenticated user"""

    # Get connections for this connector type and user
    connections = await connector_service.connection_manager.list_connections(
        user_id=user.user_id, connector_type=connector_type
    )

    # Get the connector for each connection and verify authentication
    connection_details = {}
    verified_active_connections = []
    
    for connection in connections:
        try:
            connector = await connector_service._get_connector(connection.connection_id)
            if connector is not None:
                # Actually verify the connection by trying to authenticate
                is_authenticated = await connector.authenticate()
                
                # Get base URL if available (for SharePoint/OneDrive connectors)
                base_url = None
                if hasattr(connector, 'base_url'):
                    base_url = connector.base_url
                    logger.debug(f"connector_status: Got base_url from connector.base_url: {base_url}")
                elif hasattr(connector, 'sharepoint_url'):
                    base_url = connector.sharepoint_url  # Backward compatibility
                    logger.debug(f"connector_status: Got base_url from connector.sharepoint_url: {base_url}")
                else:
                    logger.debug(f"connector_status: Connector has no base_url or sharepoint_url attribute")
                
                connection_details[connection.connection_id] = {
                    "client_id": connector.get_client_id(),
                    "is_authenticated": is_authenticated,
                    "base_url": base_url,
                }
                if is_authenticated and connection.is_active:
                    verified_active_connections.append(connection)
            else:
                connection_details[connection.connection_id] = {
                    "client_id": None,
                    "is_authenticated": False,
                    "base_url": None,
                }
        except Exception as e:
            logger.warning(
                "Could not verify connector authentication",
                connection_id=connection.connection_id,
                error=str(e),
            )
            connection_details[connection.connection_id] = {
                "client_id": None,
                "is_authenticated": False,
                "base_url": None,
            }

    # Only count connections that are both active AND actually authenticated
    has_authenticated_connection = len(verified_active_connections) > 0

    return JSONResponse(
        {
            "connector_type": connector_type,
            "authenticated": has_authenticated_connection,
            "status": "connected" if has_authenticated_connection else "not_connected",
            "connections": [
                {
                    "connection_id": conn.connection_id,
                    "name": conn.name,
                    "client_id": connection_details.get(conn.connection_id, {}).get("client_id"),
                    "is_active": conn.is_active and connection_details.get(conn.connection_id, {}).get("is_authenticated", False),
                    "is_authenticated": connection_details.get(conn.connection_id, {}).get("is_authenticated", False),
                    "base_url": connection_details.get(conn.connection_id, {}).get("base_url"),
                    "created_at": conn.created_at.isoformat(),
                    "last_sync": conn.last_sync.isoformat() if conn.last_sync else None,
                }
                for conn in connections
            ],
        }
    )


async def connector_webhook(
    connector_type: str,
    request: Request,
    connector_service=Depends(get_connector_service),
    session_manager=Depends(get_session_manager),
):
    """Handle webhook notifications from any connector type"""

    # Handle webhook validation (connector-specific)
    temp_config = {"token_file": "temp.json"}
    from connectors.connection_manager import ConnectionConfig

    temp_connection = ConnectionConfig(
        connection_id="temp",
        connector_type=str(connector_type),
        name="temp",
        config=temp_config,
    )
    try:
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_WEBHOOK_RECV)
        temp_connector = connector_service.connection_manager._create_connector(
            temp_connection
        )
        validation_response = temp_connector.handle_webhook_validation(
            request.method, dict(request.headers), dict(request.query_params)
        )
        if validation_response:
            return PlainTextResponse(validation_response)
    except (NotImplementedError, ValueError):
        # Connector type not found or validation not needed
        pass

    try:
        # Get the raw payload and headers
        payload = {}
        headers = dict(request.headers)

        if request.method == "POST":
            content_type = headers.get("content-type", "").lower()
            if "application/json" in content_type:
                payload = await request.json()
            else:
                # Some webhooks send form data or plain text
                body = await request.body()
                payload = {"raw_body": body.decode("utf-8") if body else ""}
        else:
            # GET webhooks use query params
            payload = dict(request.query_params)

        # Add headers to payload for connector processing
        payload["_headers"] = headers
        payload["_method"] = request.method

        logger.info("Webhook notification received", connector_type=connector_type)

        # Extract channel/subscription ID using connector-specific method
        try:
            temp_connector = connector_service.connection_manager._create_connector(
                temp_connection
            )
            channel_id = temp_connector.extract_webhook_channel_id(payload, headers)
        except (NotImplementedError, ValueError):
            channel_id = None

        if not channel_id:
            logger.warning(
                "No channel ID found in webhook", connector_type=connector_type
            )
            return JSONResponse({"status": "ignored", "reason": "no_channel_id"})

        # Find the specific connection for this webhook
        connection = (
            await connector_service.connection_manager.get_connection_by_webhook_id(
                channel_id
            )
        )
        if not connection or not connection.is_active:
            logger.info(
                "Unknown webhook channel, will auto-expire", channel_id=channel_id
            )
            return JSONResponse(
                {"status": "ignored_unknown_channel", "channel_id": channel_id}
            )

        # Process webhook for the specific connection
        try:
            # Get the connector instance
            connector = await connector_service._get_connector(connection.connection_id)
            if not connector:
                logger.error(
                    "Could not get connector for connection",
                    connection_id=connection.connection_id,
                )
                return JSONResponse(
                    {"status": "error", "reason": "connector_not_found"}
                )

            # Let the connector handle the webhook and return affected file IDs
            affected_files = await connector.handle_webhook(payload)

            if affected_files:
                logger.info(
                    "Webhook connection files affected",
                    connection_id=connection.connection_id,
                    affected_count=len(affected_files),
                )

                # Generate JWT token for the user (needed for OpenSearch authentication)
                user = session_manager.get_user(connection.user_id)
                if user:
                    jwt_token = session_manager.create_jwt_token(user)
                else:
                    jwt_token = None

                # Trigger incremental sync for affected files
                task_id = await connector_service.sync_specific_files(
                    connection.connection_id,
                    connection.user_id,
                    affected_files,
                    jwt_token=jwt_token,
                )

                result = {
                    "connection_id": connection.connection_id,
                    "task_id": task_id,
                    "affected_files": len(affected_files),
                }
            else:
                # No specific files identified - just log the webhook
                logger.info(
                    "Webhook general change detected, no specific files",
                    connection_id=connection.connection_id,
                )

                result = {
                    "connection_id": connection.connection_id,
                    "action": "logged_only",
                    "reason": "no_specific_files",
                }

            return JSONResponse(
                {
                    "status": "processed",
                    "connector_type": connector_type,
                    "channel_id": channel_id,
                    **result,
                }
            )

        except Exception as e:
            logger.error(
                "Failed to process webhook for connection",
                connection_id=connection.connection_id,
                error=str(e),
            )
            import traceback

            traceback.print_exc()

            return JSONResponse(
                {
                    "status": "error",
                    "connector_type": connector_type,
                    "channel_id": channel_id,
                    "error": str(e),
                },
                status_code=500,
            )

    except Exception as e:
        logger.error("Webhook processing failed", error=str(e))
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_WEBHOOK_FAILED)
        return JSONResponse(
            {"error": f"Webhook processing failed: {str(e)}"}, status_code=500
        )

async def connector_disconnect(
    connector_type: str,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Disconnect a connector by deleting its connection"""

    try:
        # Get connections for this connector type and user
        connections = await connector_service.connection_manager.list_connections(
            user_id=user.user_id, connector_type=connector_type
        )

        if not connections:
            return JSONResponse(
                {"error": f"No {connector_type} connections found"},
                status_code=404,
            )

        # Delete all connections for this connector type and user
        deleted_count = 0
        for connection in connections:
            try:
                # Get the connector to cleanup any subscriptions
                connector = await connector_service._get_connector(connection.connection_id)
                if connector and hasattr(connector, 'cleanup_subscription'):
                    subscription_id = connection.config.get("webhook_channel_id")
                    if subscription_id:
                        try:
                            await connector.cleanup_subscription(subscription_id)
                        except Exception as e:
                            logger.warning(
                                "Failed to cleanup subscription",
                                connection_id=connection.connection_id,
                                error=str(e),
                            )
            except Exception as e:
                logger.warning(
                    "Could not get connector for cleanup",
                    connection_id=connection.connection_id,
                    error=str(e),
                )

            # Delete the connection
            success = await connector_service.connection_manager.delete_connection(
                connection.connection_id
            )
            if success:
                deleted_count += 1

        logger.info(
            "Disconnected connector",
            connector_type=connector_type,
            user_id=user.user_id,
            deleted_count=deleted_count,
        )

        return JSONResponse(
            {
                "status": "disconnected",
                "connector_type": connector_type,
                "deleted_connections": deleted_count,
            }
        )

    except Exception as e:
        logger.error(
            "Failed to disconnect connector",
            connector_type=connector_type,
            error=str(e),
        )
        return JSONResponse(
            {"error": f"Disconnect failed: {str(e)}"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# IBM COS-specific endpoints
# ---------------------------------------------------------------------------

class IBMCOSConfigureBody(BaseModel):
    auth_mode: str  # "iam" or "hmac"
    endpoint: str
    # IAM fields
    api_key: Optional[str] = None
    service_instance_id: Optional[str] = None
    auth_endpoint: Optional[str] = None
    # HMAC fields
    hmac_access_key: Optional[str] = None
    hmac_secret_key: Optional[str] = None
    # Optional bucket selection
    bucket_names: Optional[List[str]] = None
    # Optional: update an existing connection
    connection_id: Optional[str] = None


async def ibm_cos_defaults(
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Return current IBM COS env-var defaults for pre-filling the config dialog.

    Sensitive values (API key, HMAC secret) are masked — only whether they are
    set is returned, not the actual values.
    """
    import os

    api_key = os.getenv("IBM_COS_API_KEY", "")
    service_instance_id = os.getenv("IBM_COS_SERVICE_INSTANCE_ID", "")
    endpoint = os.getenv("IBM_COS_ENDPOINT", "")
    hmac_access_key = os.getenv("IBM_COS_HMAC_ACCESS_KEY_ID", "")
    hmac_secret_key = os.getenv("IBM_COS_HMAC_SECRET_ACCESS_KEY", "")
    disable_iam = os.getenv("OPENRAG_IBM_COS_IAM_UI", "").lower() not in ("1", "true", "yes")

    # Try to read existing connection config for this user too
    connections = await connector_service.connection_manager.list_connections(
        user_id=user.user_id, connector_type="ibm_cos"
    )
    conn_config = {}
    if connections:
        conn_config = connections[0].config or {}

    def _pick(conn_key, env_val):
        """Prefer connection config value over env var."""
        return conn_config.get(conn_key) or env_val

    return JSONResponse({
        "api_key_set": bool(api_key or conn_config.get("api_key")),
        "service_instance_id": _pick("service_instance_id", service_instance_id),
        "endpoint": _pick("endpoint_url", endpoint),
        "hmac_access_key_set": bool(hmac_access_key or conn_config.get("hmac_access_key")),
        "hmac_secret_key_set": bool(hmac_secret_key or conn_config.get("hmac_secret_key")),
        # Return which auth mode was previously used; default to hmac when IAM is disabled
        "auth_mode": conn_config.get("auth_mode", "hmac" if (disable_iam or not (api_key or conn_config.get("api_key"))) else "iam"),
        "disable_iam": disable_iam,
        # Return bucket_names from existing connection (if any)
        "bucket_names": conn_config.get("bucket_names", []),
        # Return connection_id if an existing connection exists
        "connection_id": connections[0].connection_id if connections else None,
    })


async def ibm_cos_configure(
    body: IBMCOSConfigureBody,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Create or update an IBM COS connection with explicit credentials.

    Tests the credentials by listing buckets, then persists the connection.
    Credentials are stored in the connection config dict (not env vars) so
    the connector works even without system-level env vars.
    """
    import os
    from connectors.ibm_cos.auth import create_ibm_cos_client, create_ibm_cos_resource

    # Build the config dict that will be stored in the connection
    conn_config: dict = {
        "auth_mode": body.auth_mode,
        "endpoint_url": body.endpoint,
    }

    if body.auth_mode == "iam":
        # Resolve: use supplied value, fall back to env var, fall back to existing connection
        api_key = body.api_key or os.getenv("IBM_COS_API_KEY")
        svc_id = body.service_instance_id or os.getenv("IBM_COS_SERVICE_INSTANCE_ID")

        # If still empty, pull from existing connection config
        existing_connections = await connector_service.connection_manager.list_connections(
            user_id=user.user_id, connector_type="ibm_cos"
        )
        if not api_key and existing_connections:
            api_key = existing_connections[0].config.get("api_key")
        if not svc_id and existing_connections:
            svc_id = existing_connections[0].config.get("service_instance_id")

        if not api_key or not svc_id:
            return JSONResponse(
                {"error": "IAM mode requires api_key and service_instance_id"},
                status_code=400,
            )
        conn_config["api_key"] = api_key
        conn_config["service_instance_id"] = svc_id
        if body.auth_endpoint:
            conn_config["auth_endpoint"] = body.auth_endpoint
    else:
        # HMAC mode
        hmac_access = body.hmac_access_key or os.getenv("IBM_COS_HMAC_ACCESS_KEY_ID")
        hmac_secret = body.hmac_secret_key or os.getenv("IBM_COS_HMAC_SECRET_ACCESS_KEY")

        existing_connections = await connector_service.connection_manager.list_connections(
            user_id=user.user_id, connector_type="ibm_cos"
        )
        if not hmac_access and existing_connections:
            hmac_access = existing_connections[0].config.get("hmac_access_key")
        if not hmac_secret and existing_connections:
            hmac_secret = existing_connections[0].config.get("hmac_secret_key")

        if not hmac_access or not hmac_secret:
            return JSONResponse(
                {"error": "HMAC mode requires hmac_access_key and hmac_secret_key"},
                status_code=400,
            )
        conn_config["hmac_access_key"] = hmac_access
        conn_config["hmac_secret_key"] = hmac_secret

    if body.bucket_names is not None:
        conn_config["bucket_names"] = body.bucket_names

    # Test credentials — IAM uses client (avoids ibm_botocore discovery-call bug),
    # HMAC uses resource (S3-compatible, works with MinIO).
    try:
        if conn_config.get("auth_mode", "iam") == "hmac":
            cos = create_ibm_cos_resource(conn_config)
            list(cos.buckets.all())
        else:
            cos = create_ibm_cos_client(conn_config)
            cos.list_buckets()
    except Exception as exc:
        return JSONResponse(
            {"error": f"Could not connect to IBM COS: {exc}"},
            status_code=400,
        )

    # Persist: update existing connection or create a new one
    if body.connection_id:
        existing = await connector_service.connection_manager.get_connection(body.connection_id)
        if existing and existing.user_id == user.user_id:
            await connector_service.connection_manager.update_connection(
                connection_id=body.connection_id,
                config=conn_config,
            )
            # Evict cached connector so next call gets a fresh instance
            connector_service.connection_manager.active_connectors.pop(body.connection_id, None)
            return JSONResponse({"connection_id": body.connection_id, "status": "connected"})

    # Create a fresh connection
    connection_id = await connector_service.connection_manager.create_connection(
        connector_type="ibm_cos",
        name="IBM Cloud Object Storage",
        config=conn_config,
        user_id=user.user_id,
    )
    return JSONResponse({"connection_id": connection_id, "status": "connected"})


async def ibm_cos_list_buckets(
    connection_id: str,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """List all buckets accessible with the stored IBM COS credentials."""
    from connectors.ibm_cos.auth import create_ibm_cos_client, create_ibm_cos_resource

    connection = await connector_service.connection_manager.get_connection(connection_id)
    if not connection or connection.user_id != user.user_id:
        return JSONResponse({"error": "Connection not found"}, status_code=404)
    if connection.connector_type != "ibm_cos":
        return JSONResponse({"error": "Not an IBM COS connection"}, status_code=400)

    try:
        cfg = connection.config
        if cfg.get("auth_mode", "iam") == "hmac":
            cos = create_ibm_cos_resource(cfg)
            buckets = [b.name for b in cos.buckets.all()]
        else:
            cos = create_ibm_cos_client(cfg)
            buckets = [b["Name"] for b in cos.list_buckets().get("Buckets", [])]
        return JSONResponse({"buckets": buckets})
    except Exception as exc:
        return JSONResponse({"error": f"Failed to list buckets: {exc}"}, status_code=500)


async def ibm_cos_bucket_status(
    connection_id: str,
    connector_service=Depends(get_connector_service),
    session_manager=Depends(get_session_manager),
    user: User = Depends(get_current_user),
):
    """Return all buckets for an IBM COS connection with their ingestion status.

    Each entry includes the bucket name, whether it has been ingested (is_synced),
    and the count of indexed documents from that bucket.
    """
    from connectors.ibm_cos.auth import create_ibm_cos_client, create_ibm_cos_resource

    connection = await connector_service.connection_manager.get_connection(connection_id)
    if not connection or connection.user_id != user.user_id:
        return JSONResponse({"error": "Connection not found"}, status_code=404)
    if connection.connector_type != "ibm_cos":
        return JSONResponse({"error": "Not an IBM COS connection"}, status_code=400)

    # 1. List all buckets from COS
    try:
        cfg = connection.config
        if cfg.get("auth_mode", "iam") == "hmac":
            cos = create_ibm_cos_resource(cfg)
            all_buckets = [b.name for b in cos.buckets.all()]
        else:
            cos = create_ibm_cos_client(cfg)
            all_buckets = [b["Name"] for b in cos.list_buckets().get("Buckets", [])]
    except Exception as exc:
        return JSONResponse({"error": f"Failed to list buckets: {exc}"}, status_code=500)

    # 2. Count indexed documents per bucket from OpenSearch
    ingested_counts: dict = {}
    try:
        opensearch_client = session_manager.get_user_opensearch_client(
            user.user_id, user.jwt_token
        )
        query_body = {
            "size": 0,
            "query": {"term": {"connector_type": "ibm_cos"}},
            "aggs": {
                "doc_ids": {
                    "terms": {"field": "document_id", "size": 50000}
                }
            },
        }
        index_name = get_index_name(user.user_id)
        os_resp = opensearch_client.search(index=index_name, body=query_body)
        for bucket_entry in os_resp.get("aggregations", {}).get("doc_ids", {}).get("buckets", []):
            doc_id = bucket_entry["key"]
            if "::" in doc_id:
                bucket_name = doc_id.split("::")[0]
                ingested_counts[bucket_name] = ingested_counts.get(bucket_name, 0) + 1
    except Exception:
        pass  # OpenSearch unavailable — show zero counts

    result = [
        {
            "name": bucket,
            "ingested_count": ingested_counts.get(bucket, 0),
            "is_synced": ingested_counts.get(bucket, 0) > 0,
        }
        for bucket in all_buckets
    ]
    return JSONResponse({"buckets": result})


# ---------------------------------------------------------------------------
# Amazon S3 / S3-compatible endpoints
# ---------------------------------------------------------------------------

class S3ConfigureBody(BaseModel):
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    endpoint_url: Optional[str] = None
    region: Optional[str] = None
    bucket_names: Optional[List[str]] = None
    connection_id: Optional[str] = None


async def s3_defaults(
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Return current S3 env-var defaults for pre-filling the config dialog.

    Sensitive values (secret key) are masked — only whether they are set is returned.
    """
    import os

    access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    endpoint_url = os.getenv("AWS_S3_ENDPOINT", "")
    region = os.getenv("AWS_REGION", "")

    connections = await connector_service.connection_manager.list_connections(
        user_id=user.user_id, connector_type="aws_s3"
    )
    conn_config = {}
    if connections:
        conn_config = connections[0].config or {}

    def _pick(conn_key, env_val):
        return conn_config.get(conn_key) or env_val

    return JSONResponse({
        "access_key_set": bool(access_key or conn_config.get("access_key")),
        "secret_key_set": bool(secret_key or conn_config.get("secret_key")),
        "endpoint": _pick("endpoint_url", endpoint_url),
        "region": _pick("region", region),
        "bucket_names": conn_config.get("bucket_names", []),
        "connection_id": connections[0].connection_id if connections else None,
    })


async def s3_configure(
    body: S3ConfigureBody,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Create or update an S3 connection with explicit credentials.

    Tests the credentials by listing buckets, then persists the connection.
    """
    import os
    from connectors.aws_s3.auth import create_s3_resource

    access_key = body.access_key or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = body.secret_key or os.getenv("AWS_SECRET_ACCESS_KEY")

    # Fall back to existing connection config
    existing_connections = await connector_service.connection_manager.list_connections(
        user_id=user.user_id, connector_type="aws_s3"
    )
    if not access_key and existing_connections:
        access_key = existing_connections[0].config.get("access_key")
    if not secret_key and existing_connections:
        secret_key = existing_connections[0].config.get("secret_key")

    if not access_key or not secret_key:
        return JSONResponse(
            {"error": "access_key and secret_key are required"},
            status_code=400,
        )

    conn_config: dict = {
        "access_key": access_key.strip(),
        "secret_key": secret_key.strip(),
    }
    if body.endpoint_url:
        conn_config["endpoint_url"] = body.endpoint_url.strip()
    if body.region:
        conn_config["region"] = body.region.strip()
    if body.bucket_names is not None:
        conn_config["bucket_names"] = body.bucket_names

    # Test credentials
    try:
        s3 = create_s3_resource(conn_config)
        list(s3.buckets.all())
    except Exception as exc:
        return JSONResponse(
            {"error": f"Could not connect to S3: {exc}"},
            status_code=400,
        )

    # Persist: update existing connection or create a new one
    if body.connection_id:
        existing = await connector_service.connection_manager.get_connection(body.connection_id)
        if existing and existing.user_id == user.user_id:
            await connector_service.connection_manager.update_connection(
                connection_id=body.connection_id,
                config=conn_config,
            )
            connector_service.connection_manager.active_connectors.pop(body.connection_id, None)
            return JSONResponse({"connection_id": body.connection_id, "status": "connected"})

    connection_id = await connector_service.connection_manager.create_connection(
        connector_type="aws_s3",
        name="Amazon S3",
        config=conn_config,
        user_id=user.user_id,
    )
    return JSONResponse({"connection_id": connection_id, "status": "connected"})


async def s3_list_buckets(
    connection_id: str,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """List all buckets accessible with the stored S3 credentials."""
    from connectors.aws_s3.auth import create_s3_resource

    connection = await connector_service.connection_manager.get_connection(connection_id)
    if not connection or connection.user_id != user.user_id:
        return JSONResponse({"error": "Connection not found"}, status_code=404)
    if connection.connector_type != "aws_s3":
        return JSONResponse({"error": "Not an S3 connection"}, status_code=400)

    try:
        s3 = create_s3_resource(connection.config)
        buckets = [b.name for b in s3.buckets.all()]
        return JSONResponse({"buckets": buckets})
    except Exception as exc:
        return JSONResponse({"error": f"Failed to list buckets: {exc}"}, status_code=500)


async def s3_bucket_status(
    connection_id: str,
    connector_service=Depends(get_connector_service),
    session_manager=Depends(get_session_manager),
    user: User = Depends(get_current_user),
):
    """Return all buckets for an S3 connection with their ingestion status."""
    from connectors.aws_s3.auth import create_s3_resource

    connection = await connector_service.connection_manager.get_connection(connection_id)
    if not connection or connection.user_id != user.user_id:
        return JSONResponse({"error": "Connection not found"}, status_code=404)
    if connection.connector_type != "aws_s3":
        return JSONResponse({"error": "Not an S3 connection"}, status_code=400)

    # 1. List all buckets from S3
    try:
        s3 = create_s3_resource(connection.config)
        all_buckets = [b.name for b in s3.buckets.all()]
    except Exception as exc:
        logger.exception("Failed to list buckets from S3 for connection %s", connection_id)
        return JSONResponse({"error": "Failed to list buckets"}, status_code=500)

    # 2. Count indexed documents per bucket from OpenSearch
    ingested_counts: dict = {}
    try:
        opensearch_client = session_manager.get_user_opensearch_client(
            user.user_id, user.jwt_token
        )
        query_body = {
            "size": 0,
            "query": {"term": {"connector_type": "aws_s3"}},
            "aggs": {
                "doc_ids": {
                    "terms": {"field": "document_id", "size": 50000}
                }
            },
        }
        index_name = get_index_name(user.user_id)
        os_resp = opensearch_client.search(index=index_name, body=query_body)
        for bucket_entry in os_resp.get("aggregations", {}).get("doc_ids", {}).get("buckets", []):
            doc_id = bucket_entry["key"]
            if "::" in doc_id:
                bucket_name = doc_id.split("::")[0]
                ingested_counts[bucket_name] = ingested_counts.get(bucket_name, 0) + 1
    except Exception:
        pass  # OpenSearch unavailable — show zero counts

    result = [
        {
            "name": bucket,
            "ingested_count": ingested_counts.get(bucket, 0),
            "is_synced": ingested_counts.get(bucket, 0) > 0,
        }
        for bucket in all_buckets
    ]
    return JSONResponse({"buckets": result})


# ---------------------------------------------------------------------------

async def sync_all_connectors(
    connector_service=Depends(get_connector_service),
    session_manager=Depends(get_session_manager),
    user: User = Depends(get_current_user),
):
    """
    Sync files from all active cloud connector connections.
    """
    try:
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_SYNC_START)
        jwt_token = user.jwt_token

        # Cloud connector types to sync
        cloud_connector_types = ["google_drive", "onedrive", "sharepoint", "ibm_cos", "aws_s3"]
        
        all_task_ids = []
        synced_connectors = []
        skipped_connectors = []
        errors = []

        for connector_type in cloud_connector_types:
            try:
                # First, get existing file IDs/filenames from OpenSearch for this connector type
                existing_file_ids, existing_filenames = await get_synced_file_ids_for_connector(
                    connector_type=connector_type,
                    user_id=user.user_id,
                    session_manager=session_manager,
                    jwt_token=jwt_token,
                )
                
                if not existing_file_ids and not existing_filenames:
                    logger.debug(
                        "No existing files in OpenSearch for connector type, skipping",
                        connector_type=connector_type,
                    )
                    skipped_connectors.append(connector_type)
                    continue

                # Get all active connections for this connector type and user
                connections = await connector_service.connection_manager.list_connections(
                    user_id=user.user_id, connector_type=connector_type
                )

                active_connections = [conn for conn in connections if conn.is_active]
                if not active_connections:
                    logger.debug(
                        "No active connections for connector type",
                        connector_type=connector_type,
                    )
                    continue

                # Find the first connection that actually works
                working_connection = None
                for connection in active_connections:
                    try:
                        connector = await connector_service.get_connector(connection.connection_id)
                        if connector and await connector.authenticate():
                            working_connection = connection
                            break
                    except Exception as e:
                        logger.debug(
                            "Connection validation failed",
                            connection_id=connection.connection_id,
                            error=str(e),
                        )
                        continue

                if not working_connection:
                    logger.debug(
                        "No working connection for connector type",
                        connector_type=connector_type,
                    )
                    continue

                # Sync using document_ids if available, else use filename filter
                if existing_file_ids:
                    logger.info(
                        "Syncing specific files by document_id",
                        connector_type=connector_type,
                        file_count=len(existing_file_ids),
                    )
                    task_id = await connector_service.sync_specific_files(
                        working_connection.connection_id,
                        user.user_id,
                        existing_file_ids,
                        jwt_token=jwt_token,
                    )
                else:
                    # Fallback: use filename filtering
                    logger.info(
                        "Syncing files by filename filter",
                        connector_type=connector_type,
                        filename_count=len(existing_filenames),
                    )
                    task_id = await connector_service.sync_connector_files(
                        working_connection.connection_id,
                        user.user_id,
                        max_files=None,
                        jwt_token=jwt_token,
                        filename_filter=set(existing_filenames),
                    )
                    
                all_task_ids.append(task_id)
                synced_connectors.append(connector_type)
                logger.info(
                    "Started sync for connector type",
                    connector_type=connector_type,
                    task_id=task_id,
                    file_count=len(existing_file_ids) if existing_file_ids else len(existing_filenames),
                )

            except Exception as e:
                logger.error(
                    "Failed to sync connector type",
                    connector_type=connector_type,
                    error=str(e),
                )
                errors.append({"connector_type": connector_type, "error": str(e)})

        if not all_task_ids and not errors:
            if skipped_connectors:
                return JSONResponse(
                    {
                        "status": "no_files",
                        "message": "No files to sync. Add files from cloud connectors first.",
                        "skipped_connectors": skipped_connectors,
                    },
                    status_code=200,
                )
            return JSONResponse(
                {"error": "No active cloud connector connections found"},
                status_code=404,
            )

        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_SYNC_COMPLETE)
        return JSONResponse(
            {
                "task_ids": all_task_ids,
                "status": "sync_started",
                "message": f"Started syncing files from {len(synced_connectors)} cloud connector(s)",
                "synced_connectors": synced_connectors,
                "skipped_connectors": skipped_connectors if skipped_connectors else None,
                "errors": errors if errors else None,
            },
            status_code=201,
        )

    except Exception as e:
        logger.error("Sync all connectors failed", error=str(e))
        await TelemetryClient.send_event(Category.CONNECTOR_OPERATIONS, MessageId.ORB_CONN_SYNC_FAILED)
        return JSONResponse({"error": f"Sync failed: {str(e)}"}, status_code=500)


async def connector_token(
    connector_type: str,
    connection_id: str,
    request: Request,
    connector_service=Depends(get_connector_service),
    user: User = Depends(get_current_user),
):
    """Get access token for connector API calls (e.g., Pickers)."""
    url_connector_type = connector_type

    try:
        # 1) Load the connection and verify ownership
        connection = await connector_service.connection_manager.get_connection(connection_id)
        if not connection or connection.user_id != user.user_id:
            return JSONResponse({"error": "Connection not found"}, status_code=404)

        # 2) Get the ACTUAL connector instance/type for this connection_id
        connector = await connector_service._get_connector(connection_id)
        if not connector:
            return JSONResponse(
                {"error": f"Connector not available - authentication may have failed for {url_connector_type}"},
                status_code=404,
            )

        real_type = getattr(connector, "type", None) or getattr(connection, "connector_type", None)
        if real_type is None:
            return JSONResponse({"error": "Unable to determine connector type"}, status_code=500)

        # Optional: warn if URL path type disagrees with real type
        if url_connector_type and url_connector_type != real_type:
            # You can downgrade this to debug if you expect cross-routing.
            return JSONResponse(
                {
                    "error": "Connector type mismatch",
                    "detail": {
                        "requested_type": url_connector_type,
                        "actual_type": real_type,
                        "hint": "Call the token endpoint using the correct connector_type for this connection_id.",
                    },
                },
                status_code=400,
            )

        # 3) Branch by the actual connector type
        # GOOGLE DRIVE (google-auth)
        if real_type == "google_drive" and hasattr(connector, "oauth"):
            await connector.oauth.load_credentials()
            if connector.oauth.creds and connector.oauth.creds.valid:
                expires_in = None
                try:
                    if connector.oauth.creds.expiry:
                        import time
                        expires_in = max(0, int(connector.oauth.creds.expiry.timestamp() - time.time()))
                except Exception:
                    expires_in = None

                return JSONResponse(
                    {
                        "access_token": connector.oauth.creds.token,
                        "expires_in": expires_in,
                    }
                )
            return JSONResponse({"error": "Invalid or expired credentials"}, status_code=401)

        # ONEDRIVE / SHAREPOINT (MSAL or custom)
        if real_type in ("onedrive", "sharepoint") and hasattr(connector, "oauth"):
            # Ensure cache/credentials are loaded before trying to use them
            try:
                # Prefer a dedicated is_authenticated() that loads cache internally
                if hasattr(connector.oauth, "is_authenticated"):
                    ok = await connector.oauth.is_authenticated()
                else:
                    # Fallback: try to load credentials explicitly if available
                    ok = True
                    if hasattr(connector.oauth, "load_credentials"):
                        ok = await connector.oauth.load_credentials()

                if not ok:
                    return JSONResponse({"error": "Not authenticated"}, status_code=401)

                # Check if a specific resource is requested (for SharePoint File Picker v8)
                # The File Picker requires a token with SharePoint as the audience, not Graph
                resource = request.query_params.get("resource")

                if resource and is_valid_sharepoint_url(resource):
                    # SharePoint File Picker v8 needs a SharePoint-scoped token
                    logger.info(f"Acquiring SharePoint-scoped token for resource: {resource}")
                    if hasattr(connector.oauth, "get_access_token_for_resource"):
                        access_token = connector.oauth.get_access_token_for_resource(resource)
                    else:
                        # Fallback for connectors without resource-specific token support
                        access_token = connector.oauth.get_access_token()
                else:
                    # Default: Microsoft Graph token
                    access_token = connector.oauth.get_access_token()
                # MSAL result has expiry, but we’re returning a raw token; keep expires_in None for simplicity
                return JSONResponse({"access_token": access_token, "expires_in": None})
            except ValueError as e:
                # Typical when acquire_token_silent fails (e.g., needs re-auth)
                return JSONResponse({"error": f"Failed to get access token: {str(e)}"}, status_code=401)
            except Exception as e:
                return JSONResponse({"error": f"Authentication error: {str(e)}"}, status_code=500)

        return JSONResponse({"error": "Token not available for this connector type"}, status_code=400)

    except Exception as e:
        logger.error("Error getting connector token", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
