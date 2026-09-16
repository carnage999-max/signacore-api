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

Install the Git hooks once per checkout so every commit checks changed Python
files with isort, Black, and Ruff:

```bash
pre-commit install
make lint
```

The hooks run automatically before each commit. `make lint` runs them against
the full repository when you want to check everything explicitly.

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
make lint
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

### CI/CD deployment gate

GitHub Actions runs the full pre-commit quality check and the Django test
suite for pull requests and pushes to `main`. A successful push to `main`
then triggers the Coolify staging deployment through an authenticated deploy
webhook. Production is not triggered by this workflow.

Configure the staging Coolify application as follows:

1. Turn off **Configuration > Advanced > Deployment & Git > Auto Deploy**.
   This is required because the GitHub App can otherwise deploy the push before
   GitHub Actions finishes.
2. Enable Coolify API access under **Settings > Configuration > Advanced**.
3. Create a Coolify API token with only the `deploy` permission.
4. Copy the staging application's **Deploy Webhook (auth required)** URL.
   The webhook URL must use HTTPS; do not send the bearer token to a plain HTTP
   IP address or an untrusted public endpoint.
5. Add these GitHub repository secrets under **Settings > Secrets and
   variables > Actions**:
   `COOLIFY_STAGING_DEPLOY_WEBHOOK` for the copied URL and
   `COOLIFY_STAGING_DEPLOY_TOKEN` for the deploy-only API token.

The workflow never stores the webhook URL or token in Git. It deploys staging
only after isort, Black, Ruff, migration checks, and all Django tests pass.
Coolify's direct Git App deployment must remain disabled for this gate to be
effective.

### First run

```bash
cd signacore-api
cp .env.example .env
mkdir -p /mnt/data/media/signa-core /srv/apps/signacore-api/staticfiles
docker compose up --build -d
```

### Same-server staging with Coolify

Staging is a second Coolify Compose application on the same server. It uses
the same Git repository and Compose file, but must have a separate database,
Redis database, encryption key, shared secret, storage root, host port, and
container names. Never point staging at production PostgreSQL or
`/mnt/data/media/signa-core`.

#### 1. Prepare the server

The staging storage directory should be separate from production:

```bash
sudo mkdir -p /mnt/data/media/signa-core-staging
sudo mkdir -p /srv/apps/signacore-api-staging/staticfiles
sudo docker network inspect shared-net >/dev/null 2>&1 || sudo docker network create shared-net
```

The API Compose file binds staging to `127.0.0.1:8011`. Do not use port
`8010`, and do not reuse the production container names.

#### 2. Create an isolated PostgreSQL database

Create a new database and login role. The staging role needs `CREATEDB` so
`make validate` can create Django's temporary test database:

```bash
sudo docker exec -it postgres psql -U admin -d postgres
```

Run the following SQL, replacing the password with a strong generated value:

```sql
CREATE USER signacore_staging_user WITH PASSWORD 'replace-with-staging-db-password' CREATEDB;
CREATE DATABASE signacore_staging_db OWNER signacore_staging_user;
GRANT ALL PRIVILEGES ON DATABASE signacore_staging_db TO signacore_staging_user;
\q
```

If PostgreSQL is a separate Coolify resource, use its internal hostname and
attach both the PostgreSQL resource and this Compose application to
`shared-net`. Do not use `localhost` from inside the API container.

#### 3. Allocate a separate Redis database

Use a different Redis database number from production. For example, if
production uses database `3`, staging can use database `4`:

```text
REDIS_URL=redis://redis:6379/4
CELERY_BROKER_URL=redis://redis:6379/4
CELERY_RESULT_BACKEND=redis://redis:6379/4
```

The Redis container must also be reachable on `shared-net`. A separate Redis
resource is preferable if the production Redis instance is managed by another
team or has no database isolation.

#### 4. Create staging DNS records

Create an A record for the staging API, for example:

```text
api-staging.mysignacore.com -> <server-public-ip>
```

If a staging web app will be deployed too, use separate hosts such as
`staging.mysignacore.com` and `sign-staging.mysignacore.com`. Do not use the
production app or signer domains in staging CORS, OAuth, or email links.

#### 5. Create the Coolify application

In Coolify:

1. Create a new Project named `SignaCore Staging`.
2. Add a Docker Compose resource from the `signa-core` Git repository.
3. Select the `main` branch if staging should auto-deploy every push.
4. Set the Compose file to `signacore-api/docker-compose.yml` if the repository root contains both apps; if this API repository is connected directly, use `docker-compose.yml`.
5. Add the staging environment variables from the next section in Coolify's Environment Variables panel. Keep them secret and do not commit `.env`.
6. Configure the public domain on the `api` service as `https://api-staging.mysignacore.com` and target the service's internal port `8010`. The host mapping remains `8011` only for direct server checks.
7. Enable automatic deployments/webhooks for `main`.
8. Deploy once manually and wait for `api`, `worker`, and `beat` to become healthy/running.

The Compose variables that prevent collision are:

