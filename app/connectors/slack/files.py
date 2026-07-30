"""File download + text extraction (PRD §9.9).

Supported: PDF (pypdf), Markdown/plain text (decode), .docx (python-docx).
External/hosted files are not downloadable and are skipped by the caller.
Empty extraction (e.g. scanned/image PDF, no OCR) => extracted_ok = False.
"""

from __future__ import annotations

import io

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.document import FileRef

log = get_logger(__name__)

SUPPORTED_MIME_PREFIXES = (
    "application/pdf",
    "text/markdown",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
)


class FileSkip(Exception):
    """Non-fatal: this file cannot/should not be ingested. Caller logs + continues."""


def file_ref_from_slack(f: dict) -> FileRef:
    is_external = bool(f.get("is_external", False))
    dl = f.get("url_private_download")
    return FileRef(
        file_id=f.get("id", ""),
        name=f.get("name", "") or "",
        mime=f.get("mimetype", "") or "",
        size=int(f.get("size", 0) or 0),
        is_external=is_external,
        url_private_download=dl,
        downloadable=(not is_external and bool(dl)),
    )


def _download(ref: FileRef) -> bytes:
    if not ref.downloadable:
        raise FileSkip(f"file {ref.file_id} not downloadable (external or no url)")
    if ref.size and ref.size > settings.max_file_bytes:
        raise FileSkip(f"file {ref.file_id} too large ({ref.size} bytes)")
    headers = {"Authorization": f"Bearer {settings.slack_bot_token}"}
    with httpx.Client(timeout=settings.openai_timeout_s) as client:
        resp = client.get(ref.url_private_download, headers=headers, follow_redirects=True)
    resp.raise_for_status()
    data = resp.content
    # Guard: an unauthenticated fetch returns an HTML login page, not file bytes.
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/html") or data[:15].lstrip().lower().startswith(b"<!doctype html"):
        raise FileSkip(f"file {ref.file_id} returned HTML (auth/permission issue)")
    if len(data) > settings.max_file_bytes:
        raise FileSkip(f"file {ref.file_id} exceeds max bytes after download")
    return data


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page shouldn't kill the file
            continue
    return "\n\n".join(p for p in pages if p).strip()


def _extract_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in document.paragraphs if p.text).strip()


def _extract_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def extract_file_text(ref: FileRef) -> str:
    """Download + extract. Returns text; raises FileSkip when unsupported/undownloadable.

    Empty text from a supported type raises FileSkip too (treated as a failure, not
    a silent success), so `extracted_ok` is only set when we actually got content.
    """
    mime = (ref.mime or "").lower()
    if not any(mime.startswith(p) for p in SUPPORTED_MIME_PREFIXES):
        # fall back to extension sniffing for md/txt without a mime
        name = ref.name.lower()
        if not (name.endswith(".md") or name.endswith(".txt")):
            raise FileSkip(f"unsupported mime '{mime}' for {ref.name}")

    data = _download(ref)

    if mime.startswith("application/pdf"):
        text = _extract_pdf(data)
    elif mime.endswith("wordprocessingml.document") or ref.name.lower().endswith(".docx"):
        text = _extract_docx(data)
    else:
        text = _extract_text(data)

    if not text.strip():
        raise FileSkip(f"empty extraction for {ref.name} (scanned/image? no OCR)")
    return text
