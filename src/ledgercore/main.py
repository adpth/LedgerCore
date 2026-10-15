import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ledgercore.api.payout_routes import router as payout_router
from ledgercore.api.problems import install_problem_handlers
from ledgercore.api.routes import router
from ledgercore.config import Settings, get_settings
from ledgercore.db.session import create_engine, create_sessionmaker
from ledgercore.domain import FakePayoutProvider, PayoutProvider

log = logging.getLogger(__name__)


class AlwaysSucceedsLocalProvider(FakePayoutProvider):
    """Default provider for local runs: pays every payout instantly. NO REAL MONEY MOVES."""


def create_app(
    settings: Settings | None = None, payout_provider: PayoutProvider | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        engine = create_engine(
            cfg,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_max_overflow,
            pool_timeout=cfg.db_pool_timeout_s,
        )
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        if payout_provider is None:
            log.warning("no payout provider injected: using AlwaysSucceedsLocalProvider (fake)")
        app.state.payout_provider = payout_provider or AlwaysSucceedsLocalProvider()
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="LedgerCore", lifespan=lifespan)
    install_problem_handlers(app)
    app.include_router(router)
    app.include_router(payout_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
