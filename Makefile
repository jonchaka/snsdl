.PHONY: venv test browser-test build up down logs update

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

test:
	.venv/bin/pytest -q

browser-test:
	.venv/bin/python -m playwright install chromium
	.venv/bin/pytest -q tests/browser

build:
	docker compose build

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

update:
	docker compose build --no-cache --pull && docker compose up -d
