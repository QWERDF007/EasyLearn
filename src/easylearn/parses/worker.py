import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from easylearn.assets import Asset
from easylearn.config import Settings
from easylearn.documents.models import Document
from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.jobs.models import JobRun
from easylearn.jobs.schema import JobKind
from easylearn.jobs.service import JobLease, JobService
from easylearn.mineru.client import MinerUClient, SubmissionRejected, SubmissionUnknown
from easylearn.mineru.result import MinerUResultValidator, ParseSource
from easylearn.parses.models import ParseArtifact, ParseRun
from easylearn.parses.schema import ParseCheckpoint, ParseConfiguration, SubmissionAttempt
from easylearn.storage import LocalStorage, StoredObject


class ParseWorker:
    """Execute a fixed run; the process owns the HTTP pool and database lifecycle."""

    def __init__(
        self, jobs: JobService, storage: LocalStorage, settings: Settings, http: httpx.AsyncClient
    ) -> None:
        self.jobs = jobs
        self.storage = storage
        self.settings = settings
        self.http = http

    @classmethod
    @asynccontextmanager
    async def open(
        cls, jobs: JobService, storage: LocalStorage, settings: Settings
    ) -> AsyncIterator["ParseWorker"]:
        profile = settings.mineru
        if profile is None:
            raise DomainError("MINERU_NOT_CONFIGURED", "Configure a MinerU service", retryable=True)
        headers = (
            {"Authorization": f"Bearer {profile.api_key.get_secret_value()}"}
            if profile.api_key
            else {}
        )
        async with httpx.AsyncClient(
            base_url=str(profile.base_url),
            headers=headers,
            trust_env=False,
            follow_redirects=False,
        ) as http:
            yield cls(jobs, storage, settings, http)

    async def execute(self, job_id: UUID, *, generation: int) -> None:
        view = await self.jobs.get(job_id)
        if view.run_ref.kind != JobKind.PARSE:
            raise DomainError("JOB_KIND_INVALID", "Expected a parse job", status=409)
        lease = await self.jobs.acquire(job_id, generation=generation, owner=uuid4())
        if lease is None:
            return
        try:
            await self.jobs.supervise(lease, self._run(lease))
        except DomainError as exc:
            await self.jobs.handle_error(lease, exc)
        except OSError:
            await self.jobs.handle_error(
                lease,
                DomainError(
                    "PARSE_IO_FAILED", "Parse assets could not be accessed", retryable=True
                ),
            )

    async def _run(self, lease: JobLease) -> None:
        async with self.jobs.database.sessions() as session:
            run, asset = (
                await session.execute(
                    select(ParseRun, Asset)
                    .join(Asset, ParseRun.preview_asset_id == Asset.id)
                    .where(ParseRun.job_id == lease.job_id)
                )
            ).one()
        configuration = ParseConfiguration.model_validate(run.configuration)
        checkpoint = ParseCheckpoint.model_validate((await self.jobs.get(lease.job_id)).checkpoint)
        previous = checkpoint.submissions[-1] if checkpoint.submissions else None
        if previous is not None and previous.state in ("SUBMITTING", "SUBMIT_UNKNOWN"):
            previous.state = "SUBMIT_UNKNOWN"
            await self.jobs.save_checkpoint(
                lease, stage="SUBMIT_UNKNOWN", data=checkpoint.model_dump(mode="json")
            )
            raise SubmissionUnknown(previous.request_id)
        profile = self.settings.mineru
        if profile is None:
            raise DomainError("MINERU_NOT_CONFIGURED", "Configure a MinerU service", retryable=True)
        local_model = (
            self.settings.local_models[profile.local_model] if profile.local_model else None
        )
        if (
            str(self.http.base_url).rstrip("/") != str(configuration.service_url).rstrip("/")
            or profile.base_url != configuration.service_url
            or profile.profile_revision != configuration.profile_revision
            or local_model != configuration.local_model
        ):
            raise DomainError(
                "MINERU_PROFILE_CHANGED",
                "Restore the fixed service profile to resume this run",
                retryable=True,
            )
        mineru = MinerUClient(self.http, limits=profile.limits)
        if previous is not None and previous.state == "ACCEPTED":
            attempt = previous
        else:
            await mineru.health()
            with self.storage.path(asset.storage_key).open("rb") as source:
                digest = await run_blocking(hashlib.file_digest, source, "sha256")
                if digest.hexdigest() != asset.sha256 or source.tell() != asset.size:
                    raise DomainError("STORAGE_CORRUPT", "Fixed preview content has changed")
                source.seek(0)
                attempt = SubmissionAttempt(
                    request_id=uuid4(),
                    generation=lease.generation,
                    source_sha256=asset.sha256,
                    configuration_sha256=hashlib.sha256(
                        json.dumps(
                            configuration.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                )
                checkpoint.submissions.append(attempt)
                await self.jobs.save_checkpoint(
                    lease, stage="SUBMITTING", data=checkpoint.model_dump(mode="json")
                )
                try:
                    captured = await mineru.submit(
                        source, request_id=attempt.request_id, options=configuration.options
                    )
                except SubmissionUnknown as exc:
                    attempt.state = "SUBMIT_UNKNOWN"
                    attempt.response = await run_blocking(self.storage.write, [exc.raw_body])
                    await self.jobs.save_checkpoint(
                        lease, stage="SUBMIT_UNKNOWN", data=checkpoint.model_dump(mode="json")
                    )
                    raise
                except DomainError as exc:
                    attempt.state = "REJECTED"
                    if isinstance(exc, SubmissionRejected):
                        attempt.response = await run_blocking(self.storage.write, [exc.raw_body])
                    await self.jobs.save_checkpoint(
                        lease, stage="SUBMITTING", data=checkpoint.model_dump(mode="json")
                    )
                    raise
            attempt.state = "ACCEPTED"
            attempt.task = captured.value
            attempt.response = await run_blocking(self.storage.write, [captured.raw_body])
            await self.jobs.save_checkpoint(
                lease, stage="RUNNING", data=checkpoint.model_dump(mode="json")
            )
        task = checkpoint.task or attempt.task
        assert task is not None
        if task.backend != configuration.options.backend or task.file_names != ("input",):
            raise DomainError("MINERU_PROTOCOL_MISMATCH", "Task input identity changed")
        remaining = (
            profile.task_timeout_seconds - (datetime.now(UTC) - attempt.created_at).total_seconds()
        )
        try:
            async with asyncio.timeout(max(0, remaining)):
                while task.status not in ("completed", "failed"):
                    await self.jobs.heartbeat(lease)
                    captured = await mineru.query(task.task_id)
                    task = checkpoint.task = captured.value
                    checkpoint.task_response = await run_blocking(
                        self.storage.write, [captured.raw_body]
                    )
                    await self.jobs.save_checkpoint(
                        lease, stage="RUNNING", data=checkpoint.model_dump(mode="json")
                    )
                    if task.backend != configuration.options.backend or task.file_names != (
                        "input",
                    ):
                        raise DomainError("MINERU_PROTOCOL_MISMATCH", "Task input identity changed")
                    if task.status not in ("completed", "failed"):
                        await asyncio.sleep(profile.poll_interval_seconds)
        except TimeoutError:
            raise DomainError(
                "MINERU_TASK_TIMEOUT", "MinerU task exceeded its time budget", retryable=True
            ) from None
        if task.status == "failed":
            raise DomainError("MINERU_TASK_FAILED", "MinerU reported parsing failure")
        if checkpoint.archive is None:
            await self.jobs.save_checkpoint(
                lease, stage="DOWNLOADING", data=checkpoint.model_dump(mode="json")
            )
            async with mineru.download(task.task_id) as chunks:
                checkpoint.archive = await self.storage.write_stream(chunks)
        await self.jobs.save_checkpoint(
            lease, stage="NORMALIZING", data=checkpoint.model_dump(mode="json")
        )
        evidence = await MinerUResultValidator(
            self.storage,
            archive_limits=profile.archive_limits,
            image_limits=self.settings.image_limits,
            preview_limits=self.settings.preview_limits,
            table_limits=profile.table_limits,
            timeout=profile.validation_timeout_seconds,
        ).normalize(
            checkpoint.archive,
            options=configuration.options,
            source=ParseSource(
                document_id=run.document_id,
                parse_run_id=run.id,
                preview_asset_id=run.preview_asset_id,
                preview=StoredObject(sha256=asset.sha256, size=asset.size, key=asset.storage_key),
            ),
        )
        objects = [
            (evidence.document_ir, "application/json"),
            (checkpoint.archive, "application/zip"),
        ]
        for member in evidence.manifest.members:
            mime = (
                member.image.mime
                if member.image
                else "application/pdf"
                if member.kind == "original"
                else "text/markdown"
                if member.kind == "markdown"
                else "application/json"
            )
            objects.append((evidence.objects[member.path], mime))
        async with self.jobs.publication(lease) as session:
            registered = await Asset.register_many(session, objects)
            published = await session.get(ParseRun, run.id)
            assert published is not None
            published.document_ir_asset_id = registered[evidence.document_ir.sha256].id
            published.archive_asset_id = registered[checkpoint.archive.sha256].id
            published.evidence = evidence.model_dump(mode="json")
            for member in evidence.manifest.members:
                session.add(
                    ParseArtifact(
                        id=member.asset_id(run.id),
                        parse_run_id=run.id,
                        asset_id=registered[member.sha256].id,
                        path=member.path,
                        kind=member.kind,
                    )
                )
            document = await session.scalar(
                select(Document).where(Document.id == run.document_id).with_for_update()
            )
            assert document is not None
            current = await session.scalar(
                select(JobRun)
                .join(ParseRun, ParseRun.job_id == JobRun.id)
                .where(ParseRun.id == document.active_parse_run_id)
            )
            candidate = await session.get(JobRun, lease.job_id)
            assert candidate is not None
            if current is None or (candidate.created_at, candidate.id) > (
                current.created_at,
                current.id,
            ):
                document.active_parse_run_id = run.id
