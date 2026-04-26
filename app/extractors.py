"""
Per-format extractors for the library pipeline.

Each Extractor takes the original file path and returns:
- ExtractResult — the markdown to write to extracted.md, the extractor name,
  optional page_count, and any extra meta (chat-export participants etc).
- VerifyResult — heuristic and optional model-spot-check results that go into
  verification.json.
- DateDetection — best-effort original_date, with a source label.

The orchestrator (route handler) picks an extractor via ExtractorRegistry.pick,
then calls extract → verify → detect_date in sequence, emitting status
messages along the way via the supplied Emit callback.

Commit B ships the framework + Text + Image extractors. PDF/DOCX/DOC/chat
extractors land in commit C.
"""

from __future__ import annotations

import datetime as _dt
import io
import logging
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


Emit = Callable[[str], None]
"""Status-line callback. The orchestrator pushes these to the SSE stream."""


@dataclass
class ExtractResult:
    extracted_md: str
    extractor: str
    page_count: int | None = None
    extra_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    status: str  # "ok" | "warn" | "fail"
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DateDetection:
    date: _dt.date | None
    source: str  # one of library.DateSource literals, or "unknown"


def _noop_emit(_msg: str) -> None:
    """Default emit: discard. Tests use list-collecting emitters."""


# ---------------------------------------------------------------------------
# Extractor protocol
# ---------------------------------------------------------------------------


class Extractor(Protocol):
    kind: str

    def can_handle(self, path: Path, mime: str) -> bool: ...
    def extract(self, path: Path, emit: Emit = _noop_emit) -> ExtractResult: ...
    def verify(
        self, path: Path, result: ExtractResult, emit: Emit = _noop_emit
    ) -> VerifyResult: ...
    def detect_date(self, path: Path) -> DateDetection: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ExtractorRegistry:
    """
    Pick-by-mime, with a chat-export sniffer for text payloads. The order
    of extractors matters — chat detection runs before generic text.
    """

    def __init__(self, extractors: list[Extractor] | None = None) -> None:
        self._extractors: list[Extractor] = list(extractors or [])

    def register(self, extractor: Extractor) -> None:
        self._extractors.append(extractor)

    def pick(self, path: Path, mime: str | None = None) -> Extractor | None:
        if mime is None:
            mime, _ = mimetypes.guess_type(str(path))
            mime = mime or "application/octet-stream"
        for ex in self._extractors:
            try:
                if ex.can_handle(path, mime):
                    return ex
            except Exception as e:  # noqa: BLE001 — extractor sniffers must not crash registry
                logging.getLogger(__name__).warning(
                    "extractor sniffer failed: %s on %s — %s", ex.kind, path.name, e
                )
        return None

    def __iter__(self):
        return iter(self._extractors)


# ---------------------------------------------------------------------------
# TextExtractor
# ---------------------------------------------------------------------------


