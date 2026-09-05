"""Synthetic MinerU HTTP protocol peer: transport tests only, no inference or MinerU import."""

import hashlib
import json
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZIP_STORED, ZipFile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile

app = FastAPI()
results: dict[UUID, tuple[dict, bytes]] = {}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "version": "3.4.5",
        "protocol_version": 2,
        "task_retention_seconds": 86400,
    }


@app.post("/tasks", status_code=202)
async def submit(request: Request):
    async with request.form() as form:
        files = form.getlist("files")
        if len(files) != 1 or not isinstance(files[0], UploadFile):
            raise HTTPException(422)
        source = files[0]
        content = await source.read()
        task_id = uuid4()
        receipt = {
            "task_id": str(task_id),
            "status": "completed",
            "backend": str(form["backend"]),
            "file_names": ["input"],
            "created_at": "2026-09-05T00:00:00Z",
            "started_at": "2026-09-05T00:00:00Z",
            "completed_at": "2026-09-05T00:00:01Z",
            "error": None,
        }
        evidence = {
            "filename": source.filename,
            "content_type": source.content_type,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "request_id": request.headers.get("X-Request-ID"),
            "options": {name: str(value) for name, value in form.multi_items() if name != "files"},
        }
        archive = BytesIO()
        with ZipFile(archive, "w", compression=ZIP_STORED) as package:
            package.writestr("transport-test/input.pdf", content)
            package.writestr("transport-test/received.json", json.dumps(evidence))
        results[task_id] = receipt, archive.getvalue()
    return JSONResponse(status_code=202, content=receipt)


@app.get("/tasks/{task_id}")
async def query(task_id: UUID):
    if task_id not in results:
        raise HTTPException(404)
    return results[task_id][0]


@app.get("/tasks/{task_id}/result")
async def download(task_id: UUID):
    if task_id not in results:
        raise HTTPException(404)
    content = results[task_id][1]

    async def chunks():
        for offset in range(0, len(content), 16384):
            yield content[offset : offset + 16384]

    return StreamingResponse(chunks(), media_type="application/zip")
