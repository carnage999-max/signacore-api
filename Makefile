.DEFAULT_GOAL := help

COMPOSE := docker compose
RUN_MANAGE := $(COMPOSE) run --rm --entrypoint "" api python manage.py

.PHONY: \
	help \
	makemigrations \
	migrate \
	collectstatic \
	test \
	docker-up \
	docker-down \
	docker-restart \
	docker-destroy \
	docker-build \
	docker-build-no-cache \
	up \
	down \
	restart \
	destroy \
	build \
	build-no-cache

help:
	@printf "\nSignacore API commands\n\n"
	@printf "  make makemigrations      Create Django migrations in Docker\n"
	@printf "  make migrate             Apply Django migrations in Docker\n"
	@printf "  make collectstatic       Collect static assets in Docker\n"
	@printf "  make test                Run the Signacore test suite in Docker\n"
	@printf "  make docker-up           Start containers in detached mode\n"
	@printf "  make docker-down         Stop containers\n"
	@printf "  make docker-restart      Restart containers\n"
	@printf "  make docker-destroy      Stop containers and remove volumes\n"
	@printf "  make docker-build        Build container images\n"
	@printf "  make docker-build-no-cache  Build images without cache\n"
	@printf "  make up/down/restart/destroy/build/build-no-cache  Short aliases\n\n"

makemigrations:
	$(RUN_MANAGE) makemigrations

migrate:
	$(RUN_MANAGE) migrate

collectstatic:
	$(RUN_MANAGE) collectstatic --noinput

test:
	$(RUN_MANAGE) test

docker-up:
	$(COMPOSE) up -d

docker-down:
	$(COMPOSE) down

docker-restart:
	$(COMPOSE) restart

docker-destroy:
	$(COMPOSE) down -v --remove-orphans

docker-build:
	$(COMPOSE) build

docker-build-no-cache:
	$(COMPOSE) build --no-cache

up: docker-up

down: docker-down

restart: docker-restart

destroy: docker-destroy

build: docker-build

build-no-cache: docker-build-no-cache
