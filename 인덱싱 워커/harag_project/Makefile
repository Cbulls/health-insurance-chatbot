.PHONY: install test test-contract test-behavior lint up down

install:
	pip install -e ".[dev,parsing]"

# 계약 테스트(스크립트형) — 항상 초록이어야
test-contract:
	@for t in tests/contract/*.py; do echo "== $$t =="; PYTHONPATH=src python $$t; done

# 동작 테스트(pytest) — 구현 진행에 따라 빨강→초록
test-behavior:
	PYTHONPATH=src python -m pytest tests/behavior -v

test: test-contract test-behavior

lint:
	ruff check src tests && mypy src

up:
	docker compose -f docker/docker-compose.yml up --build

down:
	docker compose -f docker/docker-compose.yml down
