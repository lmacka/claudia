"""
Session-end auditor.

Runs after every session in a background task. Writes a session-log,
optionally rewrites 05_current_state.md, and appends any app-feedback the
auditor extracted from Liam's complaints/observations during the chat.

Forced tool-use guarantees structured output — never returns text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from app.claude import ClaudeClient, Usage
from app.storage import Message

log = structlog.get_logger()


@dataclass
class AppFeedback:
    quote: str  # short quoted snippet from Liam (or paraphrase if no clean quote)
    observation: str  # what the auditor took from it


@dataclass
class AuditorReport:
    title: str
    summary_markdown: str
    current_state_proposed: str  # full replacement, "" if no change
    current_state_rationale: str
    app_feedback: list[AppFeedback]
    usage: Usage


@dataclass
class SummariserInput:
    session_id: str
    messages: list[Message]
    current_state: str
    recent_session_logs: str = ""
    timezone_now: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _auditor_system_prompt(prompts_dir: Path) -> str:
    return (prompts_dir / "auditor.md").read_text(encoding="utf-8")


def _build_user_message(inp: SummariserInput) -> str:
    transcript_lines: list[str] = []
    for m in inp.messages:
        if m.role not in ("user", "assistant"):
            continue
        if not (m.content or "").strip():
            continue
        label = "LIAM" if m.role == "user" else "COMPANION"
        transcript_lines.append(f"### {label}\n{m.content}")
    transcript = "\n\n".join(transcript_lines) or "(empty transcript)"
    return (
        f"# Session id\n{inp.session_id}\n\n"
        f"# Current time\n{inp.timezone_now}\n\n"
        f"# Current 05_current_state.md\n```markdown\n{inp.current_state}\n```\n\n"
        f"# Recent session-log tails\n{inp.recent_session_logs or '(none)'}\n\n"
        f"# Transcript\n{transcript}\n\n"
        "Call submit_audit_report exactly once. Tool-only output."
    )


class AuditorError(Exception):
    pass


AUDIT_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "3-7 word session title, no quotes, no emoji",
        },
        "summary_markdown": {
            "type": "string",
            "description": (
                "Session log body in Markdown. Use exactly two headings: "
                "## What was discussed (2-5 sentences, plain) and "
                "## Patterns I noticed (2-4 bullets, direct, optional)."
            ),
        },
        "current_state_proposed": {
            "type": "string",
            "description": "Full new contents of 05_current_state.md if changes warranted, else empty string",
        },
        "current_state_rationale": {
            "type": "string",
            "description": "One sentence on why the update was proposed (or empty)",
        },
        "app_feedback": {
            "type": "array",
            "description": (
                "Quotes/observations from Liam ABOUT THE APP itself — UI gripes, "
                "model-behaviour complaints, missing features. Empty array if none."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "quote": {
                        "type": "string",
                        "description": "Short quote from Liam (≤25 words) or paraphrase",
                    },
                    "observation": {
                        "type": "string",
                        "description": "One sentence: what this tells us to consider for the app",
                    },
                },
                "required": ["quote", "observation"],
            },
        },
    },
    "required": [
        "title",
        "summary_markdown",
        "current_state_proposed",
        "current_state_rationale",
        "app_feedback",
    ],
}


def run_auditor(
    claude: ClaudeClient,
    prompts_dir: Path,
    model: str,
    inp: SummariserInput,
    max_tokens: int = 4096,
) -> AuditorReport:
    system = _auditor_system_prompt(prompts_dir)
    user_msg = _build_user_message(inp)

    raw = claude._c.messages.create(  # noqa: SLF001
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system}],
        messages=[{"role": "user", "content": user_msg}],
        tools=[
            {
                "name": "submit_audit_report",
                "description": "Submit your structured audit. Call exactly once.",
                "input_schema": AUDIT_TOOL_SCHEMA,
            }
        ],
        tool_choice={"type": "tool", "name": "submit_audit_report"},
    )

    tool_input: dict | None = None
    for block in raw.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_audit_report":
            tool_input = block.input
            break
    if tool_input is None:
        raise AuditorError("auditor did not call submit_audit_report tool")

    u = raw.usage
    usage = Usage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    return _normalise(tool_input, usage)


def _normalise(data: dict, usage: Usage) -> AuditorReport:
    title = str(data.get("title") or "").strip() or "untitled session"
    summary_markdown = str(data.get("summary_markdown") or "").strip()
    current_state_proposed = str(data.get("current_state_proposed") or "").strip()
    current_state_rationale = str(data.get("current_state_rationale") or "").strip()

    feedback: list[AppFeedback] = []
    raw_fb = data.get("app_feedback") or []
    if isinstance(raw_fb, list):
        for item in raw_fb:
            if not isinstance(item, dict):
                continue
            q = str(item.get("quote", "")).strip()
            obs = str(item.get("observation", "")).strip()
            if q or obs:
                feedback.append(AppFeedback(quote=q, observation=obs))

    return AuditorReport(
        title=title,
        summary_markdown=summary_markdown,
        current_state_proposed=current_state_proposed,
        current_state_rationale=current_state_rationale,
        app_feedback=feedback,
        usage=usage,
    )


def mock_auditor_report(inp: SummariserInput) -> AuditorReport:
    n_user = sum(1 for m in inp.messages if m.role == "user")
    body = (
        "## What was discussed\n"
        f"Local-mode mock summary. {n_user} user turns.\n\n"
        "## Patterns I noticed\n- Mock pattern — real auditor runs against Claude API."
    )
    return AuditorReport(
        title="Mock session",
        summary_markdown=body,
        current_state_proposed="",
        current_state_rationale="",
        app_feedback=[],
        usage=Usage(),
    )


# ---------------------------------------------------------------------------
# Side-effect helpers (write to disk)
# ---------------------------------------------------------------------------


def write_session_log(data_root: Path, session_id: str, title: str, body: str) -> Path:
    """Append a new session-log file under data_root/session-logs/."""
    logs_dir = data_root / "session-logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    date_part = session_id.split("T", 1)[0]
    slug = "-".join(w for w in title.lower().split() if w.isalnum() or "-" in w) or "session"
    slug = slug[:60].strip("-") or "session"
    base = f"{date_part}_{slug}"
    path = logs_dir / f"{base}.md"
    n = 1
    while path.exists():
        n += 1
        path = logs_dir / f"{base}-{n}.md"
    path.write_text(f"# {title}\n\n{body.strip()}\n", encoding="utf-8")
    return path


def write_current_state(data_root: Path, contents: str) -> None:
    """Replace 05_current_state.md."""
    p = data_root / "context" / "05_current_state.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    text = contents if contents.endswith("\n") else contents + "\n"
    p.write_text(text, encoding="utf-8")


def append_app_feedback(
    data_root: Path,
    session_id: str,
    items: list[AppFeedback],
) -> Path | None:
    """Append app-feedback entries to data_root/app-feedback.md. Returns path
    if anything was written, else None."""
    if not items:
        return None
    p = data_root / "app-feedback.md"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"\n## {ts} — session {session_id}\n"]
    for it in items:
        q = it.quote.strip()
        obs = it.observation.strip()
        if q:
            lines.append(f'- > "{q}"')
            if obs:
                lines.append(f"  - {obs}")
        elif obs:
            lines.append(f"- {obs}")
    block = "\n".join(lines) + "\n"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return p


# Re-export for tests/usage.
__all__ = [
    "AppFeedback",
    "AuditorError",
    "AuditorReport",
    "SummariserInput",
    "append_app_feedback",
    "mock_auditor_report",
    "run_auditor",
    "write_current_state",
    "write_session_log",
]