class TextExtractor:
    """
    Verbatim copy. Title heuristic = first non-empty line.

    The library entry's `title` is a caller concern (paste form gives the
    user a label, file uploads default to filename); this extractor only
    deals with content. The title heuristic helper is exposed for callers
    that want it.
    """

    kind = "text"

    def can_handle(self, path: Path, mime: str) -> bool:
        if mime.startswith("text/"):
            return True
        # Catch .md and .txt that mimetypes sometimes mis-guesses.
        return path.suffix.lower() in (".txt", ".md", ".markdown", ".log")

    def extract(self, path: Path, emit: Emit = _noop_emit) -> ExtractResult:
        emit("Reading text…")
        text = path.read_text(encoding="utf-8", errors="replace")
        return ExtractResult(extracted_md=text, extractor="text_verbatim")

    def verify(
        self, path: Path, result: ExtractResult, emit: Emit = _noop_emit
    ) -> VerifyResult:
        emit("Verifying byte count…")
        original_bytes = path.stat().st_size
        # Round-trip equality on UTF-8 char count is fuzzy because of decode
        # errors; use the byte count of the encoded extracted as the check.
        round_trip_bytes = len(result.extracted_md.encode("utf-8"))
        ok = abs(round_trip_bytes - original_bytes) <= max(8, original_bytes // 100)
        return VerifyResult(
            status="ok" if ok else "warn",
            checks=[
                {
                    "name": "byte_round_trip",
                    "ok": ok,
                    "detail": f"original={original_bytes}B, extracted={round_trip_bytes}B",
                }
            ],
        )

    def detect_date(self, path: Path) -> DateDetection:
        # Free-text pastes have no automatic date. Caller prompts the user.
        return DateDetection(date=None, source="unknown")

    @staticmethod
    def title_from_text(text: str, fallback: str = "Untitled note") -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped[:80]
        return fallback


# ---------------------------------------------------------------------------
# ImageExtractor
# ---------------------------------------------------------------------------


VisionTranscribeFn = Callable[[bytes, str], str]
"""(image_bytes, mime_type) -> markdown transcript."""


VisionSpotCheckFn = Callable[[bytes, str, str], str]
"""(image_bytes, mime_type, extracted_md) -> 'ok' or list of issues."""


VISION_TRANSCRIBE_PROMPT = (
    "Transcribe all visible text in this image verbatim. "
    "Note any UI chrome, sender names, timestamps. "
    "Use markdown for headings and lists. "
    "If the image contains no text, respond with exactly: NO_TEXT_FOUND."
)

VISION_SPOTCHECK_PROMPT = (
    "Below is a transcription claimed to be from the attached image. "
    "Compare them. Respond with exactly 'ok' if the transcription is "
    "complete and accurate, or list specific issues (one per line). "
    "Do not include any other commentary.\n\n"
    "Transcription:\n{extracted}"
)


class ImageExtractor:
    """
    Vision OCR via injected callable (Sonnet in prod, mocked in tests).
    EXIF date detection via Pillow.
    """

    kind = "image"
    SUPPORTED_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic")
    SPOT_CHECK_DEFAULT = True

    def __init__(
        self,
        transcribe: VisionTranscribeFn | None = None,
        spot_check: VisionSpotCheckFn | None = None,
        spot_check_enabled: bool = SPOT_CHECK_DEFAULT,
    ) -> None:
        self._transcribe = transcribe
        self._spot_check = spot_check
        self._spot_check_enabled = spot_check_enabled

    def can_handle(self, path: Path, mime: str) -> bool:
        return mime.startswith("image/") or path.suffix.lower() in self.SUPPORTED_EXTS

    def extract(self, path: Path, emit: Emit = _noop_emit) -> ExtractResult:
        if self._transcribe is None:
            raise RuntimeError(
                "ImageExtractor requires a transcribe callable; pass one at construction"
            )
        emit("Reading image…")
        image_bytes = path.read_bytes()
        mime = self._mime_for(path)
        emit("Transcribing via vision…")
        text = self._transcribe(image_bytes, mime)
        if text.strip() == "NO_TEXT_FOUND":
            text = ""
        return ExtractResult(extracted_md=text, extractor="image_vision_ocr")

    def verify(
        self, path: Path, result: ExtractResult, emit: Emit = _noop_emit
    ) -> VerifyResult:
        checks: list[dict[str, Any]] = []
        chars = len(result.extracted_md)
        # Heuristic: image with no text is a warn, not a fail (memes / photos).
        no_text_ok = chars > 0
        checks.append(
            {
                "name": "transcript_nonempty",
                "ok": no_text_ok,
                "detail": f"extracted={chars} chars (warn if 0; some images have no text)",
            }
        )
        if not no_text_ok:
            return VerifyResult(status="warn", checks=checks)

        if self._spot_check_enabled and self._spot_check is not None:
            emit("Sanity checking…")
            try:
                image_bytes = path.read_bytes()
                mime = self._mime_for(path)
                judgement = self._spot_check(image_bytes, mime, result.extracted_md).strip()
                ok = judgement.lower() == "ok"
                checks.append(
                    {
                        "name": "vision_spot_check",
                        "ok": ok,
                        "detail": "ok" if ok else f"issues: {judgement[:200]}",
                    }
                )
                if not ok:
                    return VerifyResult(status="warn", checks=checks)
            except Exception as e:  # noqa: BLE001
                checks.append(
                    {
                        "name": "vision_spot_check",
                        "ok": False,
                        "detail": f"spot-check call failed: {e!s}",
                    }
                )
                return VerifyResult(status="warn", checks=checks)

        return VerifyResult(status="ok", checks=checks)

    def detect_date(self, path: Path) -> DateDetection:
        try:
            from PIL import Image
            from PIL.ExifTags import TAGS
        except ImportError:
            return DateDetection(date=None, source="unknown")
        try:
            with Image.open(path) as img:
                exif = img.getexif()
                if not exif:
                    return DateDetection(date=None, source="unknown")
                # Tag 36867 = DateTimeOriginal. Format: "YYYY:MM:DD HH:MM:SS"
                for tag_id, value in exif.items():
                    name = TAGS.get(tag_id, tag_id)
                    if name in ("DateTimeOriginal", "DateTime", "DateTimeDigitized"):
                        if isinstance(value, str):
                            parsed = self._parse_exif_date(value)
                            if parsed:
                                return DateDetection(date=parsed, source="exif")
        except Exception as e:  # noqa: BLE001 — date detection must not crash extraction
            log.warning("image.exif_read_failed", path=str(path), error=str(e))
        return DateDetection(date=None, source="unknown")

    @staticmethod
    def _parse_exif_date(s: str) -> _dt.date | None:
        try:
            return _dt.datetime.strptime(s.split(" ")[0], "%Y:%m:%d").date()
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _mime_for(path: Path) -> str:
        guessed, _ = mimetypes.guess_type(str(path))
        if guessed:
            return guessed
        return f"image/{path.suffix.lstrip('.').lower() or 'png'}"


# ---------------------------------------------------------------------------
# Build a default registry with the extractors that exist now.
# Commit C will add PDF/DOCX/DOC/chat extractors.
# ---------------------------------------------------------------------------


def build_registry(
    transcribe: VisionTranscribeFn | None = None,
    spot_check: VisionSpotCheckFn | None = None,
) -> ExtractorRegistry:
    return ExtractorRegistry(
        [
            ImageExtractor(transcribe=transcribe, spot_check=spot_check),
            TextExtractor(),  # last — catches text/* and unknown extensions
        ]
    )


# ---------------------------------------------------------------------------
# Helpers shared with route handlers
# ---------------------------------------------------------------------------


def make_vision_callables(
    claude_client: Any,
    model: str = "claude-sonnet-4-6",
) -> tuple[VisionTranscribeFn, VisionSpotCheckFn]:
    """
    Build the transcribe + spot_check callables that wrap a ClaudeClient.
    Imported here (and not at module top) to avoid a hard claude.py
    dependency at import time — useful for tests that don't need vision.
    """

    def transcribe(image_bytes: bytes, mime: str) -> str:
        import base64

        b64 = base64.b64encode(image_bytes).decode("ascii")
        reply = claude_client.single_turn(
            model=model,
            user_content=[
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": VISION_TRANSCRIBE_PROMPT},
            ],
            max_tokens=4096,
        )
        return reply.text

    def spot_check(image_bytes: bytes, mime: str, extracted_md: str) -> str:
        import base64

        b64 = base64.b64encode(image_bytes).decode("ascii")
        prompt = VISION_SPOTCHECK_PROMPT.format(extracted=extracted_md[:4000])
        reply = claude_client.single_turn(
            model=model,
            user_content=[
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ],
            max_tokens=512,
        )
        return reply.text

    # Discourage the linter from removing the io import (used by callers
    # constructing in-memory bytes for testing).
    _ = io.BytesIO
    return transcribe, spot_check
