"""
Google Calendar tools.

Matches the Bri|Trisha|therapy regex to derive days-since-last-human-therapy
for the home-page counter (sourced from Calendar, not self-reported — per
outside-voice feedback D).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import structlog
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.google_auth import GoogleAuthConfig, load_credentials
from app.tools.registry import ToolError, ToolSpec

log = structlog.get_logger()

BRISBANE = ZoneInfo("Australia/Brisbane")

THERAPY_REGEX = re.compile(r"\b(bri|trisha|therapy|therapist|counsel+or|counsel+ing)\b", re.IGNORECASE)


def _cal_service(cfg: GoogleAuthConfig):
    creds = load_credentials(cfg)
    if creds is None:
        raise ToolError(
            "Calendar is not connected. Visit /connect-gmail to authorise."
        )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# list_calendar_events
# ---------------------------------------------------------------------------


def _list_calendar_events_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        start = args.get("start")
        end = args.get("end")
        if not start or not end:
            raise ToolError("start and end (ISO 8601) are both required")
        svc = _cal_service(cfg)
        try:
            resp = (
                svc.events()
                .list(
                    calendarId="primary",
                    timeMin=start,
                    timeMax=end,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=100,
                )
                .execute()
            )
        except HttpError as e:
            raise ToolError(f"Calendar API error: {e}") from e
        events = resp.get("items", [])
        if not events:
            return f"No events between {start} and {end}."
        out: list[str] = []
        for ev in events:
            s = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            e = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")
            summary = ev.get("summary", "(no title)")
            out.append(f"- event_id={ev['id']}  {s} → {e}  {summary!r}")
        return "\n".join(out)

    return _h


def list_calendar_events_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="list_calendar_events",
        description=(
            "List events from Liam's primary calendar between start and end (ISO 8601). "
            "Returns event summaries with their IDs for potential update/reference."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "ISO 8601 start"},
                "end": {"type": "string", "description": "ISO 8601 end"},
            },
            "required": ["start", "end"],
        },
        handler=_list_calendar_events_handler(cfg),
    )


# ---------------------------------------------------------------------------
# create_calendar_event
# ---------------------------------------------------------------------------


def _create_calendar_event_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        title = args.get("title")
        start = args.get("start")
        end = args.get("end")
        description = args.get("description", "")
        attendees = args.get("attendees") or []
        if not (title and start and end):
            raise ToolError("title, start, end are all required")
        svc = _cal_service(cfg)
        body: dict = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start, "timeZone": "Australia/Brisbane"},
            "end": {"dateTime": end, "timeZone": "Australia/Brisbane"},
        }
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        try:
            ev = svc.events().insert(calendarId="primary", body=body).execute()
        except HttpError as e:
            raise ToolError(f"Calendar API error: {e}") from e
        return f"Created event_id={ev.get('id')} — {ev.get('htmlLink','')}"

    return _h


def create_calendar_event_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="create_calendar_event",
        description=(
            "Create an event on Liam's primary calendar. Times in ISO 8601 "
            "(Australia/Brisbane timezone). Use when he asks to book something."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601"},
                "end": {"type": "string", "description": "ISO 8601"},
                "description": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "start", "end"],
        },
        handler=_create_calendar_event_handler(cfg),
    )


# ---------------------------------------------------------------------------
# update_calendar_event
# ---------------------------------------------------------------------------


def _update_calendar_event_handler(cfg: GoogleAuthConfig) -> Callable:
    def _h(args: dict):
        event_id = args.get("event_id")
        if not event_id:
            raise ToolError("event_id required")
        svc = _cal_service(cfg)
        try:
            existing = svc.events().get(calendarId="primary", eventId=event_id).execute()
        except HttpError as e:
            raise ToolError(f"Calendar API error: {e}") from e

        updates: dict = {}
        if args.get("title"):
            updates["summary"] = args["title"]
        if args.get("description") is not None:
            updates["description"] = args["description"]
        if args.get("start"):
            updates["start"] = {"dateTime": args["start"], "timeZone": "Australia/Brisbane"}
        if args.get("end"):
            updates["end"] = {"dateTime": args["end"], "timeZone": "Australia/Brisbane"}
        if not updates:
            return "No changes supplied."
        existing.update(updates)
        try:
            ev = svc.events().update(calendarId="primary", eventId=event_id, body=existing).execute()
        except HttpError as e:
            raise ToolError(f"Calendar API error: {e}") from e
        return f"Updated event_id={ev.get('id')}"

    return _h


def update_calendar_event_spec(cfg: GoogleAuthConfig) -> ToolSpec:
    return ToolSpec(
        name="update_calendar_event",
        description=(
            "Update an existing event on the primary calendar. Only supplied "
            "fields are changed. Times in ISO 8601 (Australia/Brisbane)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["event_id"],
        },
        handler=_update_calendar_event_handler(cfg),
    )


# ---------------------------------------------------------------------------
# Days-since-human-therapy — sourced from Calendar, not self-report
# ---------------------------------------------------------------------------


def days_since_last_therapy(cfg: GoogleAuthConfig, today: date | None = None) -> int | None:
    """Return days since the most recent Calendar event matching THERAPY_REGEX
    that is in the past. Returns None if Calendar isn't connected or no matches.
    """
    if today is None:
        today = datetime.now(BRISBANE).date()
    creds = load_credentials(cfg)
    if creds is None:
        return None
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    # Look back up to 180 days.
    time_min = (today - timedelta(days=180)).isoformat() + "T00:00:00Z"
    time_max = today.isoformat() + "T23:59:59Z"
    try:
        resp = (
            svc.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )
    except HttpError as e:
        log.warning("calendar.days_since.error", error=str(e))
        return None

    events = resp.get("items", [])
    last_match: date | None = None
    for ev in events:
        title = ev.get("summary") or ""
        if not THERAPY_REGEX.search(title):
            continue
        start = ev.get("start", {})
        start_str = start.get("dateTime") or start.get("date")
        if not start_str:
            continue
        try:
            if "T" in start_str:
                d = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(BRISBANE).date()
            else:
                d = date.fromisoformat(start_str)
        except ValueError:
            continue
        if d <= today:
            if last_match is None or d > last_match:
                last_match = d

    if last_match is None:
        return None
    return (today - last_match).days
