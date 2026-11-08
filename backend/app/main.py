import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.database.session import Database
from app.document_engine.docx.analyzer import analyze_docx
from app.document_engine.pptx.analyzer import analyze_pptx
from app.document_engine.xlsx.analyzer import analyze_spreadsheet
from app.services.gate import SessionGate, SlotLimiter
from app.services.storage import LocalStorage
from app.workflows.graph import Deps, build_graph

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, llm=None, checkpointer=None, database: Database | None = None) -> FastAPI:
    """Build the app. Collaborators can be injected (tests pass a fake LLM, in-memory checkpointer and test DB)."""
    settings = settings or get_settings()
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = app.state
        st.settings, st.gate = settings, SessionGate()
        st.slots = SlotLimiter(settings.max_concurrent_chats)
        st.db = database or Database(settings.sqlalchemy_url)
        st.storage = LocalStorage(settings.storage_path)
        st.analyzers = {"docx": analyze_docx, "pptx": analyze_pptx, "xlsx": analyze_spreadsheet}
        pool = None
        cp = checkpointer
        if cp is None:
            try:
                from app.workflows.checkpoint import make_checkpointer, make_pool
                pool = make_pool()
                cp = make_checkpointer(pool)
            except Exception:  # noqa: BLE001 - degrade to in-memory state rather than refuse to start
                log.exception("Postgres checkpointer unavailable; conversation state will not survive restarts")
                from langgraph.checkpoint.memory import MemorySaver
                cp = MemorySaver()
        model = llm
        if model is None and settings.groq_api_key:
            from app.llm.provider import GroqLLM
            model = GroqLLM()
        st.graph = build_graph(Deps(model, st.storage), checkpointer=cp) if model is not None else None
        if st.graph is None:
            log.warning("GROQ_API_KEY not set: uploads work, chat is disabled")
        yield
        if pool is not None:
            pool.close()
        if database is None:
            st.db.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    register_error_handlers(app)
    app.include_router(router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "app": settings.app_name, "env": settings.app_env,
                "chat_enabled": bool(getattr(app.state, "graph", None))}

    return app


app = create_app()
