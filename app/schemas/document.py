"""Common Document + FileRef schema (PRD §8). Source-independent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileRef:
    """A file attached to a source item, before/after extraction."""

    file_id: str
    name: str
    mime: str
    size: int
    is_external: bool
    url_private_download: str | None
    downloadable: bool  # not external AND has a private download url
    url_private: str | None = None
    is_canvas: bool = False  # Slack Canvas / quip doc (content is HTML at url_private)


@dataclass
class Document:
    """The unit the pipeline chunks, embeds, and stores.

    `kind` distinguishes a per-message document from a per-file document.
    Message docs use `scope_id`; file docs use `scope_ids` (multi-channel).
    """

    doc_id: str
    source: str
    text: str
    kind: str = "message"  # "message" | "file"
    scope_id: str | None = None  # message docs
    scope_ids: list[str] = field(default_factory=list)  # file docs (all channels)
    title: str | None = None
    author: str | None = None
    created_ts: str = ""  # opaque Slack ts string (never float-parsed)
    created_epoch: int = 0  # integer seconds, for range filters + recency
    permalink: str | None = None
    thread_id: str | None = None
    attachments: list[FileRef] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def all_scope_ids(self) -> list[str]:
        if self.kind == "file":
            return list(self.scope_ids)
        return [self.scope_id] if self.scope_id else []
