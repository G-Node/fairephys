"""Subjects of a compiled odML document: one 'Subject', or several under 'Subjects'.

A session belongs to one subject: with several subjects its 'subject'
property matches that subject's 'subject id'.
"""

from __future__ import annotations

import odml

from ..odml import prop

def subject_sections(doc: odml.Document) -> list[odml.Section]:
    """All subject sections: the entries of 'Subjects', else the single 'Subject'."""
    for name in ("Subjects", "Subject"):
        try:
            section = doc[name]
        except KeyError:
            continue
        return list(section.sections) if name == "Subjects" else [section]
    return []


def session_subject(doc: odml.Document, session: odml.Section) -> odml.Section | None:
    """Subject section of a session (the only subject when there is one)."""
    try:
        subjects = list(doc["Subjects"].sections)
    except KeyError:
        return next(iter(subject_sections(doc)), None)
    sid = prop(session, "subject")
    for subject in subjects:
        if str(prop(subject, "subject id")) == str(sid):
            return subject
    raise ValueError(f"Session {prop(session, 'session id')}: subject {sid!r} is not in Subjects")
