from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from easylearn.cache import DocumentCache
from easylearn.config import Settings
from easylearn.database import Database
from easylearn.documents.schema import DocumentView, FavoriteRequest
from easylearn.documents.service import DocumentService
from easylearn.errors import DomainError, ErrorView
from easylearn.exports import ExportRequest, ExportService
from easylearn.files import DocumentFiles, InstanceLock
from easylearn.jobs.schema import JobKind, TaskView
from easylearn.logging_setup import LoggingController, configure_logging
from easylearn.maintenance import MaintenanceService
from easylearn.mineru.models import MinerUModelView
from easylearn.parser import ParseRequest, ParseService
from easylearn.paths import DataPaths
from easylearn.qa import QARecordView, QARequest, QAService
from easylearn.source_edits import SourceEditRequest, SourceEditService, SourceEditView
from easylearn.tasks import TaskManager
from easylearn.translation import (
    EditTranslationRequest,
    LLMClient,
    RestoreTranslationRequest,
    TranslateRequest,
    TranslationHistoryView,
    TranslationService,
    TranslationUnitView,
)

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


@dataclass(frozen=True)
class ApplicationState:
    settings: Settings
    paths: DataPaths
    database: Database
    files: DocumentFiles
    documents: DocumentService
    parser: ParseService
    translation: TranslationService
    exporter: ExportService
    qa: QAService
    source_edits: SourceEditService
    http: httpx.AsyncClient
    tasks: TaskManager
    cache: DocumentCache
    maintenance: MaintenanceService


