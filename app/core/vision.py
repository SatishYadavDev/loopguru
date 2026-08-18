"""Vision-based extraction (OCR + description) for images and scanned PDFs.

Uses the OpenAI vision model to transcribe text and describe diagrams/tables/charts,
so image-only content (screenshots, architecture diagrams, scanned policy PDFs) becomes
searchable. Gated by ENABLE_VISION.
"""

from __future__ import annotations

import base64

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger
from app.core.openai_client import get_openai

log = get_logger(__name__)

_VISION_PROMPT = (
    "You are an OCR and image-understanding engine for an internal knowledge base. "
    "Transcribe ALL text in this image verbatim (preserve numbers, tables, and lists), "
    "and clearly describe any diagrams, charts, or screenshots so the content is fully "
    "searchable and self-contained. Output plain text only — no commentary."
)

IMAGE_MIME_PREFIX = "image/"
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def is_image(mime: str, name: str) -> bool:
    return (mime or "").lower().startswith(IMAGE_MIME_PREFIX) or (
        name or ""
    ).lower().endswith(_IMAGE_EXTS)


@retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20))
def describe_image(data: bytes, mime: str = "image/png") -> str:
    """Transcribe + describe a single image. Returns plain text ('' if nothing)."""
    if not (mime or "").startswith(IMAGE_MIME_PREFIX):
        mime = "image/png"
    b64 = base64.b64encode(data).decode()
    resp = get_openai().chat.completions.create(
        model=settings.vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()


def pdf_to_text_via_vision(data: bytes) -> str:
    """Rasterize a (scanned/image) PDF's pages and run vision on each. Capped by
    MAX_IMAGE_PAGES. Requires PyMuPDF; if unavailable, returns ''.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("PyMuPDF not installed; cannot vision-scan PDF")
        return ""
    texts: list[str] = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            if i >= settings.max_image_pages:
                break
            pix = page.get_pixmap(dpi=150)
            png = pix.tobytes("png")
            page_text = describe_image(png, "image/png")
            if page_text:
                texts.append(page_text)
    return "\n\n".join(texts).strip()
