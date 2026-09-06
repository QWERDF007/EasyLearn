"""Application logging with a bounded, file-backed handler."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "easylearn"
_HANDLER_MARKER = "_easylearn_managed"


class LoggingController:
    def __init__(self, logger: logging.Logger, handler: RotatingFileHandler) -> None:
        self._logger = logger
        self._handler = handler
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._logger.removeHandler(self._handler)
        self._handler.close()
        self._closed = True


def configure_logging(log_dir: Path, *, max_bytes: int, backup_count: int) -> LoggingController:
    if max_bytes < 1 or backup_count < 0:
        raise ValueError("Log limits are invalid")
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    for handler in tuple(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        log_dir / "easylearn.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    return LoggingController(logger, handler)
