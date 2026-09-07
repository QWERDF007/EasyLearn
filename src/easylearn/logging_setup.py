"""Application logging with one append-only file per local calendar day."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from threading import RLock
from typing import Any

from loguru import logger as loguru_logger

_LOGGER_NAME = "easylearn"
_HANDLER_MARKER = "_easylearn_managed"


class DateFileHandler(logging.Handler):
    """Write records to ``YYYY-MM-DD.log`` and switch files at midnight."""

    def __init__(
        self, log_dir: Path, *, date_provider: Callable[[], date] = date.today
    ) -> None:
        super().__init__()
        self.log_dir = log_dir
        self._date_provider = date_provider
        self._current_date: date | None = None
        self._stream: Any = None
        self._file_lock = RLock()
        self.terminator = "\n"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._file_lock:
                current_date = self._date_provider()
                if current_date != self._current_date:
                    self._switch_file(current_date)
                self._stream.write(self.format(record) + self.terminator)
                self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self._file_lock:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._current_date = None
        super().close()

    def _switch_file(self, current_date: date) -> None:
        if self._stream is not None:
            self._stream.close()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stream = (self.log_dir / f"{current_date.isoformat()}.log").open(
            "a", encoding="utf-8"
        )
        self._current_date = current_date


class LoggingController:
    def __init__(
        self,
        loggers: tuple[logging.Logger, ...],
        handlers: tuple[logging.Handler, ...],
        sink_id: int | None,
    ) -> None:
        self._loggers = loggers
        self._handlers = handlers
        self._handler = handlers[0] if handlers else None
        self._sink_id = sink_id
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        for logger in self._loggers:
            for handler in self._handlers:
                if handler in logger.handlers:
                    logger.removeHandler(handler)
        if self._sink_id is not None:
            loguru_logger.remove(self._sink_id)
        for handler in self._handlers:
            handler.close()
        self._closed = True


def _is_console_handler(handler: logging.Handler) -> bool:
    if handler.__class__.__module__.startswith(("_pytest", "pytest")):
        return False
    return isinstance(handler, logging.StreamHandler)


def configure_logging(
    log_dir: Path, *, date_provider: Callable[[], date] = date.today
) -> LoggingController:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    main_logger = logging.getLogger("easylearn.main")
    cleanup_loggers = (
        root,
        logging.getLogger(_LOGGER_NAME),
        main_logger,
        logging.getLogger("uvicorn.access"),
        logging.getLogger("mineru"),
    )
    for logger in cleanup_loggers:
        for handler in tuple(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False) or _is_console_handler(handler):
                logger.removeHandler(handler)
                if getattr(handler, _HANDLER_MARKER, False):
                    handler.close()

    file_handler = DateFileHandler(log_dir, date_provider=date_provider)
    setattr(file_handler, _HANDLER_MARKER, True)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    attached_loggers = (
        root,
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
    )
    for logger in attached_loggers:
        logger.addHandler(file_handler)
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Console handler dedicated to service lifecycle (startup, shutdown)
    console_handler = logging.StreamHandler(sys.stdout)
    setattr(console_handler, _HANDLER_MARKER, True)
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    main_logger.addHandler(console_handler)

    loguru_logger.remove()
    def forward_loguru(message: str) -> None:
        logging.getLogger("mineru").info(message.rstrip("\n"))

    sink_id = loguru_logger.add(forward_loguru, level="INFO", format="{message}")
    return LoggingController(
        (*attached_loggers, main_logger),
        (file_handler, console_handler),
        sink_id,
    )

