"""Session entries of a compiled odML document and how one is found.

A session entry is identified like a BIDS recording (sub, ses, task): by its
'session id'; with several subjects also by its 'subject' (session ids can
repeat across subjects); and when one session holds recordings of several
tasks, one entry per task with the same session id, by its 'task'. Entries
without a 'task' record the dataset's task. Several runs of one task stay
one entry (its 'runs').
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import odml

from ..odml import prop, props, section_at
from .subjects import session_subject


def session_identity(doc: odml.Document, session: odml.Section) -> dict[str, str]:
    """What identifies a session entry (BIDS sub, ses, task), as text:
    subject_id (of the session's subject), session_id and task ("" when the
    entry names no task). Labels built from a session add the task only when
    it is set, so those of single-task sessions keep their form."""
    subject = session_subject(doc, session)
    return {"subject_id": str(prop(subject, "subject id") or ""),
            "session_id": str(prop(session, "session id") or ""),
            "task": str(prop(session, "task") or "")}


def find_sessions(doc: odml.Document, session_id: str, subject: str = None,
                  task: str = None) -> list[odml.Section]:
    """Session entries with this session id (and subject, when given).

    With a task: the entries of that task, else those that name no task
    (a session of the dataset's single task).
    """
    try:
        sections = doc["Sessions"].sections
    except KeyError:
        return []
    found = [s for s in sections
             if str(prop(s, "session id")) == str(session_id)
             and (not subject or str(prop(s, "subject")) == str(subject))]
    if task:
        found = ([s for s in found if prop(s, "task") == task]
                 or [s for s in found if prop(s, "task") in (None, "")])
    return found


def lookup_session(doc: odml.Document, session_id: str, subject: str = None,
                   task: str = None) -> odml.Section | None:
    """The session entry with this session id, subject and task (see
    find_sessions); None when there is none.

    Raises ValueError when several entries match, e.g. two tasks recorded in
    one session and no task given.
    """
    found = find_sessions(doc, session_id, subject, task)
    if len(found) > 1:
        which = ", ".join(f"subject {prop(s, 'subject')} task {prop(s, 'task')}" for s in found)
        raise ValueError(f"Session {session_id}: {len(found)} entries match ({which}); "
                         "select one by subject and task")
    return found[0] if found else None


def find_session(doc: odml.Document, session_id: str, subject: str = None,
                 task: str = None) -> odml.Section:
    """The session entry with this session id, subject and task (see
    find_sessions). Raises KeyError when there is none, ValueError when
    several match."""
    if (session := lookup_session(doc, session_id, subject, task)) is not None:
        return session
    msg = f"Session {session_id} not found"
    if subject:
        msg += f" for {subject}"
    if task:
        msg += f" task {task}"
    raise KeyError(msg)


def run_entry(session: odml.Section, run: int, stream_id: str = None) -> dict:
    """The run's entry in the session's Neo Metadata 'runs' ({} when the
    session has no run entries: its recording is one run), with the duration
    and the start time of `stream_id` in that run where they differ from the
    curated stream's.

    Raises KeyError when the session has run entries but none for this run.
    """
    runs = section_at(session, "Neo Metadata/runs")
    if runs is None:
        return {}
    for sec in runs.sections:
        if (entry := props(sec)).get("run") == run:
            own = section_at(sec, f"streams/{stream_id}") if stream_id else None
            return {**entry, **props(own)} if own is not None else entry
    entries = [props(s) for s in runs.sections]
    raise KeyError(f"Session {session.name}: no run {run} in Neo Metadata runs "
                   f"({[e.get('run') for e in entries]})")


def start_datetime(start, day: date, tz_name: str | None) -> datetime:
    """A start time (a datetime, a time of day, or their ISO text) on `day`
    as an aware datetime: one with an offset (e.g. UTC from a raw header) in
    the Dataset time zone `tz_name`, one without taken as local time in it
    (UTC when no time zone is set)."""
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    if isinstance(start, datetime):
        dt = start
    elif isinstance(start, time):
        dt = datetime.combine(day, start)
    else:
        try:
            dt = datetime.fromisoformat(str(start))
        except ValueError:
            dt = datetime.combine(day, time.fromisoformat(str(start)))
    return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)


def first_sample_time(doc: odml.Document, session: odml.Section, run: int = None,
                      stream: str = None) -> datetime | None:
    """The time of the first sample of the session's recording (of the run's,
    from Neo Metadata 'runs', when the session holds several; of `stream`'s
    in that run when it started at another time), in the Dataset time zone;
    None when the session has no date or no start time."""
    entry = run_entry(session, run, stream) if run is not None else {}
    start = entry.get("start_time") or prop(session, "start time")
    day = prop(session, "date")
    if start in (None, "") or day in (None, ""):
        return None
    day = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    day = day.date() if isinstance(day, datetime) else day
    return start_datetime(start, day, prop(section_at(doc, "Dataset"), "time zone"))
