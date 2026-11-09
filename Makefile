.PHONY: venv test browser-test check build up down logs update pull

COMPOSE = docker compose -f docker-compose.yml -f docker-compose.build.yml

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

test:
	.venv/bin/pytest -q

browser-test:
	.venv/bin/python -m playwright install chromium
	.venv/bin/pytest -q tests/browser

check:
	.venv/bin/ruff check app tests tools
	node --check app/static/app.js
	node --check app/static/sw.js
	.venv/bin/python -m pytest -q -p no:cacheprovider --ignore=tests/browser

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

update:
	$(COMPOSE) build --no-cache --pull && $(COMPOSE) up -d

pull:
	docker compose pull && docker compose up -d
