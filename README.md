# OpenRAG + Docling + VLM Setup
Voraussetzungen

- Docker

- Docker Compose

Start:

- git clone https://github.com/tillwendel/openrag-docling.git

- cd openrag-docling

- cp .env.example .env -> edit file following this: https://docs.openr.ag/docker#setup

- docker-compose up -d

### Services

OpenRAG Frontend: http://localhost:3000

Langflow: http://localhost:7860

Docling Serve: http://localhost:5001

Hinweis

Beim ersten Start werden Container und ggf. Modelle geladen (kann etwas dauern).
