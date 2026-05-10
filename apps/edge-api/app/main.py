from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as api_router
from app.core.config import get_settings
from app.db.seed import seed_default_site
from app.db.session import get_session_factory, init_database
from app.services.discovery import prune_legacy_fixture_inventory
from app.services.eebus_runtime import get_eebus_runtime_manager


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_database()
        session_factory = get_session_factory()
        with session_factory() as session:
            seed_default_site(session)
            prune_legacy_fixture_inventory(session)
        yield
        get_eebus_runtime_manager().stop()

    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        summary="Local API for the Helios Home agent-first HEMS runtime.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(api_router, prefix=settings.api_prefix)

    @application.get("/")
    def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "message": "Helios Home local runtime is online.",
        }

    @application.get("/health")
    def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "mode": "standard",
            "database_ready": True,
        }

    return application


app = create_app()
