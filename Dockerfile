FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    MEDIA_ROOT=/srv/signacore/storage \
    STATIC_ROOT=/srv/signacore/staticfiles

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements ./requirements
RUN pip install --upgrade pip \
    && pip install -r requirements/dev.txt

COPY . .

RUN chmod +x docker/entrypoint.sh \
    && mkdir -p /srv/signacore/storage /srv/signacore/staticfiles

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["gunicorn", "signacore_api.wsgi:application", "--bind", "0.0.0.0:8010", "--workers", "3", "--timeout", "120"]

