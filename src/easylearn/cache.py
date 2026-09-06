"""Small process-local LRU for immutable parse snapshots."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from easylearn.document_ir.schema import DocumentIR


@dataclass(frozen=True)
class CacheStats:
    entries: int
    bytes: int


class DocumentCache:
    """Bound cached IR by both document count and serialized size.

    ``DocumentIR`` is immutable, so a cache hit can be returned directly. The
    cache is intentionally synchronous: callers already run on the single
    application event loop and this operation does not perform I/O.
    """

    def __init__(self, *, max_documents: int, max_bytes: int) -> None:
        if max_documents < 1 or max_bytes < 1:
            raise ValueError("Cache limits must be positive")
        self.max_documents = max_documents
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple[UUID, UUID], tuple[DocumentIR, int]] = OrderedDict()
        self._bytes = 0

    def get(self, document_id: UUID, parse_id: UUID) -> DocumentIR | None:
        key = (document_id, parse_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, document_id: UUID, parse_id: UUID, value: DocumentIR) -> None:
        key = (document_id, parse_id)
        size = len(value.model_dump_json(exclude_computed_fields=True).encode("utf-8"))
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        if size > self.max_bytes:
            return
        self._entries[key] = (value, size)
        self._bytes += size
        self._trim()

    def invalidate(self, document_id: UUID, parse_id: UUID | None = None) -> None:
        keys = (
            [(document_id, parse_id)]
            if parse_id is not None
            else [key for key in self._entries if key[0] == document_id]
        )
        for key in keys:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._bytes -= entry[1]

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    @property
    def stats(self) -> CacheStats:
        return CacheStats(entries=len(self._entries), bytes=self._bytes)

    def _trim(self) -> None:
        while len(self._entries) > self.max_documents or self._bytes > self.max_bytes:
            _, (_, size) = self._entries.popitem(last=False)
            self._bytes -= size
