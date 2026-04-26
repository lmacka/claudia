"""
Gmail tools.

All tools fail cleanly if not authenticated — they raise ToolError with a message
the model can surface to Liam, prompting him to /connect-gmail.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Callable

import structlog
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.google_auth import GoogleAuthConfig, load_credentials
from app.tools.registry import ToolError, ToolSpec

log = structlog.get_logger()


def _gmail_service(cfg: GoogleAuthConfig):
    creds = load_credentials(cfg)
    if creds is None:
        raise ToolError(
            "Gmail is not connected. Tell Liam to visit /connect-gmail in a browser to authorise."
        )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# search_gmail
# ---------------------------------------------------------------------------


def _search_gmail_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        query = args.get("query")
        max_results = int(args.get("max_results") or 20)
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query is required (string)")

        svc = _gmail_service(cfg)
        try:
            resp = (
                svc.users()
                .threads()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
        except HttpError as e:
            raise ToolError(f"Gmail API error: {e}") from e

        threads = resp.get("threads", [])
        if not threads:
            return f"No threads match {query!r}."

        out_lines: list[str] = [f"Found {len(threads)} thread(s):"]
        for t in threads:
            # Peek at first message snippet
            try:
                thread_detail = (
                    svc.users()
                    .threads()
                    .get(userId="me", id=t["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"])
                    .execute()
                )
                msgs = thread_detail.get("messages", [])
                if not msgs:
                    continue
                headers = {h["name"]: h["value"] for h in msgs[0]["payload"].get("headers", [])}
                out_lines.append(
                    f"- thread_id={t['id']}  "
                    f"from={headers.get('From','?')!r}  "
                    f"subject={headers.get('Subject','(no subject)')!r}  "
                    f"date={headers.get('Date','?')!r}  "
                    f"msgs={len(msgs)}"
                )
            except HttpError:
                out_lines.append(f"- thread_id={t['id']}  (metadata fetch failed)")
        return "\n".join(out_lines)

    return _h


def search_gmail_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="search_gmail",
        description=(
            "Search Liam's Gmail using Gmail's native search syntax "
            "(e.g. 'from:bri@reravel.com.au after:2026/01/01'). Returns thread IDs "
            "with summary headers. Use get_gmail_thread to fetch full content."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max threads (default 20)"},
            },
            "required": ["query"],
        },
        handler=_search_gmail_handler(cfg),
    )


# ---------------------------------------------------------------------------
# get_gmail_thread
# ---------------------------------------------------------------------------


def _decode_body(payload: dict) -> str:
    """Best-effort text extraction from a Gmail message payload."""
    parts = payload.get("parts") or []
    if payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="replace"
        )
    # Prefer text/plain part
    for p in parts:
        if p.get("mimeType") == "text/plain":
            return _decode_body(p)
    for p in parts:
        if p.get("mimeType", "").startswith("multipart/"):
            t = _decode_body(p)
            if t:
                return t
    # Fallback: first text/* part
    for p in parts:
        if p.get("mimeType", "").startswith("text/"):
            return _decode_body(p)
    return ""


def _get_gmail_thread_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        tid = args.get("thread_id")
        if not isinstance(tid, str) or not tid:
            raise ToolError("thread_id is required (string)")
        svc = _gmail_service(cfg)
        try:
            thread = svc.users().threads().get(userId="me", id=tid, format="full").execute()
        except HttpError as e:
            raise ToolError(f"Gmail API error: {e}") from e
        msgs = thread.get("messages", [])
        if not msgs:
            return "(empty thread)"
        out: list[str] = []
        for m in msgs:
            headers = {h["name"]: h["value"] for h in m["payload"].get("headers", [])}
            body = _decode_body(m["payload"]).strip()
            out.append(
                f"--- message_id={m['id']}\n"
                f"from: {headers.get('From','?')}\n"
                f"to: {headers.get('To','?')}\n"
                f"date: {headers.get('Date','?')}\n"
                f"subject: {headers.get('Subject','(no subject)')}\n\n{body}"
            )
        return "\n\n".join(out)

    return _h


def get_gmail_thread_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="get_gmail_thread",
        description=(
            "Fetch the full text of a Gmail thread by its thread_id "
            "(returned by search_gmail). Returns all messages with headers."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
            },
            "required": ["thread_id"],
        },
        handler=_get_gmail_thread_handler(cfg),
    )


# ---------------------------------------------------------------------------
# get_gmail_message (with attachments listing)
# ---------------------------------------------------------------------------


def _get_gmail_message_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        mid = args.get("message_id")
        if not isinstance(mid, str) or not mid:
            raise ToolError("message_id is required (string)")
        svc = _gmail_service(cfg)
        try:
            m = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as e:
            raise ToolError(f"Gmail API error: {e}") from e
        headers = {h["name"]: h["value"] for h in m["payload"].get("headers", [])}
        body = _decode_body(m["payload"]).strip()
        attachments = _list_attachments(m["payload"])

        lines = [
            f"message_id: {m['id']}",
            f"from: {headers.get('From','?')}",
            f"to: {headers.get('To','?')}",
            f"date: {headers.get('Date','?')}",
            f"subject: {headers.get('Subject','(no subject)')}",
            "",
            body,
            "",
            "attachments:",
        ]
        if not attachments:
            lines.append("(none)")
        else:
            for a in attachments:
                lines.append(
                    f"- filename={a['filename']!r} mime={a['mimeType']} size={a['size']} attachment_id={a['attachment_id']}"
                )
        return "\n".join(lines)

    return _h


def _list_attachments(payload: dict) -> list[dict]:
    out: list[dict] = []
    for part in payload.get("parts", []) or []:
        body = part.get("body", {}) or {}
        filename = part.get("filename", "") or ""
        aid = body.get("attachmentId")
        if aid and filename:
            out.append(
                {
                    "filename": filename,
                    "mimeType": part.get("mimeType", "application/octet-stream"),
                    "size": body.get("size", 0),
                    "attachment_id": aid,
                }
            )
        if part.get("parts"):
            out.extend(_list_attachments(part))
    return out


def get_gmail_message_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="get_gmail_message",
        description=(
            "Fetch a specific Gmail message by message_id. Returns headers, body, "
            "and a list of attachments (with attachment_id for save_gmail_attachment)."
        ),
        input_schema={
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
        handler=_get_gmail_message_handler(cfg),
    )


# ---------------------------------------------------------------------------
# save_gmail_attachment
# ---------------------------------------------------------------------------


_MIME_TO_SUBDIR = {
    "application/pdf": "pdfs",
    "image/png": "images",
    "image/jpeg": "images",
    "image/jpg": "images",
    "image/gif": "images",
    "image/webp": "images",
    "message/rfc822": "emails",
}


def _save_gmail_attachment_handler(cfg: GoogleAuthConfig, data_root: Path) -> Callable:
    def _h(args: dict):
        mid = args.get("message_id")
        aid = args.get("attachment_id")
        filename = args.get("filename", "attachment")
        if not isinstance(mid, str) or not mid:
            raise ToolError("message_id required")
        if not isinstance(aid, str) or not aid:
            raise ToolError("attachment_id required")

        svc = _gmail_service(cfg)
        try:
            res = (
                svc.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=mid, id=aid)
                .execute()
            )
        except HttpError as e:
            raise ToolError(f"Gmail API error: {e}") from e

        data_b64 = res.get("data")
        if not data_b64:
            raise ToolError("attachment is empty")
        data = base64.urlsafe_b64decode(data_b64 + "=" * (-len(data_b64) % 4))

        # Pick subdir from filename suffix
        suffix = Path(filename).suffix.lower()
        mime_guess = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".eml": "message/rfc822",
        }.get(suffix, "application/octet-stream")
        subdir = _MIME_TO_SUBDIR.get(mime_guess, "files")

        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-.") or "attachment"
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        dest_dir = data_root / "uploads" / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{ts}_{safe}"
        dest.write_bytes(data)

        # Rebuild index to include the new file
        from app.tools.documents import rebuild_index

        try:
            rebuild_index(data_root)
        except OSError:
            pass

        rel = str(dest.relative_to(data_root))
        return f"Saved {len(data)} bytes to {rel}"

    return _h


def save_gmail_attachment_spec(cfg: GoogleAuthConfig, data_root: Path) -> ToolSpec:
    return ToolSpec(
        name="save_gmail_attachment",
        description=(
            "Download a Gmail attachment and save it under /data/uploads/. "
            "Use message_id + attachment_id from get_gmail_message. Returns the "
            "saved path (relative to /data) which you can then read via read_document."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "filename": {"type": "string", "description": "Original filename"},
            },
            "required": ["message_id", "attachment_id", "filename"],
        },
        handler=_save_gmail_attachment_handler(cfg, data_root),
    )


# ---------------------------------------------------------------------------
# create_gmail_draft — DRAFT ONLY, NEVER SENDS
# ---------------------------------------------------------------------------


def _create_gmail_draft_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        to = args.get("to")
        subject = args.get("subject", "")
        body = args.get("body", "")
        thread_id = args.get("thread_id")

        if not isinstance(to, str) or "@" not in to:
            raise ToolError("to must be a valid email address")

        from email.mime.text import MIMEText

        msg = MIMEText(body, "plain", "utf-8")
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        draft_body = {"message": {"raw": raw}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        svc = _gmail_service(cfg)
        try:
            res = svc.users().drafts().create(userId="me", body=draft_body).execute()
        except HttpError as e:
            raise ToolError(f"Gmail API error: {e}") from e
        return f"Draft created: draft_id={res.get('id')}  (NOT sent — review in Gmail before sending)"

    return _h


def create_gmail_draft_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="create_gmail_draft",
        description=(
            "Create a Gmail draft (NEVER sends). Use when Liam asks you to draft an email. "
            "Returns the draft_id for reference. Liam reviews and sends from Gmail himself."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Email address"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body"},
                "thread_id": {
                    "type": "string",
                    "description": "Optional — reply within this thread",
                },
            },
            "required": ["to", "body"],
        },
        handler=_create_gmail_draft_handler(cfg),
    )


# ---------------------------------------------------------------------------
# propose_promote_upload (approval-gated; state-mutating)
# ---------------------------------------------------------------------------


def _propose_promote_upload_handler(data_root: Path, append_event: Callable) -> Callable:
    def _h(args: dict):
        upload_path = args.get("upload_path", "")
        destination = args.get("destination", "")
        rationale = args.get("rationale", "")
        if not isinstance(upload_path, str) or not upload_path.startswith("uploads/"):
            raise ToolError("upload_path must start with 'uploads/'")
        if not isinstance(destination, str) or not destination.startswith("context/source-material/"):
            raise ToolError("destination must start with 'context/source-material/'")
        # Just record as a pending mutation event — the approval UI picks it up.
        append_event(
            "pending_promote_upload",
            {
                "upload_path": upload_path,
                "destination": destination,
                "rationale": rationale,
            },
        )
        return (
            f"Promotion proposed ({upload_path} → {destination}). "
            "Will appear in session-end approval UI."
        )

    return _h


def propose_promote_upload_spec(data_root: Path, append_event_for_session: Callable) -> ToolSpec:
    return ToolSpec(
        name="propose_promote_upload",
        description=(
            "Propose moving a file from uploads/ to context/source-material/. "
            "Use when a saved upload turns out to be canonical context worth keeping. "
            "Approval-gated — the move happens only if Liam approves at session end."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "upload_path": {"type": "string", "description": "e.g. uploads/pdfs/foo.pdf"},
                "destination": {
                    "type": "string",
                    "description": "e.g. context/source-material/foo.pdf",
                },
                "rationale": {"type": "string", "description": "One-line reason"},
            },
            "required": ["upload_path", "destination", "rationale"],
        },
        handler=_propose_promote_upload_handler(data_root, append_event_for_session),
    )
