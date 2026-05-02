"""
Context pack loader + system prompt assembler.

Block 1 (cached, stable):
    companion.md + SKILL.md + 01-04 + 06_interpretive_notes
Block 2 (cached, rotates):
    INDEX.md
Block 3 (uncached, volatile):
    05_current_state.md + recent session-log tails + same-day raw transcripts
    + app-feedback.md tail + current date
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger()

TZ = ZoneInfo("Australia/Brisbane")


@dataclass
class SystemPromptBlocks:
    block1: str
    block2: str
    block3: str
    token_estimate: int


class ContextLoader:
    def __init__(
        self,
        data_root: Path,
        prompts_dir: Path,
        mode: str = "adult",
        display_name: str = "",
        kid_parent_display_name: str = "your parent",
        kid_parent_display_name_provider=None,
        people_md_provider=None,
    ) -> None:
        self.data_root = data_root
        self.prompts_dir = prompts_dir
        self.context_dir = data_root / "context"
        self.mode = mode
        self.display_name = display_name
        # Either a static value (back-compat for tests) or a callable returning
        # the value at assemble time. The callable form lets file-backed
        # overrides apply without recreating the loader.
        self._kid_parent_display_name_static = kid_parent_display_name
        self._kid_parent_display_name_provider = kid_parent_display_name_provider
        # Callable[[], str] returning the rendered people roster, or None.
        # Concatenated under INDEX.md in block 2 when present.
        self._people_md_provider = people_md_provider

    @property
    def kid_parent_display_name(self) -> str:
        if self._kid_parent_display_name_provider is not None:
            try:
                return self._kid_parent_display_name_provider() or self._kid_parent_display_name_static
            except Exception:  # noqa: BLE001
                return self._kid_parent_display_name_static
        return self._kid_parent_display_name_static

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except OSError as e:
            log.error("context.read_error", path=str(path), error=str(e))
            return ""

    def _read_last(self, path: Path, max_bytes: int = 4096) -> str:
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                return fh.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        except OSError:
            return ""

    def _recent_session_logs(self, n: int = 3, max_bytes_each: int = 2048) -> str:
        logs_dir = self.data_root / "session-logs"
        if not logs_dir.exists():
            return ""
        files = sorted(logs_dir.glob("*.md"), reverse=True)[:n]
        parts: list[str] = []
        for f in files:
            tail = self._read_last(f, max_bytes_each).strip()
            if tail:
                parts.append(f"### {f.stem}\n\n{tail}")
        return "\n\n".join(parts)

    def _recent_same_day_transcripts(
        self, window_hours: int = 8, max_chars_each: int = 2500
    ) -> str:
        sessions_dir = self.data_root / "sessions"
        if not sessions_dir.exists():
            return ""
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        parts: list[str] = []
        candidates = sorted(
            (p for p in sessions_dir.glob("*.jsonl") if ".bak-" not in p.name),
            reverse=True,
        )[:6]
        for path in candidates:
            try:
                created: datetime | None = None
                lines: list[str] = []
                with path.open("r", encoding="utf-8") as fh:
                    for raw in fh:
                        try:
                            rec = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") == "header" and not created:
                            ts = rec.get("created_at")
                            if ts:
                                try:
                                    created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                except ValueError:
                                    created = None
                        if rec.get("type") == "message":
                            role = rec.get("role")
                            content = (rec.get("content") or "").strip()
                            if role not in ("user", "assistant") or not content:
                                continue
                            meta = rec.get("meta") or {}
                            if meta.get("is_synthetic_opener"):
                                continue
                            lines.append(f"{role}: {content}")
                if created is None or created < cutoff:
                    continue
                tail = "\n".join(lines)
                if len(tail) > max_chars_each:
                    tail = "…\n" + tail[-max_chars_each:]
                if tail:
                    parts.append(f"### {path.stem}\n{tail}")
            except OSError:
                continue
        return "\n\n".join(parts)

    def _app_feedback_tail(self, max_bytes: int = 2048) -> str:
        return self._read_last(self.data_root / "app-feedback.md", max_bytes).strip()

    def assemble(self, frame_tag: str = "") -> SystemPromptBlocks:
        # Block 1 — stable, cached. Mode picks which companion prompt loads
        # (companion-adult.md vs companion-kid.md) plus which context files
        # are included. Kid mode uses a smaller, more targeted context set.
        companion_file = f"companion-{self.mode}.md"
        companion = self._read(self.prompts_dir / companion_file)

        # Substitute display-name placeholders in the kid prompt so the model
        # knows what to call the kid and how to refer to the parent.
        if self.mode == "kid":
            companion = (
                companion
                .replace("{{DISPLAY_NAME}}", self.display_name or "the kid")
                .replace("{{PARENT_DISPLAY_NAME}}", self.kid_parent_display_name)
            )

        if self.mode == "kid":
            # Kid mode: minimal context. The parent has typically just
            # populated context/01_background.md; the rest of the adult-mode
            # files don't apply.
            stable_files = [
                "01_background.md",
                "04_relationship_map.md",
            ]
        else:
            stable_files = [
                "SKILL.md",
                "01_background.md",
                "02_patterns.md",
                "03_therapy_history.md",
                "04_relationship_map.md",
                "06_interpretive_notes.md",
            ]

        stable_parts = [f"# COMPANION PROMPT\n\n{companion}"]
        for name in stable_files:
            content = self._read(self.context_dir / name).strip()
            if content:
                stable_parts.append(f"# {name}\n\n{content}")

        # Frame-tag dispatch (per /plan-eng-review D9): kid action buttons
        # post a `frame=<tag>` along with their message. The companion prompt
        # has tag-keyed behaviour blocks; we just inject the active tag here
        # so the model sees it as part of its system context.
        if frame_tag:
            stable_parts.append(
                f"# ACTIVE FRAME\n\n"
                f"The user just clicked the action button for `frame={frame_tag}`. "
                "Apply the matching behaviour block from the companion prompt."
            )

        block1 = "\n\n---\n\n".join(stable_parts)

        # Block 2 — rotated. INDEX.md + people.md (when a provider is wired).
        index = self._read(self.context_dir / "INDEX.md").strip()
        block2_parts = [
            f"# INDEX.md\n\n{index}"
            if index
            else "# INDEX.md\n\n(empty — no source-material or uploads yet)"
        ]
        if self._people_md_provider is not None:
            try:
                people_md = self._people_md_provider() or ""
                if people_md.strip():
                    block2_parts.append(people_md.strip())
            except Exception as e:  # noqa: BLE001
                log.warning("context.people_md_provider_failed", error=str(e))
        block2 = "\n\n".join(block2_parts)

        # Block 3 — volatile
        current_state = self._read(self.context_dir / "05_current_state.md").strip()
        session_log_tails = self._recent_session_logs(n=3, max_bytes_each=2048)
        same_day = self._recent_same_day_transcripts()
        app_feedback = self._app_feedback_tail()
        now = datetime.now(TZ).strftime("%A %d %B %Y, %H:%M %Z")
        volatile_parts = [
            f"# Current date/time\n\n{now}",
            f"# 05_current_state.md\n\n{current_state}" if current_state else "",
            f"# Recent session-log tails\n\n{session_log_tails}" if session_log_tails else "",
            (
                "# Earlier today — raw transcripts (not yet audited)\n\n"
                "These are sessions you (the companion) already had with Liam today. "
                "Don't repeat yourself or treat previously-covered information as new.\n\n"
                f"{same_day}"
            )
            if same_day
            else "",
            (
                "# App-feedback notes (recent)\n\n"
                "Notes the auditor has captured from prior sessions about issues with "
                "the app itself (UI, model behaviour, missing features). For Claude's "
                "awareness only — don't bring these up unless directly relevant.\n\n"
                f"{app_feedback}"
            )
            if app_feedback
            else "",
        ]
        block3 = "\n\n---\n\n".join(p for p in volatile_parts if p)

        total_chars = len(block1) + len(block2) + len(block3)
        return SystemPromptBlocks(
            block1=block1,
            block2=block2,
            block3=block3,
            token_estimate=total_chars // 4,
        )
