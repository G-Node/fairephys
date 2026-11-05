"""Text templates of the registries ("{token}" placeholders) and label helpers."""

from __future__ import annotations

import re

# A template token "{name}", or an optional group "{?words {name} words}"
# (groups: words before, token, words after; plain token)
_TEMPLATE_TOKEN_RE = re.compile(r"\{\?([^{}]*)\{([^{}]+)\}([^{}]*)\}|\{([^{}]+)\}")
_EMPTY_OPTIONAL = "\x00"  # marks an empty optional template token while rendering
# An empty optional token, with parentheses that hold nothing else, and the whitespace before it
_EMPTY_OPTIONAL_RE = re.compile(r"\s*(?:\(\x00\)|\x00)")


def machine_label(text: str) -> str:
    """lookupLabel without whitespace ("..._Probe 1" -> "..._Probe-1")."""
    return re.sub(r"\s+", "-", text.strip())


def fill_template(template: str, lookup) -> str | None:
    """Fill the "{token}" placeholders of a text with lookup(token name).

    Values are stripped of surrounding whitespace (a YAML block text ends with
    a newline). A missing, empty or multi-valued token makes the whole result
    None. Optional tokens "{?token}" render as empty text instead: the
    whitespace separating an empty optional token from the text before it
    (or, at the start, from the text after it) is dropped, and so are
    parentheses that hold nothing else, so "{description}\\n\\n{?note}" equals
    the plain description when there is no note and "{name} ({?dataset citation})"
    the plain name. An optional group "{?words {token} words}" carries the
    words around one token and is left out with it by the same rules:
    "recordings{?, provided as {format},} were" renders "recordings, provided
    as NSx, were", or "recordings were" without a format. A result that ends
    up empty is None.
    """
    missing = []

    def text_of(name: str) -> str:
        val = lookup(name)
        return "" if val is None or isinstance(val, list) else str(val).strip()

    def fill(match: re.Match) -> str:
        before, group_token, after, token = match.groups()
        if group_token is not None:
            text = text_of(group_token)
            return f"{before}{text}{after}" if text else _EMPTY_OPTIONAL
        optional = token.startswith("?")
        text = text_of(token[1:] if optional else token)
        if not (text or optional):
            missing.append(token)
        return text or _EMPTY_OPTIONAL

    out = _TEMPLATE_TOKEN_RE.sub(fill, template)
    if missing:
        return None
    if _EMPTY_OPTIONAL in out:
        leading = _EMPTY_OPTIONAL_RE.match(out) is not None
        out = _EMPTY_OPTIONAL_RE.sub("", out)
        if leading:
            out = out.lstrip()
    return out or None
