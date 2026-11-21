.PHONY: venv test browser-test check build up down logs update pull release

COMPOSE = docker compose -f docker-compose.yml -f docker-compose.build.yml
# Shown in the footer as "snsdl <version> (<commit>)"; a checkout on a tag reports the tag.
export APP_VERSION ?= $(shell git describe --tags --exact-match 2>/dev/null || echo dev)
export APP_COMMIT ?= $(shell git rev-parse --short=7 HEAD 2>/dev/null)

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

# make release VERSION=1.2.3: release the Unreleased notes, commit, tag; prints the push to run.
release:
	@test -n "$(VERSION)" || { echo "usage: make release VERSION=1.2.3"; exit 1; }
	@b=$$(git branch --show-current); test "$$b" = main || { echo "release from main (on $${b:-detached HEAD})"; exit 1; }
	@test -z "$$(git status --porcelain)" || { echo "commit or stash your changes first"; exit 1; }
	@test -z "$$(git tag -l v$(VERSION))" || { echo "tag v$(VERSION) already exists"; exit 1; }
	@python3 tools/release.py $(VERSION) && git add CHANGELOG.md && git commit -q -m "release: v$(VERSION)" \
	  || { git restore --staged --worktree CHANGELOG.md; echo "release aborted, CHANGELOG.md restored"; exit 1; }
	@git tag -a v$(VERSION) -m "v$(VERSION)" \
	  || { git reset -q --hard HEAD~1; echo "tagging failed, release commit undone"; exit 1; }
	@echo "tagged v$(VERSION); publish with: git push origin main v$(VERSION)"
