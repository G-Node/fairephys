"""Validation report for collecting and formatting results."""

import functools
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def guarded(check):
    """A sanity check that cannot crash silently: an exception becomes an
    error of its report (the ValidationReport among the arguments) naming
    the check, and the report is returned."""
    @functools.wraps(check)
    def run(*args, **kwargs):
        report = next(a for a in (*args, *kwargs.values()) if isinstance(a, ValidationReport))
        try:
            return check(*args, **kwargs)
        except Exception as exc:
            report.add_error("Sanity", f"{check.__name__} failed: {type(exc).__name__}: {exc}")
            return report
    return run


def files_exist(report: "ValidationReport", *paths: Path, layer: str = "Sanity") -> bool:
    """Whether every path exists; each missing one is an error of the report."""
    missing = [p for p in paths if not Path(p).exists()]
    for path in missing:
        report.add_error(layer, f"File not found: {path}")
    return not missing


@dataclass
class ValidationReport:
    """Collect validation errors, warnings, and info."""

    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, layer: str, msg: str) -> None:
        self.errors.append(f"[{layer}] {msg}")

    def add_warning(self, layer: str, msg: str) -> None:
        self.warnings.append(f"[{layer}] {msg}")

    def add_info(self, key: str, value: Any) -> None:
        self.info[key] = value

    def to_markdown(self) -> str:
        """Render report as markdown."""
        sections = [
            f"# Validation Report: {self.name}\n\n*Generated: {self.timestamp}*\n",
            self._status_section(),
            self._issues_section(),
            self._info_section("Session", ["subject_id", "species", "session_description", "session_start_time"]),
            self._info_section("Recording", ["sampling_rate_hz", "duration_seconds", "duration_formatted", "recording_segments", "total_channels"]),
            self._info_section("Electrodes", ["electrode_groups", "brain_regions", "probes", "total_probe_contacts", "channel_names_matched"]),
            self._data_integrity_section(),
            self._probe_details_section(),
            self._electrode_table_section(),
            self._cross_format_section(),
            self._bids_mapping_section(),
            self._geometry_section(),
            self._bids_section(),
            self._nwb_inspector_section(),
        ]
        return "\n".join(s for s in sections if s)

    def _grouped_issues(self) -> dict[str, dict[str, list]]:
        """Group issues by layer."""
        layers: dict[str, dict[str, list]] = {}
        for msg in self.errors:
            layer = msg.split("]")[0].strip("[")
            layers.setdefault(layer, {"errors": [], "warnings": []})["errors"].append(msg)
        for msg in self.warnings:
            layer = msg.split("]")[0].strip("[")
            layers.setdefault(layer, {"errors": [], "warnings": []})["warnings"].append(msg)
        return layers

    def _status_section(self) -> str:
        layers = self._grouped_issues()
        lines = ["## Status\n", f"**{'PASS' if self.passed else 'FAIL'}**\n"]
        lines.append("| Layer | Status | Issues |\n|-------|--------|--------|")
        for layer, issues in sorted(layers.items()):
            n_err, n_warn = len(issues["errors"]), len(issues["warnings"])
            lines.append(f"| {layer} | {'FAIL' if n_err else 'PASS'} | {n_err} errors, {n_warn} warnings |")
        if not layers:
            lines.append("| All | PASS | No issues |")
        return "\n".join(lines) + "\n"

    def _issues_section(self) -> str:
        if not self.errors and not self.warnings:
            return ""
        layers = self._grouped_issues()
        lines = ["## Issues\n"]
        for layer, issues in sorted(layers.items()):
            lines.append(f"### {layer}")
            lines.extend(f"- [ERROR] {m.split('] ', 1)[-1]}" for m in issues["errors"])
            lines.extend(f"- [WARN] {m.split('] ', 1)[-1]}" for m in issues["warnings"])
            lines.append("")
        return "\n".join(lines)

    def _info_section(self, title: str, keys: list[str]) -> str:
        data = {k: self.info[k] for k in keys if k in self.info}
        if not data:
            return ""
        lines = [f"## {title}\n"]
        for k, v in data.items():
            val = ", ".join(str(x) for x in v) if isinstance(v, list) else v
            lines.append(f"- **{k}**: {val}")
        return "\n".join(lines) + "\n"

    def _data_integrity_section(self) -> str:
        # One row set per compared NWB series; a single series has its
        # values as top-level info keys
        series = self.info.get("integrity_series") or (
            [self.info] if any(k in self.info for k in ["raw_samples", "nwb_samples"]) else [])
        if not series and "data_integrity_check" not in self.info:
            return ""
        several = len(series) > 1
        lines = ["## Data Integrity (Raw vs NWB)\n"]
        if series:
            lines.append("| Property | Raw | NWB |\n|----------|-----|-----|")
            for s in series:
                label = f" ({s['series']}, segment {s.get('segment', 'n/a')})" if several else ""
                lines.append(f"| Samples{label} | {s.get('raw_samples', 'n/a')} | {s.get('nwb_samples', 'n/a')} |")
                lines.append(f"| Channels{label} | {s.get('raw_channels', 'n/a')} | {s.get('nwb_channels', 'n/a')} |")
                if "raw_scale" in s or "nwb_scale" in s:
                    lines.append(f"| Scale (per stored unit){label} | {s.get('raw_scale', 'n/a')} | {s.get('nwb_scale', 'n/a')} |")
            lines.append("")
        for s in series:
            label = f" ({s['series']})" if several else ""
            if "raw_stream" in s:
                lines.append(f"- **Raw stream{label}**: {s['raw_stream']}")
            if "scale_note" in s:
                lines.append(f"- **Scale{label}**: {s['scale_note']}")
            if s.get("nwb_is_stub"):
                lines.append(f"- **NWB is stub file{label}**: Yes (truncated for testing)")
            if several:
                diff = f", max diff {s['max_diff']:.2e} V" if "max_diff" in s else ""
                lines.append(f"- **{s['series']} vs raw segment {s.get('segment', 'n/a')}**: {s['result']}{diff}")
        if "data_integrity_check" in self.info:
            lines.append(f"- **Data integrity check**: {self.info['data_integrity_check']}")
        if "max_sample_diff" in self.info:
            lines.append(f"- **Max sample difference**: {self.info['max_sample_diff']}")
        return "\n".join(lines) + "\n"

    def _probe_details_section(self) -> str:
        if "probe_details" not in self.info:
            return ""
        lines = ["## ProbeInterface Details\n"]
        for p in self.info["probe_details"]:
            lines.extend([
                f"### {p['name']}",
                f"- **Contacts**: {p['contacts']}",
                f"- **device_channel_indices**: {p['device_channel_indices']}",
                f"- **contact_ids**: {p['contact_ids']}\n",
            ])
        return "\n".join(lines)

    def _electrode_table_section(self) -> str:
        tbl = self.info.get("nwb_electrode_table")
        if not tbl:
            return ""
        lines = ["## NWB Electrode Table\n"]
        lines.append(f"- **Total electrodes**: {tbl.get('total', 'n/a')}")
        lines.append(f"- **Columns** (sorted; the file's order is neuroconv's, which varies between runs): "
                     f"{', '.join(tbl.get('columns', []))}")
        if "by_group" in tbl:
            lines.append("- **By electrode group**:")
            lines.extend(f"  - {g}: {c}" for g, c in tbl["by_group"].items())
        lines.append("")

        preview = self.info.get("nwb_electrodes_preview", [])
        if preview:
            cols = list(preview[0].keys())
            lines.append("### Electrode Preview (first 10)\n")
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
            lines.extend("| " + " | ".join(str(r.get(c, ""))[:30] for c in cols) + " |" for r in preview)
        return "\n".join(lines) + "\n"

    def _cross_format_section(self) -> str:
        """Cross-format consistency (BIDS <-> NWB)."""
        keys = [
            "bids_neural_channels", "bids_auxiliary_channels",
            "channel_count_consistency", "nwb_bids_probe_groups",
        ]
        data = {k: self.info[k] for k in keys if k in self.info}
        if not data:
            return ""
        lines = ["## Cross-Format Consistency (BIDS <-> NWB)\n"]
        for k, v in data.items():
            lines.append(f"- **{k}**: {v}")
        return "\n".join(lines) + "\n"

    def _bids_mapping_section(self) -> str:
        """BIDS electrode mapping consistency (channels <-> electrodes <-> probes)."""
        keys = ["channels_to_electrodes", "electrodes_to_probes", "bids_electrode_mapping"]
        data = {k: self.info[k] for k in keys if k in self.info}
        if not data:
            return ""
        lines = ["## BIDS Electrode Mapping\n"]
        for k, v in data.items():
            lines.append(f"- **{k}**: {v}")
        return "\n".join(lines) + "\n"

    def _geometry_section(self) -> str:
        """Three-way geometry verification (ProbeInterface <-> BIDS <-> NWB)."""
        keys = [k for k in self.info if k.endswith("_geometry") or k in [
            "nwb_electrode_groups", "nwb_bids_coordinates"
        ]]
        if not keys:
            return ""
        lines = ["## Geometry Verification (ProbeInterface <-> BIDS <-> NWB)\n"]
        for k in sorted(keys):
            v = self.info[k]
            val = ", ".join(str(x) for x in v) if isinstance(v, list) else v
            lines.append(f"- **{k}**: {val}")
        return "\n".join(lines) + "\n"

    def _bids_section(self) -> str:
        if "bids_validator_output" not in self.info:
            return ""
        lines = []
        try:
            data = json.loads(self.info["bids_validator_output"])
            summary = data.get("summary", {})
            if summary:
                lines.extend([
                    "## Dataset Summary (BIDS)\n",
                    f"- **Subjects**: {', '.join(summary.get('subjects', []))}",
                    f"- **Sessions**: {', '.join(summary.get('sessions', []))}",
                    f"- **Modalities**: {', '.join(summary.get('modalities', []))}",
                    f"- **Total Files**: {summary.get('totalFiles', 'n/a')}\n",
                ])
        except (json.JSONDecodeError, TypeError):
            pass
        lines.extend(["## BIDS Validator Output\n", "```json", self.info["bids_validator_output"], "```\n"])
        return "\n".join(lines)

    def _nwb_inspector_section(self) -> str:
        keys = sorted(k for k in self.info if k.startswith("nwb_inspector_"))
        if not keys:
            return ""
        lines = ["## NWB Inspector Output\n"]
        for k in keys:
            lines.extend([f"### {k.replace('nwb_inspector_', '')}.nwb\n", "```", self.info[k], "```\n"])
        return "\n".join(lines)
