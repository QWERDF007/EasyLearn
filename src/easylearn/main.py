import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint

from easylearn.config import Settings
from easylearn.database import Database
from easylearn.errors import DomainError, ErrorView
from easylearn.storage import LocalStorage
from easylearn.uploads.schema import UploadCreatedView, UploadLimits, UploadRequest, UploadView
from easylearn.uploads.service import UploadService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.database_url.get_secret_value())
        storage = LocalStorage(settings.storage_root)
        app.state.database = database
        app.state.storage = storage
        app.state.uploads = UploadService(database, storage, settings)
        try:
            yield
        finally:
            await database.close()

    app = FastAPI(title="EasyLearn", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_identity(request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        database: Database = request.app.state.database
        storage: LocalStorage = request.app.state.storage
        db_ok = storage_ok = False
        try:
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            db_ok = True
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        try:
            await asyncio.to_thread(storage.check)
            storage_ok = True
        except OSError:
            pass
        ok = db_ok and storage_ok
        return JSONResponse(
            status_code=200 if ok else 503,
            content={
                "status": "ready" if ok else "unavailable",
                "database": db_ok,
                "storage": storage_ok,
            },
        )

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=ErrorView(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                request_id=request.state.request_id,
            ).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorView(
                code="REQUEST_INVALID",
                message="Request does not satisfy the contract",
                request_id=request.state.request_id,
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
                request_id=request.state.request_id,
            ).model_dump(mode="json"),
        )

    @app.post("/api/v1/uploads", status_code=201, response_model=UploadCreatedView)
    async def create_upload(body: UploadRequest, request: Request) -> UploadCreatedView:
        uploads: UploadService = request.app.state.uploads
        upload = await uploads.create(body)
        return UploadCreatedView.model_validate(
            {
                **upload.model_dump(),
                "limits": UploadLimits(
                    max_file_bytes=settings.upload_max_bytes,
                    max_chunk_bytes=settings.upload_chunk_bytes,
                ),
            }
        )

    @app.get("/api/v1/uploads/{upload_id}", response_model=UploadView)
    async def get_upload(upload_id: UUID, request: Request) -> UploadView:
        uploads: UploadService = request.app.state.uploads
        return await uploads.get(upload_id)

    @app.patch("/api/v1/uploads/{upload_id}/content", status_code=204)
    async def append_upload(
        upload_id: UUID, request: Request, upload_offset: Annotated[int, Header(ge=0)]
    ) -> Response:
        content = bytearray()
        async for part in request.stream():
            content.extend(part)
            if len(content) > settings.upload_chunk_bytes:
                raise DomainError("UPLOAD_CHUNK_INVALID", "Chunk exceeds limit", status=413)
        uploads: UploadService = request.app.state.uploads
        offset = await uploads.append(upload_id, upload_offset, bytes(content))
        return Response(status_code=204, headers={"Upload-Offset": str(offset)})

    @app.post("/api/v1/uploads/{upload_id}/complete", response_model=UploadView)
    async def complete_upload(upload_id: UUID, request: Request) -> UploadView:
        uploads: UploadService = request.app.state.uploads
        return await uploads.complete(upload_id)

    return app
