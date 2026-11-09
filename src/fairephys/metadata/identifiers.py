"""Identifiers of related publications: their type and normalised value.

The types are those of DataCite's relatedIdentifierType that an entry of
dataset.yaml 'Related Publications' can name, read from the form of the
identifier: a DOI (bare 10.xxxx/..., doi:10.xxxx/... or a doi.org URL), an
arXiv id (arxiv:), a PubMed id (pmid:), a Handle (hdl: or a hdl.handle.net
URL), an ISBN (isbn:) or a URL (http(s)://). Any other form is an error.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import odml

from ..odml import prop, subsection

# The Dataset subsection of the entries, one subsection each (Related Publications_1, ...)
SECTION = "Related Publications"
FORMS = ("a DOI (10.xxxx/..., doi:10.xxxx/... or https://doi.org/10.xxxx/...), a URL (http(s)://...), "
         "arxiv:<id>, pmid:<number>, hdl:<handle> or isbn:<ISBN>")

_DOI_RE = re.compile(r"(?:doi:|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?:hdl:|https?://hdl\.handle\.net/)(\d[\w.]*/\S+)", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arxiv:(\S+)", re.IGNORECASE)
_PMID_RE = re.compile(r"pmid:(\d+)", re.IGNORECASE)
_ISBN_RE = re.compile(r"isbn:((?:\d-?){9}[\dXx]|(?:\d-?){12}\d)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# The link of each type, as DataCite and readers take it: the resolver URL
# (a Handle by https, the form DataCite uses; openMINDS writes its own
# pattern, see openminds_schemas.yaml), an ISBN as written (it has none)
_LINKS = {"DOI": "https://doi.org/{}", "arXiv": "https://arxiv.org/abs/{}",
          "PMID": "https://pubmed.ncbi.nlm.nih.gov/{}/", "Handle": "https://hdl.handle.net/{}", "URL": "{}",
          "ISBN": "isbn:{}"}


class Identifier(NamedTuple):
    """An identifier's DataCite type (DOI, URL, arXiv, PMID, Handle, ISBN)
    and its value without prefix (a DOI as the bare 10.xxxx/...)."""
    type: str
    value: str

    @property
    def link(self) -> str:
        """The resolvable URL, an ISBN as written (isbn:<ISBN>)."""
        return _LINKS[self.type].format(self.value)


def parse_identifier(text: str) -> Identifier:
    """Type and normalised value of an identifier; one of no recognised form raises."""
    text = str(text or "").strip()
    for kind, pattern in (("DOI", _DOI_RE), ("Handle", _HANDLE_RE), ("arXiv", _ARXIV_RE), ("PMID", _PMID_RE),
                          ("ISBN", _ISBN_RE), ("URL", _URL_RE)):
        if m := pattern.fullmatch(text):
            return Identifier(kind, m.group(1) if pattern.groups else m.group(0))
    raise ValueError(f"Identifier '{text}' has no recognised form: give {FORMS}")


class Publication(NamedTuple):
    """An entry of the Dataset's Related Publications: the identifier as
    given and parsed (None for an entry without identifier, such as a
    thesis), its DataCite relationType and the citation."""
    given: str
    identifier: Identifier | None
    relation: str
    citation: str

    @property
    def reference(self) -> str:
        """The citation followed by the identifier's link unless it already
        holds the identifier's value (any case); the link alone without a
        citation, the citation alone without an identifier."""
        if self.identifier is None:
            return self.citation
        if not self.citation:
            return self.identifier.link
        if self.identifier.value.lower() in self.citation.lower():
            return self.citation
        return f"{self.citation} {self.identifier.link}"


def related_publications(dataset: odml.Section | None, name: str = SECTION) -> list[Publication]:
    """The Dataset's Related Publications (the subsection `name`) in their
    order; an identifier of no recognised form, or an entry with neither
    identifier nor citation, raises."""
    entries = subsection(dataset, name)
    publications = []
    for entry in (entries.sections if entries is not None else []):
        given = str(prop(entry, "identifier") or "").strip()
        citation = str(prop(entry, "citation") or "").strip()
        if not (given or citation):
            raise ValueError(f"{entry.name}: neither identifier nor citation")
        publications.append(Publication(given, parse_identifier(given) if given else None,
                                        str(prop(entry, "relation") or ""), citation))
    return publications