```text
SIGNACORE_API_PORT=8011
SIGNACORE_API_CONTAINER_NAME=signacore-staging-api
SIGNACORE_WORKER_CONTAINER_NAME=signacore-staging-worker
SIGNACORE_BEAT_CONTAINER_NAME=signacore-staging-beat
```

#### 6. Set the staging environment

Use the following values as the starting point. Generate new values for every
secret; do not copy production secrets or `FERNET_KEY`:

```text
SECRET_KEY=<unique-staging-django-secret>
DEBUG=False
ALLOWED_HOSTS=api-staging.mysignacore.com,127.0.0.1,localhost
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
CSRF_TRUSTED_ORIGINS=https://staging.mysignacore.com,https://sign-staging.mysignacore.com

DB_NAME=signacore_staging_db
DB_USER=signacore_staging_user
DB_PASSWORD=<unique-staging-db-password>
DB_HOST=<postgresql-host-on-shared-net>
DB_PORT=5432
DB_SSLMODE=require

REDIS_URL=redis://redis:6379/4
CELERY_BROKER_URL=redis://redis:6379/4
CELERY_RESULT_BACKEND=redis://redis:6379/4

FERNET_KEY=<unique-staging-fernet-key>
SIGNACORE_TEMP_ROOT=/run/signacore
SIGNACORE_HOST_STORAGE_ROOT=/mnt/data/media/signa-core-staging
SIGNACORE_STORAGE_ROOT=/mnt/data/media/signa-core
MEDIA_ROOT=/mnt/data/media/signa-core
MEDIA_URL=/media/
SIGNACORE_HOST_STATIC_ROOT=/srv/apps/signacore-api-staging/staticfiles
STATIC_ROOT=/srv/apps/signacore-api/staticfiles

CORS_ALLOWED_ORIGINS=https://staging.mysignacore.com,https://sign-staging.mysignacore.com
SIGNACORE_SHARED_SECRET=<unique-staging-service-secret>
SIGNACORE_SERVICE_USERNAME=signacore-staging-service
SIGNACORE_APP_URL=https://staging.mysignacore.com
SIGNACORE_SIGNER_PORTAL_URL=https://sign-staging.mysignacore.com
SIGNING_LINK_EXPIRY_DAYS=7
OTP_EXPIRY_MINUTES=10
OTP_RESEND_COOLDOWN_SECONDS=60

STRIPE_SECRET_KEY=sk_test_<staging-test-mode-key>
STRIPE_WEBHOOK_SECRET=whsec_<staging-webhook-secret>
STRIPE_API_VERSION=2026-02-25.clover

EMAIL_HOST=smtp.resend.com
EMAIL_PORT=465
EMAIL_USE_SSL=True
EMAIL_HOST_USER=resend
EMAIL_HOST_PASSWORD=<staging-email-provider-key>
DEFAULT_FROM_EMAIL=SignaCore Staging <verified-staging-sender@mysignacore.com>

SIGNACORE_API_PORT=8011
SIGNACORE_API_CONTAINER_NAME=signacore-staging-api
SIGNACORE_WORKER_CONTAINER_NAME=signacore-staging-worker
SIGNACORE_BEAT_CONTAINER_NAME=signacore-staging-beat
GUNICORN_WORKERS=2
GUNICORN_TIMEOUT=120
CELERY_CONCURRENCY=2
```

Leave Google and Apple credentials empty unless OAuth is being tested. If
OAuth is enabled, register only the exact staging callback URLs with each
provider and use staging client credentials:

```text
GOOGLE_OAUTH_REDIRECT_URI=https://staging.mysignacore.com/api/auth/oauth/callback/google
APPLE_OAUTH_REDIRECT_URI=https://staging.mysignacore.com/api/auth/oauth/callback/apple
```

#### 7. Run the first deployment checks

From the API repository checkout used by the deployment, verify the rendered
Compose configuration and service separation:

```bash
docker compose config --quiet
docker ps --format '{{.Names}}' | grep signacore-staging
curl -I https://api-staging.mysignacore.com/api/health/
```

Then run the deployment validation against the staging database. If executing
from the server checkout, ensure its `.env` contains the staging values before
running this command:

```bash
make validate
```

This checks pending migrations, Django production checks, organization data
isolation, authentication/OTP throttling, and the full API test suite. OAuth
provider exchanges remain mocked in the suite and require a separate manual
staging test with provider credentials.

#### 8. Production promotion

Pushing to `main` can deploy staging automatically through the Coolify
webhook. Keep production deployment manual: review the staging health check,
logs, migrations, email delivery, signer flow, and billing webhook behavior,
then deploy production deliberately with its existing production environment.

## Commercial account configuration

Stripe credentials are configured only on the Django service. Subscription amounts, currencies, billing intervals, and availability are managed by the superuser under **Billing plan prices** in Django admin. Register this production webhook in Stripe:

```text
https://api.mysignacore.com/api/billing/webhooks/stripe/
```

Subscribe it to `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`.

Google and Apple client credentials are split between the web app and API: the web app receives public client IDs and exact callback URLs, while Django receives provider secrets and the Apple `.p8` private key. The full variable list is in `.env.example`.
