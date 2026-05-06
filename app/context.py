"""
Context pack loader + system prompt assembler.

Block 1 (cached, stable):
    companion-adult.md + 01_background.md + optional additional
    instructions appended at assemble time
Block 2 (cached, rotates):
    library index (rendered from Library.render_index_md via provider)
    + people roster (rendered from People.render_people_md via provider)
Block 3 (uncached, volatile):
    05_current_state.md + recent session-log tails + same-day raw
    transcripts (from SqliteSessionStore via provider) + app-feedback
    tail (from app_feedback table) + current date
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
        display_name: str = "",
        people_md_provider=None,
        additional_instructions_provider=None,
        library_index_provider=None,
        same_day_transcripts_provider=None,
    ) -> None:
        self.data_root = data_root
        self.prompts_dir = prompts_dir
        self.context_dir = data_root / "context"
        self.display_name = display_name
        self._people_md_provider = people_md_provider
        # Callable[[], str] returning the user's additional instructions
        # (set in /setup/3 or /settings, persisted in kv_store). When
        # non-empty, appended to the companion prompt under a clear heading.
        self._additional_instructions_provider = additional_instructions_provider
        # Callable[[], str] returning the rendered library index (markdown).
        self._library_index_provider = library_index_provider
        # Callable[[], str] returning recent same-day transcripts (markdown).
        self._same_day_transcripts_provider = same_day_transcripts_provider

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


    def _app_feedback_tail(self, max_bytes: int = 2048) -> str:
        try:
            from app.db_audit import app_feedback_tail

            return app_feedback_tail(self.data_root, max_chars=max_bytes)
        except Exception as e:  # noqa: BLE001
            log.debug("app_feedback_tail.unavailable", error=str(e))
            return ""

    def assemble(self, frame_tag: str = "") -> SystemPromptBlocks:
        # Block 1 — stable, cached.
        companion = self._read(self.prompts_dir / "companion-adult.md")

        # Append user's free-form additional instructions (set in /setup/3
        # or /settings). When non-empty, scoped under a clear heading so
        # overrides are visible to anyone reading the assembled prompt.
        if self._additional_instructions_provider is not None:
            try:
                extra = (self._additional_instructions_provider() or "").strip()
            except Exception:  # noqa: BLE001
                extra = ""
            if extra:
                companion = (
                    companion
                    + "\n\n## Additional instructions from the user\n\n"
                    + extra
                )

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

        # Frame tag plumbing kept for callers that pass one in; today no UI
        # surfaces frame buttons, so this is essentially a no-op.
        if frame_tag:
            stable_parts.append(
                f"# ACTIVE FRAME\n\n"
                f"The user just clicked the action button for `frame={frame_tag}`. "
                "Apply the matching behaviour block from the companion prompt."
            )

        block1 = "\n\n---\n\n".join(stable_parts)

        # Block 2 — rotated. Library index + people roster.
        rendered_index = ""
        if self._library_index_provider is not None:
            try:
                rendered_index = (self._library_index_provider() or "").strip()
            except Exception as e:  # noqa: BLE001
                log.warning("context.library_index_provider_failed", error=str(e))
        block2_parts = [
            rendered_index
            or "# INDEX.md\n\n(empty — no source-material or uploads yet)"
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
        same_day = ""
        if self._same_day_transcripts_provider is not None:
            try:
                same_day = (self._same_day_transcripts_provider() or "").strip()
            except Exception as e:  # noqa: BLE001
                log.warning("context.same_day_provider_failed", error=str(e))
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
