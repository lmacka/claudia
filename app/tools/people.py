"""
People tools — list_people / lookup_person / search_people.

These are the runtime companion tools (different from app/people.py which
owns the data layer). The auditor's people_updates schema lives in
app/summariser.py.
"""

from __future__ import annotations

import structlog

from app.library import Library
from app.people import People
from app.tools.registry import ToolError, ToolSpec

log = structlog.get_logger()


def list_people_spec(people: People) -> ToolSpec:
    def handler(_args: dict) -> str:
        return people.render_people_md()

    return ToolSpec(
        name="list_people",
        description=(
            "Returns the active people roster as markdown. The same roster "
            "is included in block 2 of your system prompt at session start; "
            "use this tool to refresh if you've been chatting for a while."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=handler,
    )


def lookup_person_spec(people: People, library: Library) -> ToolSpec:
    def handler(args: dict) -> str:
        identifier = (args.get("id_or_name") or "").strip()
        if not identifier:
            raise ToolError("id_or_name is required")

        meta = people.get(identifier)
        if meta is None:
            meta = people.find_near_match(identifier, max_distance=2)
        if meta is None:
            raise ToolError(f"no person found matching {identifier!r}")

        # Bump last_mentioned (best-effort; never fail the lookup).
        try:
            people.touch(meta.id)
        except Exception as e:  # noqa: BLE001
            log.warning("people.lookup.touch_failed", person_id=meta.id, error=str(e))

        notes = people.get_notes(meta.id) or ""

        linked_titles: list[str] = []
        for doc_id in meta.linked_documents:
            doc_meta = library.get(doc_id)
            if doc_meta is not None:
                linked_titles.append(f"- {doc_meta.title} (`{doc_id}`)")

        out: list[str] = [f"# {meta.name}"]
        if meta.aliases:
            out.append(f"_aliases: {', '.join(meta.aliases)}_")
        out.append(f"**Category:** {meta.category}")
        if meta.relationship:
            out.append(f"**Relationship:** {meta.relationship}")
        if meta.summary:
            out.append("")
            out.append(meta.summary)
        if meta.important_context:
            out.append("")
            out.append("## Important context")
            out.extend(f"- {bullet}" for bullet in meta.important_context)
        if notes.strip():
            out.append("")
            out.append("## Notes")
            out.append(notes.strip())
        if linked_titles:
            out.append("")
            out.append("## Linked documents")
            out.extend(linked_titles)
        return "\n".join(out)

    return ToolSpec(
        name="lookup_person",
        description=(
            "Get the full record for one person — meta, notes, and titles "
            "of every linked document. Call this when a conversation focuses "
            "on a specific person. Bumps that person's last-mentioned timestamp."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_name": {
                    "type": "string",
                    "description": "Person id (e.g. 'rhiannon-ohara') or full name. Near-name matches by Levenshtein distance ≤2.",
                }
            },
            "required": ["id_or_name"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def search_people_spec(people: People) -> ToolSpec:
    def handler(args: dict) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return "no results — empty query"
        matches = people.search(query)
        if not matches:
            return f"no people match {query!r}"
        return "\n".join(
            f"- **{m.name}** (`{m.id}`) — {m.category}"
            + (f" — {m.summary}" if m.summary else "")
            for m in matches
        )

    return ToolSpec(
        name="search_people",
        description=(
            "Substring search across name, aliases, tags, summary, and notes. "
            "Use when the user refers to someone obliquely ('the woman from the "
            "school assessment')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query. Substring match, case-insensitive.",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )
