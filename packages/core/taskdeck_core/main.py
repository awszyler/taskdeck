from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from taskdeck_core.api.attachments import router as attachments_router
from taskdeck_core.api.audit import router as audit_router
from taskdeck_core.api.auth import router as auth_router
from taskdeck_core.api.costs import router as costs_router
from taskdeck_core.api.im_identity import router as im_identity_router
from taskdeck_core.api.intent import router as intent_router
from taskdeck_core.api.internal import router as internal_router
from taskdeck_core.api.members import router as members_router
from taskdeck_core.api.memory import router as memory_router
from taskdeck_core.api.runners import router as runners_router
from taskdeck_core.api.sandbox import router as sandbox_router
from taskdeck_core.api.stt import router as stt_router
from taskdeck_core.api.tasks import router as tasks_router
from taskdeck_core.api.workspaces import router as workspaces_router
from taskdeck_core.api.ws import EventBus, ws_router
from taskdeck_core.artifacts.store import LocalFSArtifactStore
from taskdeck_core.audit.sink import AuditEventSink
from taskdeck_core.auth.bootstrap import router as bootstrap_router
from taskdeck_core.auth.cognito_client import CognitoClient
from taskdeck_core.auth.cognito_jwt import CognitoJwtVerifier
from taskdeck_core.auth.flow_store import InMemoryFlowStore
from taskdeck_core.auth.session import make_fernet
from taskdeck_core.cost.pricing import Pricing
from taskdeck_core.cost.sink import CostEventSink
from taskdeck_core.crp.handler import crp_router
from taskdeck_core.crp.hub import RunnerHub
from taskdeck_core.db.engine import db_lifespan
from taskdeck_core.deps.resolver import DependencyResolver
from taskdeck_core.dispatcher.service import Dispatcher
from taskdeck_core.hardening.body_size import BodySizeMiddleware
from taskdeck_core.hardening.metrics import setup_metrics
from taskdeck_core.hardening.rate_limit import (
    RateLimitExceeded,
    SlowAPIMiddleware,
    _rate_limit_handler,
    limiter,
)
from taskdeck_core.im.wecom.binder import BindCodeCache
from taskdeck_core.im.wecom.client import WecomClient, _NoopWecomClient
from taskdeck_core.im.wecom.handler import router as wecom_router
from taskdeck_core.im.wecom.notifier import WecomNotifier
from taskdeck_core.intent.bedrock_client import BedrockIntentClient
from taskdeck_core.intent.parser import IntentParser, OpenAIClient, _MissingLLMClient
from taskdeck_core.memory.embedding import (
    BedrockEmbeddingClient,
    OpenAIEmbeddingClient,
    _MissingEmbeddingClient,
)
from taskdeck_core.memory.ingestor import MemoryIngestor
from taskdeck_core.metrics.refresher import run_forever as run_metrics_refresher
from taskdeck_core.settings import Settings
from taskdeck_core.stt.client import (
    AwsTranscribeSTTClient,
    OpenAISTTClient,
    _MissingSTTClient,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()  # type: ignore[call-arg]

    bus = EventBus()
    hub = RunnerHub()
    # artifact_store is set in lifespan once the dir is known; dispatcher gets it via setter below.
    # embedding_client is set in lifespan; dispatcher gets it via setter below.
    dispatcher = Dispatcher(hub, publisher=bus)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = s
        app.state.event_bus = bus
        app.state.runner_hub = hub
        app.state.dispatcher = dispatcher
        # Cognito wiring (P5.1). disabled mode skips entirely.
        flow_store_gc_task: asyncio.Task[None] | None = None
        if s.auth_mode == "cognito":
            app.state.fernet = make_fernet(s.session_encryption_key)
            app.state.cognito_client = CognitoClient(
                region=s.cognito_region,
                client_id=s.cognito_client_id,
                user_pool_id=s.cognito_user_pool_id,
            )
            app.state.cognito_jwt_verifier = CognitoJwtVerifier(
                region=s.cognito_region,
                user_pool_id=s.cognito_user_pool_id,
                client_id=s.cognito_client_id,
            )
            flow_store = InMemoryFlowStore()
            app.state.flow_store = flow_store
            flow_store_gc_task = asyncio.create_task(flow_store.gc_loop())
        s.artifact_dir.mkdir(parents=True, exist_ok=True)
        app.state.artifact_store = LocalFSArtifactStore(s.artifact_dir)
        dispatcher._artifact_store = app.state.artifact_store
        if s.memory_enabled:
            if s.bedrock_embedding_enabled:
                embedding_client: object = BedrockEmbeddingClient(
                    model_id=s.bedrock_embedding_model,
                    region=s.bedrock_region,
                )
            elif s.litellm_base_url and s.litellm_api_key:
                embedding_client = OpenAIEmbeddingClient(
                    base_url=s.litellm_base_url,
                    api_key=s.litellm_api_key,
                    model=s.memory_embedding_model,
                )
            else:
                embedding_client = _MissingEmbeddingClient()
        else:
            embedding_client = _MissingEmbeddingClient()
        app.state.embedding_client = embedding_client
        dispatcher._embedding_client = embedding_client  # type: ignore[attr-defined]
        dispatcher._settings = s  # type: ignore[attr-defined]
        app.state.wecom_bind_codes = BindCodeCache()
        if s.bedrock_intent_enabled:
            llm_client: object = BedrockIntentClient(
                model_id=s.bedrock_intent_model,
                region=s.bedrock_intent_region,
            )
            intent_model = s.bedrock_intent_model
        elif s.litellm_base_url and s.litellm_api_key:
            llm_client = OpenAIClient(base_url=s.litellm_base_url, api_key=s.litellm_api_key)
            intent_model = s.intent_parser_model
        else:
            llm_client = _MissingLLMClient()
            intent_model = s.intent_parser_model
        app.state.intent_parser = IntentParser(
            client=llm_client,  # type: ignore[arg-type]
            model=intent_model,
            timeout=s.intent_parser_timeout_seconds,
        )
        # STT backend selection. Mirror api/stt.py's logic so the
        # eagerly-instantiated client doesn't override what the lazy
        # path would have picked.
        if s.stt_backend == "aws-transcribe":
            bucket = s.stt_s3_bucket or s.attachment_bucket
            if not bucket:
                stt_client: object = _MissingSTTClient()
            else:
                stt_client = AwsTranscribeSTTClient(
                    bucket=bucket,
                    region=s.stt_region,
                    bus=bus,
                    language_options=[
                        c.strip()
                        for c in s.stt_language_options.split(",")
                        if c.strip()
                    ],
                )
        elif s.litellm_base_url and s.litellm_api_key:
            stt_client = OpenAISTTClient(
                base_url=s.litellm_base_url,
                api_key=s.litellm_api_key,
                bus=bus,
                model=s.stt_model,
            )
        else:
            stt_client = _MissingSTTClient()
        app.state.stt_client = stt_client
        if not hasattr(app.state, "wecom_client"):
            if s.wecom_enabled and s.wecom_corp_id and s.wecom_secret:
                app.state.wecom_client = WecomClient(
                    corp_id=s.wecom_corp_id,
                    secret=s.wecom_secret,
                    agent_id=s.wecom_agent_id,
                )
            else:
                app.state.wecom_client = _NoopWecomClient()
        async with db_lifespan(app):
            pricing = Pricing.load(s.pricing_file_path)
            cost_sink = CostEventSink(
                sessionmaker=app.state.db_sessionmaker,
                pricing=pricing,
                enabled=s.cost_tracking_enabled,
            )
            app.state.event_bus.subscribe_callback(cost_sink.handle)
            app.state.cost_sink = cost_sink
            audit_sink = AuditEventSink(sessionmaker=app.state.db_sessionmaker)
            app.state.event_bus.subscribe_callback(audit_sink.handle)
            app.state.audit_sink = audit_sink
            notifier = WecomNotifier(
                client=app.state.wecom_client,
                sessionmaker=app.state.db_sessionmaker,
                public_base_url=s.public_base_url,
            )
            app.state.event_bus.subscribe_callback(notifier.handle)
            app.state.wecom_notifier = notifier
            resolver = DependencyResolver(
                sessionmaker=app.state.db_sessionmaker,
                dispatcher=app.state.dispatcher,
                bus=app.state.event_bus,
            )
            app.state.event_bus.subscribe_callback(resolver.handle)
            app.state.deps_resolver = resolver
            ingestor = MemoryIngestor(
                sessionmaker=app.state.db_sessionmaker,
                artifact_store=app.state.artifact_store,
                embedding_client=app.state.embedding_client,
                enabled=s.memory_enabled,
            )
            app.state.event_bus.subscribe_callback(ingestor.handle)
            app.state.memory_ingestor = ingestor
            metrics_task: asyncio.Task[None] | None = None
            if s.metrics_enabled:
                metrics_task = asyncio.create_task(
                    run_metrics_refresher(app.state.db_sessionmaker)
                )
            try:
                yield
            finally:
                if metrics_task is not None:
                    metrics_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await metrics_task
                if flow_store_gc_task is not None:
                    flow_store_gc_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await flow_store_gc_task
                await app.state.wecom_client.aclose()

    app = FastAPI(title="Taskdeck Core", lifespan=lifespan)

    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(tasks_router)
    app.include_router(workspaces_router)
    app.include_router(members_router)
    app.include_router(costs_router)
    app.include_router(audit_router)
    app.include_router(memory_router)
    app.include_router(internal_router)
    app.include_router(intent_router)
    app.include_router(sandbox_router)
    app.include_router(runners_router)
    app.include_router(attachments_router)
    app.include_router(stt_router)
    app.include_router(crp_router(hub, dispatcher, bus=bus))
    app.include_router(ws_router(bus))
    app.include_router(wecom_router)
    app.include_router(im_identity_router)
    app.include_router(auth_router)
    app.include_router(bootstrap_router)

    # Hardening: body size limit
    app.add_middleware(
        BodySizeMiddleware,
        max_bytes=s.request_max_body_bytes,
        # /api/v1/attachments has its own per-file cap (attachment_max_bytes,
        # default 50 MB) and streams the multipart body without buffering.
        exempt_prefixes=("/api/v1/attachments",),
    )

    # Hardening: rate limit via slowapi
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Prometheus metrics
    if s.metrics_enabled:
        setup_metrics(app)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("taskdeck_core.main:app", host="0.0.0.0", port=8000, reload=True)
