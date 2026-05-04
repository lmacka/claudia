"""
Session-end auditor.

Runs after every session in a background task. Writes a session-log,
optionally rewrites 05_current_state.md, and appends any app-feedback the
auditor extracted from Liam's complaints/observations during the chat.

Forced tool-use guarantees structured output — never returns text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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
class PeopleUpdate:
    """One change to apply to /data/people/. add | update | touch."""

    action: str  # "add" | "update" | "touch"
    name: str = ""  # required for "add"
    id: str = ""  # required for "update" / "touch"
    category: str = ""
    summary: str = ""
    relationship: str = ""
    aliases: list[str] = field(default_factory=list)
    important_context: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    append_note: str = ""


@dataclass
class AuditorReport:
    title: str
    summary_markdown: str
    current_state_proposed: str  # full replacement, "" if no change
    current_state_rationale: str
    app_feedback: list[AppFeedback]
    people_updates: list[PeopleUpdate]
    usage: Usage


@dataclass
class SummariserInput:
    session_id: str
    messages: list[Message]
    current_state: str
    recent_session_logs: str = ""
    timezone_now: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _auditor_system_prompt(
    prompts_dir: Path,
    mode: str = "adult",
    display_name: str = "",
    parent_display_name: str = "",
) -> str:
    """Load auditor-{mode}.md and substitute display-name placeholders.

    Mode is "adult" or "kid" — files are auditor-adult.md / auditor-kid.md.
    """
    candidate = prompts_dir / f"auditor-{mode}.md"
    text = candidate.read_text(encoding="utf-8")
    if mode == "kid":
        text = (
            text
            .replace("{{DISPLAY_NAME}}", display_name or "the kid")
            .replace("{{PARENT_DISPLAY_NAME}}", parent_display_name or "your parent")
        )
    return text


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
        "people_updates": {
            "type": "array",
            "description": (
                "Updates to apply to the /people store after this session. "
                "Use 'add' for a brand-new person mentioned for the first time, "
                "'update' for changes/notes on an existing record (id required), "
                "or 'touch' to bump last_mentioned without other changes. "
                "Empty array if no people-related changes."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update", "touch"]},
                    "name": {"type": "string", "description": "Required for 'add'."},
                    "id": {
                        "type": "string",
                        "description": "Existing person id, required for 'update' and 'touch'.",
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "co-parent", "family", "partner", "friend",
                            "professional", "child", "colleague", "other",
                        ],
                    },
                    "summary": {"type": "string"},
                    "relationship": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "important_context": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "append_note": {
                        "type": "string",
                        "description": "Markdown paragraph to append to notes.md.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    "required": [
        "title",
        "summary_markdown",
        "current_state_proposed",
        "current_state_rationale",
        "app_feedback",
        "people_updates",
    ],
}


def run_auditor(
    claude: ClaudeClient,
    prompts_dir: Path,
    model: str,
    inp: SummariserInput,
    max_tokens: int = 4096,
    mode: str = "adult",
    display_name: str = "",
    parent_display_name: str = "",
) -> AuditorReport:
    system = _auditor_system_prompt(
        prompts_dir,
        mode=mode,
        display_name=display_name,
        parent_display_name=parent_display_name,
    )
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

    people_updates: list[PeopleUpdate] = []
    raw_pu = data.get("people_updates") or []
    if isinstance(raw_pu, list):
        for item in raw_pu:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "")).strip()
            if action not in ("add", "update", "touch"):
                continue
            people_updates.append(
                PeopleUpdate(
                    action=action,
                    name=str(item.get("name", "")).strip(),
                    id=str(item.get("id", "")).strip(),
                    category=str(item.get("category", "")).strip(),
                    summary=str(item.get("summary", "")).strip(),
                    relationship=str(item.get("relationship", "")).strip(),
                    aliases=[
                        str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()
                    ],
                    important_context=[
                        str(c).strip() for c in (item.get("important_context") or []) if str(c).strip()
                    ],
                    tags=[
                        str(t).strip() for t in (item.get("tags") or []) if str(t).strip()
                    ],
                    append_note=str(item.get("append_note", "")).strip(),
                )
            )

    return AuditorReport(
        title=title,
        summary_markdown=summary_markdown,
        current_state_proposed=current_state_proposed,
        current_state_rationale=current_state_rationale,
        app_feedback=feedback,
        people_updates=people_updates,
        usage=usage,
    )


def apply_people_updates(people, updates: list[PeopleUpdate]) -> list[dict]:
    """
    Apply auditor's people_updates to the People store. Near-name matches
    on 'add' get merged into 'update' against the existing record (alias
    appended) instead of duplicating.

    Returns a structured list describing what was actually applied, suitable
    for logging in the session-log.
    """
    applied: list[dict] = []
    for u in updates:
        try:
            if u.action == "add":
                if not u.name:
                    log.warning("auditor.people_add_missing_name")
                    continue
                # Near-match: merge into existing record instead of duplicating.
                near = people.find_near_match(u.name, max_distance=2)
                if near is not None:
                    fields: dict = {}
                    if u.name not in [near.name] + near.aliases:
                        fields["aliases"] = list({*near.aliases, u.name})
                    if u.summary and not near.summary:
                        fields["summary"] = u.summary
                    if u.category and near.category == "other":
                        fields["category"] = u.category
                    if u.relationship and not near.relationship:
                        fields["relationship"] = u.relationship
                    if u.important_context:
                        fields["important_context"] = list(
                            {*near.important_context, *u.important_context}
                        )
                    if u.tags:
                        fields["tags"] = list({*near.tags, *u.tags})
                    if fields:
                        people.update(near.id, **fields)
                    if u.append_note:
                        people.append_note(near.id, u.append_note)
                    applied.append(
                        {"action": "merged_into_existing", "id": near.id, "matched": u.name}
                    )
                else:
                    new_id = people.add(
                        name=u.name,
                        category=(u.category or "other"),  # type: ignore[arg-type]
                        relationship=u.relationship,
                        summary=u.summary,
                        important_context=u.important_context,
                        tags=u.tags,
                        aliases=u.aliases,
                        notes=u.append_note,
                    )
                    applied.append({"action": "added", "id": new_id, "name": u.name})

            elif u.action == "update":
                if not u.id:
                    log.warning("auditor.people_update_missing_id")
                    continue
                meta = people.get(u.id)
                if meta is None:
                    log.warning("auditor.people_update_unknown_id", id=u.id)
                    continue
                fields: dict = {}
                if u.summary:
                    fields["summary"] = u.summary
                if u.relationship:
                    fields["relationship"] = u.relationship
                if u.category:
                    fields["category"] = u.category
                if u.aliases:
                    fields["aliases"] = list({*meta.aliases, *u.aliases})
                if u.important_context:
                    fields["important_context"] = list(
                        {*meta.important_context, *u.important_context}
                    )
                if u.tags:
                    fields["tags"] = list({*meta.tags, *u.tags})
                if fields:
                    people.update(u.id, **fields)
                if u.append_note:
                    people.append_note(u.id, u.append_note)
                applied.append({"action": "updated", "id": u.id})

            elif u.action == "touch":
                if not u.id or people.get(u.id) is None:
                    log.warning("auditor.people_touch_missing", id=u.id)
                    continue
                people.touch(u.id)
                applied.append({"action": "touched", "id": u.id})

        except Exception as e:  # noqa: BLE001
            log.error(
                "auditor.people_update_failed",
                action=u.action,
                id=u.id,
                name=u.name,
                error=str(e),
            )
    return applied


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
        people_updates=[],
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


def _report_to_dict(session_id: str, report: AuditorReport) -> dict:
    return {
        "session_id": session_id,
        "title": report.title,
        "summary_markdown": report.summary_markdown,
        "current_state_proposed": report.current_state_proposed,
        "current_state_rationale": report.current_state_rationale,
        "app_feedback": [
            {"quote": f.quote, "observation": f.observation} for f in report.app_feedback
        ],
        "people_updates": [
            {
                "action": u.action,
                "name": u.name,
                "id": u.id,
                "category": u.category,
                "summary": u.summary,
                "relationship": u.relationship,
                "aliases": list(u.aliases),
                "important_context": list(u.important_context),
                "tags": list(u.tags),
                "append_note": u.append_note,
            }
            for u in report.people_updates
        ],
        "written_at": datetime.now(UTC).isoformat(),
    }


def write_audit_sidecar(
    data_root: Path,
    session_id: str,
    report: AuditorReport,
) -> None:
    """Persist an AuditorReport for /review. Writes to the audit_reports
    table (T-NEW-I phase 2). Replaces the per-session JSON file in
    /data/audit-sidecars/."""
    from app.db_audit import save_audit_report

    save_audit_report(data_root, session_id, _report_to_dict(session_id, report))


def read_audit_sidecar(data_root: Path, session_id: str) -> dict | None:
    """Load the saved report for /session/<id>/review. None if no audit yet."""
    from app.db_audit import load_audit_report

    return load_audit_report(data_root, session_id)


def append_app_feedback(
    data_root: Path,
    session_id: str,
    items: list[AppFeedback],
) -> int | None:
    """Insert app-feedback rows. Returns count inserted, or None if empty input."""
    if not items:
        return None
    from app.db_audit import append_app_feedback as _db_append

    return _db_append(
        data_root,
        session_id,
        [{"quote": it.quote.strip(), "observation": it.observation.strip()} for it in items],
    )


# Re-export for tests/usage.
__all__ = [
    "read_audit_sidecar",
    "write_audit_sidecar",
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
