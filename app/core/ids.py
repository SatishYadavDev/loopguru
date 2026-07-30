"""Deterministic id + content-hash helpers (PRD §8, §10.2).

- doc_id: stable string key per source item.
- point_id: UUIDv5 of "{doc_id}:{chunk_idx}" — Qdrant requires uint64/UUID.
- content_hash: sha256 over the parts that define "has this changed".
"""

from __future__ import annotations

import hashlib
import uuid

# Fixed namespace so UUIDv5 ids are stable across processes/machines.
_NAMESPACE = uuid.UUID("6f4d2a1e-9b3c-4c5d-8e7f-0a1b2c3d4e5f")


def slack_message_doc_id(channel_id: str, ts: str) -> str:
    return f"slack:{channel_id}:{ts}"


def slack_file_doc_id(file_id: str) -> str:
    return f"slack:file:{file_id}"


def point_id(doc_id: str, chunk_idx: int) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{doc_id}:{chunk_idx}"))


def content_hash(*parts: str | None) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
