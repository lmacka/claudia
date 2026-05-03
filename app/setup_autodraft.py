"""Auto-draft the setup wizard's profile fields from uploaded library docs.

User uploads psych assessments / school reports / journals on /setup/2,
clicks "Auto-draft profile from my documents", and we run a Sonnet call
that summarises the docs into four paragraphs the wizard then pre-fills:

    who         - who they are right now
    stressors   - what they're navigating right now
    never       - things claudia should never do for them
    for         - what claudia is for in their life

The user can edit any of the four before committing /setup/3.
"""

from __future__ import annotations

import re

import structlog

from app.claude import ClaudeClient
from app.library import Library

log = structlog.get_logger()


_SYSTEM_PROMPT = (
    "You are drafting a private profile for a user-facing AI companion. The "
    "user just uploaded documents (psych assessments, school reports, journals, "
    "plans). Read them and produce four short paragraphs for the user to "
    "review and edit. Be specific to what's in the documents. Do not invent "
    "things the documents do not say. If a section has no relevant evidence, "
    "leave it short and obviously a default."
)


_USER_TEMPLATE = (
    "Documents:\n\n{docs}\n\n"
    "---\n\n"
    "Draft four sections for the wizard. Each is one paragraph (≤4 sentences). "
    "Use plain English in second person where natural; the user IS the subject "
    "of the documents.\n\n"
    "Return ONLY this exact format, no preamble:\n\n"
    "WHO: <one paragraph: who they are right now — diagnoses, role, life stage, "
    "core traits the documents describe>\n\n"
    "STRESSORS: <one paragraph: what they're navigating right now — recent "
    "challenges, transitions, ongoing pressures the documents flag>\n\n"
    "NEVER: <one paragraph: what claudia should never do — based on patterns "
    "in the documents about how the user wants to be supported. If the documents "
    "don't speak to this, default to: 'Don't moralise, don't push solutions, "
    "don't pretend to feel things.'>\n\n"
    "FOR: <one paragraph: what claudia is for — the kind of thinking-partner "
    "support that would help, given what the documents reveal>"
)


def _build_doc_blob(library: Library, max_docs: int = 10, max_chars_per_doc: int = 8000) -> str:
    """Concatenate up to N most-recent active doc extracts into one string."""
    docs = library.list_active()[:max_docs]
    blocks: list[str] = []
    for d in docs:
        text = (library.get_extracted(d.id) or "").strip()
        if not text:
            continue
        if len(text) > max_chars_per_doc:
            text = text[:max_chars_per_doc] + "\n\n[…document truncated for prompt…]"
        date_label = d.original_date.isoformat() if d.original_date else "date unknown"
        blocks.append(f"## {d.title} ({d.kind}, {date_label})\n\n{text}")
    return "\n\n---\n\n".join(blocks)


def _parse_sections(text: str) -> dict[str, str]:
    """Parse the WHO/STRESSORS/NEVER/FOR labels into a dict."""
    out: dict[str, str] = {}
    # Match labels at the start of a line; capture everything until the next
    # label or end of string.
    pattern = re.compile(
        r"^(?P<label>WHO|STRESSORS|NEVER|FOR):\s*(?P<body>.+?)(?=^\s*(?:WHO|STRESSORS|NEVER|FOR):|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    for m in pattern.finditer(text):
        label = m.group("label").lower()
        body = m.group("body").strip()
        out[f"section_{label}"] = body
    return out


def auto_draft_profile(
    claude: ClaudeClient,
    model: str,
    library: Library,
) -> dict[str, str]:
    """Run the LLM pass and return {section_who, section_stressors, section_never, section_for}.

    Returns {} if the library has no extractable text. Raises on Anthropic API
    failure — caller is responsible for surfacing a friendly error.
    """
    docs_blob = _build_doc_blob(library)
    if not docs_blob:
        log.info("setup.autodraft.no_docs")
        return {}

    user_prompt = _USER_TEMPLATE.format(docs=docs_blob)
    reply = claude.single_turn(
        model=model,
        user_content=[{"type": "text", "text": user_prompt}],
        system=_SYSTEM_PROMPT,
        max_tokens=2048,
    )
    sections = _parse_sections(reply.text)
    log.info(
        "setup.autodraft.completed",
        sections_drafted=sorted(sections.keys()),
        in_tokens=reply.usage.input_tokens,
        out_tokens=reply.usage.output_tokens,
    )
    return sections
