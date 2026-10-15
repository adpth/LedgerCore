.PHONY: sync up down migrate api lint typecheck test check drift-check relay payout-recovery

sync:
	uv sync

up:
	docker compose up -d --wait

down:
	docker compose down

migrate:
	uv run alembic upgrade head

api:
	uv run uvicorn ledgercore.main:app --reload

drift-check:
	uv run ledgercore-drift-check

relay:
	uv run ledgercore-outbox-relay

payout-recovery:
	uv run ledgercore-payout-recovery

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint typecheck test
