# Signacore API

Django 5.x + DRF microservice for Signacore.

## Scope

This folder is isolated from the existing frontend codebase and is intended to become the backend source of truth for:

- PDF upload and field detection via PyMuPDF
- document and signer lifecycle management
- signer OTP verification
- Django-backed admin login and superuser-managed admin accounts
- admin audit logging for document, signer, user, and password actions
- PDF flattening and signed file storage
- Celery-backed email and background jobs

## Local bootstrap

```bash
cd signacore-api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
cp .env.example .env
python manage.py migrate
python manage.py test
python manage.py runserver 127.0.0.1:8010
```

## Current foundation

- Django project settings for local SQLite and server PostgreSQL
- DRF/Celery/bootstrap wiring
- core Signacore models and enums
- health endpoint at `/api/health/`
- PyMuPDF service shell for AcroForm and heuristic analysis

## Admin users

Create the first owner account with Django:

```bash
python manage.py createsuperuser
```

That superuser can sign in to the standalone SignaCore web app, create additional admin users, and change admin passwords. New admins receive an email notification when their account is created. Password changes also trigger an email notification and an audit log entry.

The backend also keeps Django's default admin available as a superuser fallback:

```text
https://api.mysignacore.com/admin/
```

The default admin is branded with SignaCore styling and links to the generated API documentation.

## API documentation

DRF Spectacular exposes the live OpenAPI contract and a branded docs UI for Django staff users:

```text
https://api.mysignacore.com/api/docs/
https://api.mysignacore.com/api/schema/
https://api.mysignacore.com/api/redoc/
```

Swagger and Redoc assets are served from `drf-spectacular-sidecar`, so the docs do not depend on external CDN assets at runtime.

## Docker deployment

This service is set up for container deployment while still using server-hosted infrastructure:

- PostgreSQL stays on the server, not in `docker-compose`
- Redis stays on the server, not in `docker-compose`
- uploaded and signed documents live on a server bind mount, not S3
- all Signacore containers join external Docker network `shared-net`
- Signacore media uses `/mnt/data/media/signa-core/`
- Signacore static files use `/srv/apps/signacore-api/staticfiles/`

### Deployment artifacts

- `Dockerfile`
- `docker-compose.yml`
- `.env.example`
- `docker/entrypoint.sh`

## Make commands

These commands are Docker-native and are intended to work on the server without a repo-local virtualenv.

```bash
make help
make makemigrations
make migrate
make collectstatic
make test
make docker-up
make docker-down
make docker-restart
make docker-destroy
make docker-build
make docker-build-no-cache
```

Short aliases are also available for the Docker lifecycle commands:

```bash
make up
make down
make restart
make destroy
make build
make build-no-cache
```

### First run

```bash
cd signacore-api
cp .env.example .env
mkdir -p /mnt/data/media/signa-core /srv/apps/signacore-api/staticfiles
docker compose up --build -d
```
