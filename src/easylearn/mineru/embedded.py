"""In-process MinerU VLM runtime used by the application parser."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from loguru import logger

from easylearn.errors import DomainError
from easylearn.execution import run_blocking
from easylearn.mineru.archive import result_root
from easylearn.mineru.schema import MinerUOptions

type Analyzer = Callable[..., Awaitable[tuple[dict[str, Any], Any]]]


class EmbeddedMinerU:
    """Own the vendored MinerU runtime and its process-wide model lifecycle."""

    def __init__(
        self,
        model_path: Path,
        *,
        timeout: float = 900,
        analyzer: Analyzer | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("MinerU timeout must be finite and positive")
        self.model_path = model_path
        self.timeout = timeout
        self._analyzer = analyzer
        self._lock = asyncio.Lock()
        self._runtime_loaded = False

    @property
    def configured(self) -> bool:
        return self.model_path.is_dir() and (mineru_source_root() / "mineru").is_dir()

    async def parse(
        self,
        input_pdf: Path,
        output_directory: Path,
        options: MinerUOptions,
        check: Callable[[], Awaitable[None]] | None = None,
        model_path: Path | None = None,
    ) -> None:
        selected_model_path = model_path or self.model_path
        if not selected_model_path.is_dir():
            raise DomainError(
                "MINERU_MODEL_UNAVAILABLE",
                f"MinerU model directory does not exist: {selected_model_path}",
            )
        if options.backend != "vlm-engine":
            raise DomainError(
                "MINERU_BACKEND_UNSUPPORTED",
                "Embedded MinerU currently supports the vlm-engine backend",
            )
        if check is not None:
            await check()

        async with self._lock:
            if check is not None:
                await check()
            analyzer = self._analyzer or load_analyzer()
            if self._analyzer is None:
                self._runtime_loaded = True
            pdf_bytes = await run_blocking(input_pdf.read_bytes)
            result_directory = output_directory / result_root(options)
            image_directory = result_directory / "images"
            image_directory.mkdir(parents=True, exist_ok=True)

            try:
                with mineru_output_flags(options):
                    middle, model_output = await self._run_analyzer(
                        analyzer,
                        pdf_bytes,
                        image_directory,
                        options,
                        selected_model_path,
                    )
                await run_blocking(
                    write_result_tree,
                    result_directory,
                    input_pdf,
                    middle,
                    model_output,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise DomainError(
                    "MINERU_TIMEOUT",
                    "MinerU inference exceeded the task time limit",
                    retryable=True,
                ) from None
            except (ImportError, ModuleNotFoundError, OSError) as exc:
                logger.exception("Embedded MinerU could not be loaded")
                raise DomainError(
                    "MINERU_UNAVAILABLE",
                    f"Embedded MinerU could not be loaded: {type(exc).__name__}",
                    retryable=True,
                ) from exc
            except Exception as exc:
                logger.exception("Embedded MinerU inference failed")
                raise DomainError(
                    "MINERU_FAILED",
                    f"Embedded MinerU inference failed: {type(exc).__name__}",
                    retryable=True,
                ) from exc
            if check is not None:
                await check()

    async def _run_analyzer(
        self,
        analyzer: Analyzer,
        pdf_bytes: bytes,
        image_directory: Path,
        options: MinerUOptions,
        model_path: Path,
    ) -> tuple[dict[str, Any], Any]:
        _ensure_mineru_source_on_path()
        from mineru.data.data_reader_writer import (  # type: ignore[import-not-found]
            FileBasedDataWriter,
        )

        image_writer = FileBasedDataWriter(str(image_directory))
        return await asyncio.wait_for(
            analyzer(
                pdf_bytes,
                image_writer=image_writer,
                backend="transformers",
                model_path=str(model_path),
                image_analysis=options.image_analysis,
            ),
            timeout=self.timeout,
        )

    async def close(self) -> None:
        async with self._lock:
            if not self._runtime_loaded:
                return
            await run_blocking(shutdown_mineru_runtime)
            self._runtime_loaded = False


def mineru_source_root() -> Path:
    return Path(__file__).resolve().parents[3] / "3rdparty" / "MinerU"


def _ensure_mineru_source_on_path() -> None:
    source = str(mineru_source_root())
    if source not in sys.path:
        sys.path.insert(0, source)


def load_analyzer() -> Analyzer:
    _ensure_mineru_source_on_path()
    from mineru.backend.vlm.vlm_analyze import (  # type: ignore[import-not-found]
        aio_doc_analyze,
    )

    return cast(Analyzer, aio_doc_analyze)


def shutdown_mineru_runtime() -> None:
    _ensure_mineru_source_on_path()
    from mineru.backend.vlm.vlm_analyze import (
        shutdown_cached_models,
    )
    from mineru.utils.pdf_image_tools import (  # type: ignore[import-not-found]
        shutdown_pdf_render_executor,
    )

    shutdown_cached_models()
    shutdown_pdf_render_executor()


@contextmanager
def mineru_output_flags(options: MinerUOptions) -> Iterator[None]:
    values = {
        "MINERU_VLM_FORMULA_ENABLE": str(options.formula_enable).lower(),
        "MINERU_VLM_TABLE_ENABLE": str(options.table_enable).lower(),
        "TORCH_CUDNN_V8_API_DISABLED": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_result_tree(
    result_directory: Path,
    input_pdf: Path,
    middle: dict[str, Any],
    model_output: Any,
) -> None:
    _ensure_mineru_source_on_path()
    from mineru.backend.vlm.vlm_middle_json_mkcontent import (  # type: ignore[import-not-found]
        union_make,
    )
    from mineru.utils.enum_class import MakeMode  # type: ignore[import-not-found]

    pdf_info = middle.get("pdf_info")
    if not isinstance(pdf_info, list):
        raise ValueError("MinerU middle result does not contain pdf_info")
    result_directory.mkdir(parents=True, exist_ok=True)
    image_directory = result_directory / "images"
    image_directory.mkdir(parents=True, exist_ok=True)
    (result_directory / "input_origin.pdf").write_bytes(input_pdf.read_bytes())
    (result_directory / "input_middle.json").write_text(
        json.dumps(middle, ensure_ascii=False, indent=4, allow_nan=False),
        encoding="utf-8",
    )
    (result_directory / "input_model.json").write_text(
        json.dumps(model_output, ensure_ascii=False, indent=4, allow_nan=False),
        encoding="utf-8",
    )
    (result_directory / "input.md").write_text(
        union_make(pdf_info, MakeMode.MM_MD, "images"),
        encoding="utf-8",
    )
    (result_directory / "input_content_list.json").write_text(
        json.dumps(
            union_make(pdf_info, MakeMode.CONTENT_LIST, "images"),
            ensure_ascii=False,
            indent=4,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    (result_directory / "input_content_list_v2.json").write_text(
        json.dumps(
            union_make(pdf_info, MakeMode.CONTENT_LIST_V2, "images"),
            ensure_ascii=False,
            indent=4,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
