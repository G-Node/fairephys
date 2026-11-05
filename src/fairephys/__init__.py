"""fairephys: FAIR curation of extracellular electrophysiology datasets.

YAML templates are compiled to one odML document, from which NWB, BIDS
(BEP032), openMINDS and the documentation are generated. The subpackages
(`metadata`, `enrichment`, `probes`, `bids`, `converters`, `export`,
`validation`, `curation`) are imported explicitly: importing `fairephys`
alone loads none of their dependencies.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("fairephys")
except PackageNotFoundError:  # a source checkout that is not installed
    __version__ = "0.0.0"
