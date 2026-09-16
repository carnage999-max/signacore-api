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
- organization-isolated company accounts and signer identities
- Stripe subscription checkout, portal access, and signed webhook processing
- Google and Apple account sign-in through raw `httpx` clients

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

Signing invitations point to `https://sign.mysignacore.com`, which must proxy to the Django service. Admin and billing redirects return to `https://mysignacore.com`; these hosts are configured independently with `SIGNACORE_SIGNER_PORTAL_URL` and `SIGNACORE_APP_URL`.

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

Original PDFs, completed PDFs, signature images, signer-entered values, and document-sensitive metadata are encrypted at rest with `FERNET_KEY`. PDF processing uses private temporary files under `SIGNACORE_TEMP_ROOT`; Docker mounts `/run/signacore` as memory-backed storage for the API container.

Before the first production start, set `DEBUG=False`, a unique `SECRET_KEY` of at least 50 characters, and the HTTPS hardening variables from `.env.example`. The API trusts the reverse-proxy HTTPS header, so Nginx must send `X-Forwarded-Proto https`; direct HTTP access to the Gunicorn port is expected to redirect in production.

Treat `FERNET_KEY` as a permanent data-encryption key. Back it up outside the server and do not replace it during a normal deployment. Losing it makes encrypted records unrecoverable; changing it requires an explicit key-rotation migration.

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
make validate
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

### Same-server staging

Staging should be a separate Coolify deployment using this same Compose file and a separate environment. Set `SIGNACORE_API_CONTAINER_NAME=signacore-staging-api`, `SIGNACORE_WORKER_CONTAINER_NAME=signacore-staging-worker`, `SIGNACORE_BEAT_CONTAINER_NAME=signacore-staging-beat`, and `SIGNACORE_API_PORT=8011` so it cannot collide with production. Use a separate PostgreSQL database, storage root, Redis database, encryption key, shared secret, OAuth callback URLs, and Stripe test-mode credentials. Never point staging at production PostgreSQL or `/mnt/data/media/signa-core`.

After the staging deployment is healthy, run:

```bash
make validate
```

This checks that the running database has no pending migrations, runs Django's production deployment checks, and runs the full isolated API test suite, including document organization isolation and authentication/OTP abuse throttling. OAuth provider exchanges remain mocked in this suite and must be tested separately with staging credentials. The staging PostgreSQL user must have `CREATEDB` so Django can create its temporary test database.

## Commercial account configuration

Stripe credentials are configured only on the Django service. Subscription amounts, currencies, billing intervals, and availability are managed by the superuser under **Billing plan prices** in Django admin. Register this production webhook in Stripe:

```text
https://api.mysignacore.com/api/billing/webhooks/stripe/
```

Subscribe it to `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`.

Google and Apple client credentials are split between the web app and API: the web app receives public client IDs and exact callback URLs, while Django receives provider secrets and the Apple `.p8` private key. The full variable list is in `.env.example`.
