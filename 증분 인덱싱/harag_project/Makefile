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

# ── CI 재현(푸시 전 로컬 검증) ──
ci-contract:
	python scripts/run_contract_tests.py

ci-behavior:
	PYTHONPATH=src python -m pytest tests/behavior -q --asyncio-mode=auto

ci-integration:
	@for t in tests/integration/*.py; do echo "== $$t =="; PYTHONPATH=src python $$t || exit 1; done

# CI 차단 게이트 전체(GitHub Actions와 동일 순서)
ci: ci-contract ci-behavior ci-integration
	@echo "✓ 모든 차단 게이트 통과 — 푸시 안전"
