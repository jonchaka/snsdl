.PHONY: venv test build up down logs update

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

test:
	.venv/bin/pytest -q

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