def _state(request: Request) -> ApplicationState:
    return cast(ApplicationState, request.app.state.services)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    logger = logging.getLogger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        paths = DataPaths(settings.data_dir, settings.tmp_dir).ensure()
        instance_lock = InstanceLock(paths.lock)
        instance_lock.acquire()
        logging_controller: LoggingController = configure_logging(
            settings.log_dir,
        )
        database = Database(paths.database)
        manager = TaskManager(
            queue_limit=settings.tasks.queue_limit,
            concurrency={
                JobKind.PARSE: settings.tasks.parse_concurrency,
                JobKind.TRANSLATE: settings.tasks.translation_requests,
                JobKind.QA: settings.tasks.qa_requests,
                JobKind.EXPORT: 1,
            },
            retention_seconds=settings.tasks.finished_task_retention_minutes * 60,
        )
        http: httpx.AsyncClient | None = None
        parser: ParseService | None = None
        try:
            await database.open()
            maintenance = MaintenanceService(
                database,
                paths,
                tmp_retention_seconds=settings.files.tmp_retention_hours * 60 * 60,
                export_retention_seconds=settings.files.export_retention_days * 24 * 60 * 60,
                parse_keep=settings.files.keep_parse_versions,
            )
            await maintenance.cleanup()
            cache = DocumentCache(
                max_documents=settings.cache.max_documents,
                max_bytes=settings.cache.max_mb * 1024 * 1024,
            )
            files = DocumentFiles(paths)
            documents = DocumentService(
                database,
                files,
                max_upload_bytes=settings.max_upload_bytes,
                cache=cache,
            )
            proxy = settings.llm.resolved_proxy
            http = httpx.AsyncClient(
                trust_env=not settings.llm.local_only and not proxy,
                proxy=proxy,
                follow_redirects=False,
            )
            parser = ParseService(database, files, documents, manager, settings)
            source_edits = SourceEditService(database, documents)
            translation = TranslationService(
                database, documents, manager, settings, llm=LLMClient(settings, http)
            )
            exporter = ExportService(database, documents, manager)
            qa = QAService(database, documents, manager, settings, llm=LLMClient(settings, http))
            manager.register(JobKind.PARSE, parser.execute)
            manager.register(JobKind.TRANSLATE, translation.execute)
            manager.register(JobKind.EXPORT, exporter.execute)
            manager.register(JobKind.QA, qa.execute)
            await manager.start()
            app.state.services = ApplicationState(
                settings=settings,
                paths=paths,
                database=database,
                files=files,
                documents=documents,
                parser=parser,
                translation=translation,
                exporter=exporter,
                qa=qa,
                source_edits=source_edits,
                http=http,
                tasks=manager,
                cache=cache,
                maintenance=maintenance,
            )
            logger.info("EasyLearn started at %s:%s", settings.app.host, settings.app.port)
            yield
        finally:
            await manager.close()
            if parser is not None:
                await parser.close()
            if http is not None:
                await http.aclose()
            await database.close()
            logger.info("EasyLearn stopped")
            logging_controller.close()
            instance_lock.release()

    app = FastAPI(title="EasyLearn", version="0.1.0", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).with_name("static"))),
        name="static",
    )

    @app.middleware("http")
    async def request_identity(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=ErrorView(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                request_id=getattr(request.state, "request_id", ""),
            ).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorView(
                code="REQUEST_INVALID",
                message="Request does not satisfy the contract",
                request_id=getattr(request.state, "request_id", ""),
                details={
                    "issues": [
                        {"location": list(issue["loc"]), "type": issue["type"]}
                        for issue in exc.errors()
                    ]
                },
            ).model_dump(mode="json"),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=ErrorView(
                code=f"HTTP_{exc.status_code}",
                message="HTTP request could not be served",
                request_id=getattr(request.state, "request_id", ""),
            ).model_dump(mode="json"),
        )

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, object]:
        state = _state(request)
        database = state.database
        async with database.read() as connection:
            cursor = await connection.execute("SELECT 1")
            await cursor.fetchone()
            await cursor.close()
        settings = state.settings
        manager = state.tasks
        return {
            "status": "ready",
            "server_boot_id": str(manager.server_boot_id),
            "mineru": {
                "mode": "embedded",
                "backend": "transformers",
                "configured": _state(request).parser.mineru.configured,
                "models": len(_state(request).parser.model_catalog.list()),
            },
            "llm": {
                "configured": bool(
                    settings.llm.model and settings.llm.base_url and settings.llm_api_key
                ),
                "active_provider": settings.llm.active_provider,
                "model": settings.llm.model,
                "base_url": settings.llm.base_url,
                "has_api_key": bool(settings.llm_api_key),
            },
            "extensions": settings.extensions.model_dump(mode="json"),
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        static_dir = Path(__file__).with_name("static")
        version = str(int((static_dir / "app.js").stat().st_mtime))
        return templates.TemplateResponse(
            request, "index.html", {"title": "EasyLearn", "version": version}
        )

    @app.get("/api/documents", response_model=tuple[DocumentView, ...])
    async def list_documents(
        request: Request, favorite: bool | None = Query(default=None)
    ) -> tuple[DocumentView, ...]:
        state = _state(request)
        documents = await state.documents.list(favorite=favorite)
        items: list[DocumentView] = []
        for doc in documents:
            tasks = await state.tasks.active_for(doc.document_id)
            items.append(doc.model_copy(update={"tasks": tasks}))
        return tuple(items)

    @app.get("/api/mineru/models", response_model=tuple[MinerUModelView, ...])
    async def list_mineru_models(request: Request) -> tuple[MinerUModelView, ...]:
        return _state(request).parser.model_catalog.list()

    @app.post("/api/documents", response_model=DocumentView, status_code=201)
    async def create_document(
        request: Request, file: Annotated[UploadFile, File()]
    ) -> DocumentView:
        return await _state(request).documents.create(file)

    @app.get("/api/documents/{document_id}", response_model=DocumentView)
    async def get_document(document_id: UUID, request: Request) -> DocumentView:
        state = _state(request)
        document = await state.documents.get(document_id)
        tasks = await state.tasks.active_for(document_id)
        return document.model_copy(update={"tasks": tasks})

    @app.patch("/api/documents/{document_id}/favorite", response_model=DocumentView)
    async def favorite_document(
        document_id: UUID, body: FavoriteRequest, request: Request
    ) -> DocumentView:
        return await _state(request).documents.set_favorite(document_id, body.favorite)

    @app.delete("/api/documents/{document_id}", status_code=204)
    async def delete_document(document_id: UUID, request: Request) -> Response:
        state = _state(request)
        await state.tasks.begin_document_deletion(document_id)
        try:
            await state.documents.delete(document_id)
            return Response(status_code=204)
        finally:
            await state.tasks.end_document_deletion(document_id)

    @app.post("/api/documents/{document_id}/parse", response_model=TaskView, status_code=202)
    async def parse_document(
        document_id: UUID, request: Request, body: ParseRequest | None = None
    ) -> TaskView:
        return await _state(request).parser.submit(document_id, body)

    @app.get("/api/documents/{document_id}/parses/{parse_id}")
    async def get_parse(
        document_id: UUID,
        parse_id: UUID,
        request: Request,
        page: int | None = Query(default=None, ge=0),
        block_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        ir = await _state(request).source_edits.effective_ir(document_id, parse_id)
        payload = ir.model_dump(mode="json", exclude_computed_fields=True)
        if page is not None:
            payload["blocks"] = [
                block
                for block in payload["blocks"]
                if any(region["page_index"] == page for region in block["source_regions"])
                or page in (block.get("source_locator") or {}).get("page_indices", [])
            ]
        if block_id is not None:
            selected = {block_id}
            changed = True
            while changed:
                changed = False
                for block in payload["blocks"]:
                    if (
                        block.get("parent_block_id") in selected
                        and block["block_id"] not in selected
                    ):
                        selected.add(block["block_id"])
                        changed = True
            payload["blocks"] = [
                block for block in payload["blocks"] if block["block_id"] in selected
            ]
        return payload

    @app.get(
        "/api/documents/{document_id}/parses/{parse_id}/markdown",
        response_class=PlainTextResponse,
    )
    async def get_markdown(
        document_id: UUID,
        parse_id: UUID,
        request: Request,
        language: Literal["source", "zh", "bilingual", "raw"] = Query(default="source"),
    ) -> PlainTextResponse:
        from easylearn.rendering import render_markdown

        state = _state(request)
        if language == "raw":
            return PlainTextResponse(
                await state.documents.load_raw_markdown(document_id, parse_id),
                media_type="text/markdown; charset=utf-8",
            )
        ir = await state.source_edits.effective_ir(document_id, parse_id)
        translations = await state.translation.effective_map(document_id, parse_id)
        asset_links = {
            str(asset.asset_id): (
                f"/api/documents/{document_id}/files/asset:{parse_id}:{asset.asset_id}"
            )
            for asset in ir.assets
        }
        if language == "source":
            content = render_markdown(ir, language="source", asset_links=asset_links)
        elif language == "zh":
            content = render_markdown(
                ir, translations, language="chinese", asset_links=asset_links
            )
        else:
            content = render_markdown(ir, translations, bilingual=True, asset_links=asset_links)
        return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")

    @app.get(
        "/api/documents/{document_id}/source-edits",
        response_model=tuple[SourceEditView, ...],
    )
    async def list_source_edits(
        document_id: UUID,
        request: Request,
        parse_id: Annotated[UUID, Query(...)],
    ) -> tuple[SourceEditView, ...]:
        return await _state(request).source_edits.list(document_id, parse_id)

    @app.patch(
        "/api/documents/{document_id}/source-edits",
        response_model=SourceEditView,
    )
    async def edit_source(
        document_id: UUID,
        body: SourceEditRequest,
        request: Request,
    ) -> SourceEditView:
        return await _state(request).source_edits.edit(document_id, body)

    @app.post("/api/documents/{document_id}/translate", response_model=TaskView, status_code=202)
    async def translate_document(
        document_id: UUID, body: TranslateRequest, request: Request
    ) -> TaskView:
        return await _state(request).translation.submit(document_id, body)

    @app.get(
        "/api/documents/{document_id}/translations",
        response_model=tuple[TranslationUnitView, ...],
    )
    async def list_translations(
        document_id: UUID,
        request: Request,
        parse_id: Annotated[UUID, Query(...)],
    ) -> tuple[TranslationUnitView, ...]:
        return await _state(request).translation.list(document_id, parse_id)

    @app.patch(
        "/api/documents/{document_id}/translations/{unit_id}",
        response_model=TranslationUnitView,
    )
    async def edit_translation(
        document_id: UUID,
        unit_id: str,
        body: EditTranslationRequest,
        request: Request,
    ) -> TranslationUnitView:
        return await _state(request).translation.edit(document_id, unit_id, body)

    @app.get(
        "/api/documents/{document_id}/translations/{unit_id}/history",
        response_model=tuple[TranslationHistoryView, ...],
    )
    async def translation_history(
        document_id: UUID,
        unit_id: str,
        request: Request,
        parse_id: Annotated[UUID, Query(...)],
    ) -> tuple[TranslationHistoryView, ...]:
        return await _state(request).translation.history(document_id, parse_id, unit_id)

    @app.post(
        "/api/documents/{document_id}/translations/{unit_id}/history/{history_id}/restore",
        response_model=TranslationUnitView,
    )
    async def restore_translation(
        document_id: UUID,
        unit_id: str,
        history_id: UUID,
        body: RestoreTranslationRequest,
        request: Request,
    ) -> TranslationUnitView:
        return await _state(request).translation.restore(document_id, unit_id, history_id, body)

    @app.post("/api/documents/{document_id}/exports", response_model=TaskView, status_code=202)
    async def export_document(document_id: UUID, body: ExportRequest, request: Request) -> TaskView:
        return await _state(request).exporter.submit(document_id, body)

    @app.post("/api/documents/{document_id}/qa", response_model=TaskView, status_code=202)
    async def ask_question(document_id: UUID, body: QARequest, request: Request) -> TaskView:
        return await _state(request).qa.submit(document_id, body)

    @app.get("/api/documents/{document_id}/qa", response_model=tuple[QARecordView, ...])
    async def list_questions(document_id: UUID, request: Request) -> tuple[QARecordView, ...]:
        return await _state(request).qa.list(document_id)

    @app.delete("/api/documents/{document_id}/qa/{qa_id}", status_code=204)
    async def delete_question(document_id: UUID, qa_id: UUID, request: Request) -> Response:
        await _state(request).qa.delete(document_id, qa_id)
        return Response(status_code=204)

    @app.get("/api/tasks/{task_id}", response_model=TaskView)
    async def get_task(task_id: UUID, request: Request) -> TaskView:
        return await _state(request).tasks.get(task_id)

    @app.post("/api/tasks/{task_id}/cancel", response_model=TaskView)
    async def cancel_task(task_id: UUID, request: Request) -> TaskView:
        return await _state(request).tasks.cancel(task_id)

    @app.get("/api/tasks/{task_id}/answer-stream")
    async def answer_stream(task_id: UUID, request: Request) -> StreamingResponse:
        manager = _state(request).tasks

        async def events() -> AsyncIterator[str]:
            async def cancel_if_active() -> None:
                try:
                    view = await manager.get(task_id)
                    if not view.status.terminal:
                        await manager.cancel(task_id)
                except DomainError:
                    return

            try:
                async for chunk, view in manager.answer_stream(task_id):
                    if chunk:
                        yield "data: " + json.dumps({"delta": chunk}, ensure_ascii=False) + "\n\n"
                    if view.status.terminal:
                        yield "event: done\n"
                        done = json.dumps(view.model_dump(mode="json"), ensure_ascii=False)
                        yield ("data: " + done + "\n\n")
            finally:
                cleanup = asyncio.create_task(cancel_if_active())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                await cleanup

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/documents/{document_id}/files/{file_id:path}")
    async def get_file(document_id: UUID, file_id: str, request: Request) -> FileResponse:
        path = await _state(request).documents.file_path(document_id, file_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    return app
