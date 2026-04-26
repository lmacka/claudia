"""Tests for app/extractors.py — framework + Text + Image extractors."""

from __future__ import annotations

import datetime as _dt
import io
from pathlib import Path

import pytest
from PIL import Image

from app.extractors import (
    DateDetection,
    ExtractorRegistry,
    ExtractResult,
    ImageExtractor,
    TextExtractor,
    VerifyResult,
    build_registry,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emits() -> tuple[list, callable]:
    """Returns (collected_list, emit_fn)."""
    collected: list[str] = []
    return collected, collected.append


def _png_bytes(size: tuple[int, int] = (4, 4)) -> bytes:
    img = Image.new("RGB", size, color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_png(path: Path) -> None:
    path.write_bytes(_png_bytes())


# ---------------------------------------------------------------------------
# TextExtractor
# ---------------------------------------------------------------------------


def test_text_extractor_can_handle_text_mime():
    ex = TextExtractor()
    assert ex.can_handle(Path("note.txt"), "text/plain")
    assert ex.can_handle(Path("note.md"), "text/markdown")
    assert ex.can_handle(Path("anything.log"), "application/octet-stream")  # by ext
    assert not ex.can_handle(Path("photo.png"), "image/png")


def test_text_extractor_extract_verbatim(tmp_path: Path):
    p = tmp_path / "note.txt"
    p.write_text("hello world\nline two\n", encoding="utf-8")
    emits, emit = _emits()
    result = TextExtractor().extract(p, emit)
    assert result.extracted_md == "hello world\nline two\n"
    assert result.extractor == "text_verbatim"
    assert "Reading text" in emits[0]


def test_text_extractor_verify_byte_round_trip(tmp_path: Path):
    p = tmp_path / "note.txt"
    content = "small note"
    p.write_text(content, encoding="utf-8")
    ex = TextExtractor()
    extract = ex.extract(p)
    verify = ex.verify(p, extract)
    assert verify.status == "ok"
    assert any(check["name"] == "byte_round_trip" for check in verify.checks)


def test_text_extractor_detect_date_returns_unknown(tmp_path: Path):
    p = tmp_path / "note.txt"
    p.write_text("free text", encoding="utf-8")
    detection = TextExtractor().detect_date(p)
    assert detection.date is None
    assert detection.source == "unknown"


def test_text_extractor_title_heuristic():
    text = "  \n\n  First real line\nsecond line\n"
    assert TextExtractor.title_from_text(text) == "First real line"


def test_text_extractor_title_truncates_long_first_line():
    text = "x" * 200
    assert len(TextExtractor.title_from_text(text)) == 80


def test_text_extractor_title_fallback_for_empty():
    assert TextExtractor.title_from_text("") == "Untitled note"
    assert TextExtractor.title_from_text("\n\n  \n") == "Untitled note"


# ---------------------------------------------------------------------------
# ImageExtractor — extract
# ---------------------------------------------------------------------------


def test_image_extractor_can_handle_image_mime():
    ex = ImageExtractor(transcribe=lambda b, m: "")
    assert ex.can_handle(Path("p.png"), "image/png")
    assert ex.can_handle(Path("p.jpg"), "image/jpeg")
    assert ex.can_handle(Path("p.heic"), "application/octet-stream")  # by ext
    assert not ex.can_handle(Path("notes.txt"), "text/plain")


def test_image_extractor_extract_calls_transcribe(tmp_path: Path):
    p = tmp_path / "snap.png"
    _write_png(p)

    captured: dict = {}

    def fake_transcribe(image_bytes: bytes, mime: str) -> str:
        captured["bytes_len"] = len(image_bytes)
        captured["mime"] = mime
        return "## Sofia\nkk\nidk like its fine"

    emits, emit = _emits()
    ex = ImageExtractor(transcribe=fake_transcribe)
    result = ex.extract(p, emit)
    assert result.extractor == "image_vision_ocr"
    assert "Sofia" in result.extracted_md
    assert captured["bytes_len"] > 0
    assert captured["mime"] == "image/png"
    assert any("Transcribing" in m for m in emits)


def test_image_extractor_no_text_found_returns_empty(tmp_path: Path):
    p = tmp_path / "blank.png"
    _write_png(p)
    ex = ImageExtractor(transcribe=lambda b, m: "NO_TEXT_FOUND")
    result = ex.extract(p)
    assert result.extracted_md == ""


def test_image_extractor_requires_transcribe_callable(tmp_path: Path):
    p = tmp_path / "snap.png"
    _write_png(p)
    ex = ImageExtractor()  # no transcribe injected
    with pytest.raises(RuntimeError):
        ex.extract(p)


# ---------------------------------------------------------------------------
# ImageExtractor — verify
# ---------------------------------------------------------------------------


def test_image_verify_ok_with_text_and_passing_spotcheck(tmp_path: Path):
    p = tmp_path / "snap.png"
    _write_png(p)
    ex = ImageExtractor(
        transcribe=lambda b, m: "transcribed text",
        spot_check=lambda b, m, e: "ok",
    )
    extract = ex.extract(p)
    verify = ex.verify(p, extract)
    assert verify.status == "ok"
    assert any(c["name"] == "vision_spot_check" and c["ok"] for c in verify.checks)


def test_image_verify_warn_when_no_text(tmp_path: Path):
    p = tmp_path / "blank.png"
    _write_png(p)
    ex = ImageExtractor(transcribe=lambda b, m: "NO_TEXT_FOUND")
    extract = ex.extract(p)
    verify = ex.verify(p, extract)
    assert verify.status == "warn"
    # Spot-check skipped because empty text gates it.
    assert all(c["name"] != "vision_spot_check" for c in verify.checks)


def test_image_verify_warn_when_spotcheck_lists_issues(tmp_path: Path):
    p = tmp_path / "snap.png"
    _write_png(p)
    ex = ImageExtractor(
        transcribe=lambda b, m: "transcribed",
        spot_check=lambda b, m, e: "missing the word 'urgent' in line 3\nwrong sender name",
    )
    extract = ex.extract(p)
    verify = ex.verify(p, extract)
    assert verify.status == "warn"
    spot = next(c for c in verify.checks if c["name"] == "vision_spot_check")
    assert not spot["ok"]
    assert "missing" in spot["detail"]


def test_image_verify_warn_when_spotcheck_raises(tmp_path: Path):
    p = tmp_path / "snap.png"
    _write_png(p)

    def boom(*_args):
        raise RuntimeError("spot check api blew up")

    ex = ImageExtractor(
        transcribe=lambda b, m: "transcribed",
        spot_check=boom,
    )
    extract = ex.extract(p)
    verify = ex.verify(p, extract)
    assert verify.status == "warn"
    spot = next(c for c in verify.checks if c["name"] == "vision_spot_check")
    assert "spot-check call failed" in spot["detail"]


def test_image_verify_skips_spotcheck_when_disabled(tmp_path: Path):
    p = tmp_path / "snap.png"
    _write_png(p)
    spot_calls = []
    ex = ImageExtractor(
        transcribe=lambda b, m: "transcribed",
        spot_check=lambda b, m, e: spot_calls.append("called") or "ok",
        spot_check_enabled=False,
    )
    verify = ex.verify(p, ex.extract(p))
    assert verify.status == "ok"
    assert spot_calls == []  # never called


# ---------------------------------------------------------------------------
# ImageExtractor — date detection
# ---------------------------------------------------------------------------


def test_image_detect_date_no_exif(tmp_path: Path):
    p = tmp_path / "no-exif.png"
    _write_png(p)
    detection = ImageExtractor().detect_date(p)
    assert detection.date is None
    assert detection.source == "unknown"


def test_image_parse_exif_date_valid():
    parsed = ImageExtractor._parse_exif_date("2024:11:23 18:42:01")
    assert parsed == _dt.date(2024, 11, 23)


def test_image_parse_exif_date_invalid_returns_none():
    assert ImageExtractor._parse_exif_date("not a date") is None
    assert ImageExtractor._parse_exif_date("") is None


def test_image_detect_date_with_real_exif(tmp_path: Path):
    """Write a JPEG with EXIF DateTimeOriginal and confirm we read it back."""
    p = tmp_path / "phone.jpg"
    img = Image.new("RGB", (8, 8), color=(10, 20, 30))
    exif = img.getexif()
    exif[306] = "2026:04:25 09:15:00"  # DateTime
    exif[36867] = "2026:04:25 09:15:00"  # DateTimeOriginal
    img.save(p, format="JPEG", exif=exif)

    detection = ImageExtractor().detect_date(p)
    assert detection.date == _dt.date(2026, 4, 25)
    assert detection.source == "exif"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_pick_image_first(tmp_path: Path):
    reg = build_registry(transcribe=lambda b, m: "")
    p = tmp_path / "snap.png"
    _write_png(p)
    picked = reg.pick(p, "image/png")
    assert picked is not None
    assert picked.kind == "image"


def test_registry_pick_text_for_text_payload(tmp_path: Path):
    reg = build_registry()
    p = tmp_path / "note.txt"
    p.write_text("hello", encoding="utf-8")
    picked = reg.pick(p, "text/plain")
    assert picked is not None
    assert picked.kind == "text"


def test_registry_pick_returns_none_for_unknown(tmp_path: Path):
    reg = build_registry()
    p = tmp_path / "mystery.bin"
    p.write_bytes(b"\x00\x01\x02")
    # Without a binary handler, no extractor can_handle this.
    picked = reg.pick(p, "application/octet-stream")
    assert picked is None


def test_registry_iteration():
    reg = build_registry()
    kinds = [ex.kind for ex in reg]
    assert "image" in kinds
    assert "text" in kinds


def test_registry_swallows_sniffer_exceptions(tmp_path: Path):
    """A misbehaving extractor's can_handle must not crash the registry."""

    class BoomExtractor:
        kind = "boom"

        def can_handle(self, path, mime):
            raise RuntimeError("sniffer blew up")

        def extract(self, path, emit=lambda _m: None):  # pragma: no cover
            raise NotImplementedError

        def verify(self, path, result, emit=lambda _m: None):  # pragma: no cover
            raise NotImplementedError

        def detect_date(self, path):  # pragma: no cover
            raise NotImplementedError

    reg = ExtractorRegistry([BoomExtractor(), TextExtractor()])
    p = tmp_path / "n.txt"
    p.write_text("hi", encoding="utf-8")
    picked = reg.pick(p, "text/plain")
    assert picked is not None
    assert picked.kind == "text"


# ---------------------------------------------------------------------------
# Smoke: dataclass shapes
# ---------------------------------------------------------------------------


def test_extract_result_extra_meta_default():
    r = ExtractResult(extracted_md="x", extractor="t")
    assert r.extra_meta == {}


def test_verify_result_checks_default():
    v = VerifyResult(status="ok")
    assert v.checks == []


def test_date_detection_unknown():
    d = DateDetection(date=None, source="unknown")
    assert d.date is None
