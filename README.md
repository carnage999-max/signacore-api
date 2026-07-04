# Signacore API

Django 5.x + DRF microservice for Signacore.

## Scope

This folder is isolated from the existing frontend codebase and is intended to become the backend source of truth for:

- PDF upload and field detection via PyMuPDF
- document and signer lifecycle management
- signer OTP verification
- PDF flattening and signed file storage
- Celery-backed email and background jobs

## Local bootstrap

```bash
cd signacore-api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
python manage.py migrate
python manage.py test
python manage.py runserver 0.0.0.0:8010
```

## Current foundation

- Django project settings for local SQLite and server PostgreSQL
- DRF/Celery/bootstrap wiring
- core Signacore models and enums
- health endpoint at `/api/health/`
- PyMuPDF service shell for AcroForm and heuristic analysis

## Docker deployment

This service is set up for container deployment while still using server-hosted infrastructure:

- PostgreSQL stays on the server, not in `docker-compose`
- Redis stays on the server, not in `docker-compose`
- uploaded and signed documents live on a server bind mount, not S3
- all Signacore containers join external Docker network `shared-net`

### Deployment artifacts

- `Dockerfile`
- `docker-compose.yml`
- `.env.docker.example`
- `docker/entrypoint.sh`

### First run

```bash
cd signacore-api
cp .env.docker.example .env.docker
docker network create shared-net || true
docker compose up --build -d
```
