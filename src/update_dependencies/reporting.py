"""Portable structured results and GitHub Actions integration."""

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Report:
    schema_version: int = 1
    changed_files: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self):
        return {
            **asdict(self),
            "changed": bool(self.changed_files),
            "change_count": len(self.changed_files),
        }

    def json(self):
        return json.dumps(self.to_dict(), indent=2) + "\n"


def github_report(report: Report, report_path: Path | None):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        values = {
            "changed": str(bool(report.changed_files)).lower(),
            "change-count": str(len(report.changed_files)),
            "changed-files": json.dumps(report.changed_files),
            "report-path": str(report_path) if report_path else "",
        }
        with Path(output).open("a", encoding="utf-8") as stream:
            for key, value in values.items():
                delimiter = uuid.uuid4().hex
                stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as stream:
            mode = "Proposed" if report.dry_run else "Updated"
            stream.write(f"## Dependency updates\n\n{mode} {len(report.changed_files)} file(s).\n\n")
            stream.writelines(f"- {json.dumps(name)}\n" for name in report.changed_files)
