"""
bug_report.py - The structure of a bug raised from MAS-QA-Bridge.

A ``BugReport`` is laid out the way a developer reads it, top to bottom:

    Title            <Feature/screen> – <symptom>      (searchable, one line)
    Severity / Priority / Reproducibility / Found in build
    Summary          one or two sentences of context
    Environment      app version, OS build, display/DPI, configuration, tester, time
    Preconditions    data / account / state needed before step 1
    Steps            numbered, the failing step highlighted
    Expected result  vs.  Actual result
    Evidence         screenshots inline, recordings attached
    Traceability     test case, test run (links)

It renders to the HTML stored in the Bug's "Repro Steps" field and can be
saved as a local draft (JSON + HTML) when submission fails, so nothing is lost.
"""
from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from qa_session import FAILED, TestSession

SEVERITIES = ["1 - Critical", "2 - High", "3 - Medium", "4 - Low"]
PRIORITIES = [1, 2, 3, 4]
REPRODUCIBILITY = ["Always", "Intermittent", "Once (could not reproduce)"]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def _p(text: str) -> str:
    return "<br>".join(html.escape(line) for line in text.strip().splitlines())


@dataclass
class BugReport:
    title: str = ""
    severity: str = "3 - Medium"
    priority: int = 2
    reproducibility: str = "Always"
    found_in: str = ""  # application version / build number
    area_path: str = ""
    iteration_path: str = ""
    assigned_to: str = ""
    tags: list[str] = field(default_factory=lambda: ["MAS-QA-Bridge"])
    extra_fields: dict[str, str] = field(default_factory=dict)  # custom required fields, by reference name

    summary: str = ""
    preconditions: str = ""
    steps: list[str] = field(default_factory=list)
    failing_step: Optional[int] = None  # 1-based index into ``steps``
    expected: str = ""
    actual: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    attachments: list[Path] = field(default_factory=list)

    test_case_id: Optional[int] = None
    test_case_title: str = ""
    configuration: str = ""
    run_id: Optional[int] = None
    result_id: Optional[int] = None

    # ------------------------------------------------------------ building
    @classmethod
    def from_session(
        cls,
        session: Optional[TestSession],
        step_index: Optional[int] = None,
        environment: Optional[dict[str, str]] = None,
        found_in: str = "",
    ) -> "BugReport":
        """Pre-fill a report from the test being executed.

        Steps up to (and including) the failing one become the repro steps;
        its expected result and the tester's "actual" comment become the
        Expected / Actual sections; that step's evidence comes first.
        """
        report = cls(environment=dict(environment or {}), found_in=found_in)
        if session is None:
            return report

        point = session.point
        report.test_case_id = point.test_case_id or None
        report.test_case_title = point.title
        report.configuration = point.configuration
        report.run_id = session.run_id
        report.result_id = session.result_id

        if step_index is None:
            failed = [i for i, s in enumerate(session.steps) if s.outcome == FAILED]
            step_index = failed[0] if failed else None

        if step_index is not None and 0 <= step_index < len(session.steps):
            executed = session.steps[: step_index + 1]
            failing = session.steps[step_index]
            report.failing_step = len(executed)
            report.expected = failing.expected
            report.actual = failing.comment
            symptom = failing.comment or f"step {failing.number} fails: {failing.action}"
        else:
            executed = session.steps
            symptom = "unexpected behaviour"
        report.steps = [s.action for s in executed]
        report.title = f"{point.title} – {symptom}"[:250]
        report.summary = (
            f"While executing test case #{point.test_case_id} \"{point.title}\""
            + (f" ({point.configuration})" if point.configuration else "")
            + f", {symptom[0].lower() + symptom[1:] if symptom else ''}."
        )

        items = session.attachments()
        if step_index is not None:
            failing_path = session.steps[step_index].action_path
            items.sort(key=lambda e: 0 if e.step_action_path == failing_path else (1 if e.kind == "recording" else 2))
        report.attachments = [e.path for e in items]
        return report

    # ------------------------------------------------------------ validation
    def validate(self) -> tuple[list[str], list[str]]:
        """(errors that block submission, warnings worth a second look)."""
        errors, warnings = [], []
        if len(self.title.strip()) < 10:
            errors.append("Title is required (describe the symptom, e.g. 'Orders – total not updated after quantity change').")
        if not [s for s in self.steps if s.strip()]:
            errors.append("Add at least one step to reproduce.")
        if not self.actual.strip():
            errors.append("Actual result is required - describe what happened.")
        if not self.expected.strip():
            warnings.append("Expected result is empty.")
        if not self.found_in.strip():
            warnings.append("'Found in build' is empty - developers need the application version.")
        if not self.attachments:
            warnings.append("No screenshot or recording attached.")
        return errors, warnings

    # ------------------------------------------------------------ rendering
    def repro_html(self, media_urls: Optional[dict[str, str]] = None, links: Optional[dict[str, str]] = None) -> str:
        """HTML for the Bug's Repro Steps field.

        ``media_urls`` maps attachment file names to URLs (ADO attachment URLs
        on submit, file:// URLs for the local preview); images are shown inline.
        ``links`` may hold ``test_case`` and ``run`` web URLs.
        """
        media_urls = media_urls or {}
        links = links or {}
        h3 = "<h3 style='margin:14px 0 4px 0'>{}</h3>"
        out = []

        badges = [
            f"<b>Severity:</b> {html.escape(self.severity)}",
            f"<b>Priority:</b> {self.priority}",
            f"<b>Reproducibility:</b> {html.escape(self.reproducibility)}",
        ]
        if self.found_in:
            badges.append(f"<b>Found in:</b> {html.escape(self.found_in)}")
        out.append("<p>" + " &nbsp;|&nbsp; ".join(badges) + "</p>")

        if self.summary.strip():
            out.append(h3.format("Summary") + f"<p>{_p(self.summary)}</p>")

        if self.environment:
            rows = "".join(
                f"<tr><td style='padding:2px 12px 2px 0;color:#666'><b>{html.escape(k)}</b></td>"
                f"<td style='padding:2px 0'>{html.escape(v)}</td></tr>"
                for k, v in self.environment.items()
            )
            out.append(h3.format("Environment") + f"<table>{rows}</table>")

        if self.preconditions.strip():
            out.append(h3.format("Preconditions") + f"<p>{_p(self.preconditions)}</p>")

        items = []
        for number, step in enumerate((s for s in self.steps if s.strip()), start=1):
            text = _p(step)
            if number == self.failing_step:
                text = f"<span style='color:#c62828'><b>{text}</b> &nbsp;⟵ fails here</span>"
            items.append(f"<li>{text}</li>")
        out.append(h3.format("Steps to reproduce") + f"<ol>{''.join(items)}</ol>")

        out.append(h3.format("Expected result") + f"<p>{_p(self.expected) or '<i>not specified</i>'}</p>")
        out.append(
            h3.format("Actual result") + f"<p style='color:#c62828'>{_p(self.actual) or '<i>not specified</i>'}</p>"
        )

        if self.attachments:
            media = []
            for path in self.attachments:
                url = media_urls.get(path.name)
                name = html.escape(path.name)
                if url and path.suffix.lower() in IMAGE_SUFFIXES:
                    media.append(f"<p><b>{name}</b><br><img src='{html.escape(url)}' style='max-width:900px' width='900'></p>")
                elif url:
                    media.append(f"<p>🎞 <a href='{html.escape(url)}'>{name}</a></p>")
                else:
                    media.append(f"<p>{name}</p>")
            out.append(h3.format("Evidence") + "".join(media))

        trace = []
        if self.test_case_id:
            label = f"Test case #{self.test_case_id} {html.escape(self.test_case_title)}"
            trace.append(f"<a href='{links['test_case']}'>{label}</a>" if links.get("test_case") else label)
        if self.run_id:
            label = f"Test run #{self.run_id}"
            trace.append(f"<a href='{links['run']}'>{label}</a>" if links.get("run") else label)
        trace.append("Reported with MAS-QA-Bridge")
        out.append(f"<p style='color:#666;margin-top:16px'>{' · '.join(trace)}</p>")
        return "".join(out)

    def system_info_html(self) -> str:
        return "<br>".join(f"<b>{html.escape(k)}:</b> {html.escape(v)}" for k, v in self.environment.items())

    # ------------------------------------------------------------ drafts
    def save_draft(self, folder: Path, error: Optional[str] = None) -> Path:
        """Write ``bug_<timestamp>.json`` (+ ``.html`` preview) to ``folder``."""
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / f"bug_draft_{datetime.now():%Y%m%d_%H%M%S}"
        data = asdict(self)
        data["attachments"] = [str(p) for p in self.attachments]
        if error:
            data["submit_error"] = error
        stem.with_suffix(".json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        media = {p.name: p.resolve().as_uri() for p in self.attachments if p.exists()}
        page = f"<html><body style='font-family:Segoe UI,sans-serif'><h2>{html.escape(self.title)}</h2>{self.repro_html(media)}</body></html>"
        stem.with_suffix(".html").write_text(page, encoding="utf-8")
        return stem.with_suffix(".json")

    @classmethod
    def load_draft(cls, path: Path) -> "BugReport":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data.pop("submit_error", None)
        data["attachments"] = [Path(p) for p in data.get("attachments", [])]
        return cls(**data)
