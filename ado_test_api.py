"""
ado_test_api.py - Azure DevOps Test Plans integration for MAS-QA-Bridge.

Client over the Azure DevOps REST API (api-version 7.1), authenticated with a PAT.

Browse (Test Plans hierarchy)
    list_plans()                        GET   {project}/_apis/testplan/plans
    list_suites(plan)                   GET   {project}/_apis/testplan/Plans/{plan}/suites
    list_points(plan, suite)            GET   {project}/_apis/testplan/Plans/{plan}/Suites/{suite}/TestPoint
    get_test_case_steps(test_case_id)   GET   {project}/_apis/wit/workitems/{id}  (Microsoft.VSTS.TCM.Steps)

Execute
    create_test_run(test_point_id)      POST  {project}/_apis/test/runs
    get_test_results(run_id)            GET   {project}/_apis/test/Runs/{run}/results
    update_test_result(...)             PATCH {project}/_apis/test/Runs/{run}/results   (+ step results)
    upload_test_run_attachment(...)     POST  {project}/_apis/test/Runs/{run}/attachments
    upload_test_result_attachment(...)  POST  {project}/_apis/test/Runs/{run}/Results/{id}/attachments
    complete_test_run(run_id)           PATCH {project}/_apis/test/runs/{run}

Defects
    bug_metadata()                      GET   {project}/_apis/wit/workitemtypes/Bug/fields, classificationnodes
    create_bug(report)                  POST  {project}/_apis/wit/workitems/$Bug  (+ wit/attachments)
    run_diagnostics()                   read-only pre-flight checks

``record_execution`` chains these into the single "Publish" action of the UI and
reports per-step outcomes the same way the web Test Runner does, so results show
up step by step in Test Plans.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional
from urllib.parse import quote

import requests

from qa_session import FAILED, UNSPECIFIED, EvidenceItem, TestPoint, TestSession, TestStep

if TYPE_CHECKING:
    from bug_report import BugReport

log = logging.getLogger(__name__)

VALID_OUTCOMES = {
    "Passed",
    "Failed",
    "Blocked",
    "NotApplicable",
    "Paused",
    "Inconclusive",
    "NotExecuted",
}
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # ADO's default test attachment limit


class AdoError(RuntimeError):
    """A failed Azure DevOps REST call."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class AdoConfig:
    organization: str
    project: str
    pat: str
    test_plan_id: Optional[int] = None
    base_url: str = "https://dev.azure.com"
    api_version: str = "7.1"
    attachment_api_version: str = "7.1-preview.1"
    run_name_prefix: str = "MAS-QA-Bridge"
    attachment_target: str = "result"  # "result" | "run" | "both"
    step_attachments: bool = True  # attach step screenshots to the step itself
    timeout: float = 30.0
    verify_ssl: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdoConfig":
        """Build from the ``ado:`` section of config.yaml.

        The PAT is taken from the ``ADO_PAT`` environment variable when set,
        otherwise from the config file.
        """
        data = dict(data or {})
        pat = os.environ.get("ADO_PAT") or data.get("pat") or ""
        missing = [k for k in ("organization", "project") if not str(data.get(k) or "").strip()]
        if not pat:
            missing.append("pat (or ADO_PAT env var)")
        if missing:
            raise ValueError(f"Missing ADO settings: {', '.join(missing)}")

        plan_id = data.get("test_plan_id")
        return cls(
            organization=str(data["organization"]).strip(),
            project=str(data["project"]).strip(),
            pat=pat.strip(),
            test_plan_id=int(plan_id) if plan_id else None,
            base_url=str(data.get("base_url") or cls.base_url).rstrip("/"),
            api_version=str(data.get("api_version") or cls.api_version),
            attachment_api_version=str(data.get("attachment_api_version") or cls.attachment_api_version),
            run_name_prefix=str(data.get("run_name_prefix") or cls.run_name_prefix),
            attachment_target=str(data.get("attachment_target") or cls.attachment_target).lower(),
            step_attachments=bool(data.get("step_attachments", True)),
            timeout=float(data.get("timeout_seconds") or cls.timeout),
            verify_ssl=bool(data.get("verify_ssl", True)),
        )


def _iso(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ----------------------------------------------------------------------------
# Test case steps (Microsoft.VSTS.TCM.Steps) parsing
# ----------------------------------------------------------------------------
class _TextExtractor(HTMLParser):
    _BREAKS = {"br", "p", "div", "li", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._BREAKS:
            self.parts.append("\n")
        elif tag == "img":
            self.parts.append("[image]")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(fragment: Optional[str]) -> str:
    """Steps are stored as HTML; show them as plain text."""
    if not fragment:
        return ""
    parser = _TextExtractor()
    parser.feed(fragment)
    text = unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse_steps_xml(
    xml_text: Optional[str],
    fetch_shared: Optional[Callable[[int], tuple[str, Optional[str]]]] = None,
) -> list[TestStep]:
    """Flatten a test case's steps XML into ``TestStep`` objects.

    Shared steps (``<compref ref="...">``) are expanded via ``fetch_shared``,
    which returns ``(title, steps_xml)`` for a Shared Steps work item. Their
    children get a compound action path (``<compref id><step id>``), which is
    how Test Plans addresses steps inside shared steps.
    """
    steps: list[TestStep] = []
    if not xml_text:
        return steps

    def walk(element: ET.Element, path_prefix: str, id_prefix: str, shared_title: Optional[str]) -> None:
        for node in element:
            if node.tag == "step":
                step_id = int(node.get("id", "0"))
                strings = node.findall("parameterizedString")
                steps.append(
                    TestStep(
                        number=len(steps) + 1,
                        step_id=step_id,
                        action_path=f"{path_prefix}{step_id:08x}",
                        step_identifier=f"{id_prefix}{step_id}",
                        action=html_to_text(strings[0].text if strings else ""),
                        expected=html_to_text(strings[1].text if len(strings) > 1 else ""),
                        shared_title=shared_title,
                    )
                )
            elif node.tag == "compref":
                comp_id = int(node.get("id", "0"))
                ref = int(node.get("ref", "0"))
                if fetch_shared and ref:
                    title, shared_xml = fetch_shared(ref)
                    if shared_xml:
                        shared_root = ET.fromstring(shared_xml)
                        walk(shared_root, f"{path_prefix}{comp_id:08x}", f"{id_prefix}{comp_id};", title)
                else:
                    steps.append(
                        TestStep(
                            number=len(steps) + 1,
                            step_id=comp_id,
                            action_path=f"{path_prefix}{comp_id:08x}",
                            step_identifier=f"{id_prefix}{comp_id}",
                            action=f"Shared steps #{ref}",
                        )
                    )
                # Steps nested inside <compref> follow the shared steps.
                walk(node, path_prefix, id_prefix, shared_title)

    walk(ET.fromstring(xml_text), "", "", None)
    return steps


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------
class AdoTestClient:
    """Azure DevOps Test Plans client authenticated with a PAT."""

    def __init__(self, config: AdoConfig) -> None:
        self.config = config
        self.session = requests.Session()
        # PAT auth = HTTP Basic with an empty user name.
        self.session.auth = ("", config.pat)
        self.session.headers.update({"Accept": "application/json", "User-Agent": "MAS-QA-Bridge/2.0"})
        self.session.verify = config.verify_ssl
        self._shared_cache: dict[int, tuple[str, Optional[str]]] = {}
        self._wit_fields_cache: dict[str, dict[str, dict[str, Any]]] = {}

    # ----------------------------------------------------------------- basics
    @property
    def org_url(self) -> str:
        return f"{self.config.base_url}/{quote(self.config.organization, safe='')}"

    @property
    def project_url(self) -> str:
        return f"{self.org_url}/{quote(self.config.project, safe='')}"

    def run_web_url(self, run_id: int) -> str:
        return f"{self.project_url}/_testManagement/runs?_a=runCharts&runId={run_id}"

    def work_item_web_url(self, work_item_id: int) -> str:
        return f"{self.project_url}/_workitems/edit/{work_item_id}"

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Optional[bytes] = None,
        content_type: Optional[str] = None,
        params: Optional[dict[str, Any]] = None,
        api_version: Optional[str] = None,
    ) -> requests.Response:
        url = f"{self.project_url}/_apis/{path.lstrip('/')}"
        query = {"api-version": api_version or self.config.api_version, **(params or {})}
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = content_type or "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = self.session.request(
                method, url, params=query, data=data, headers=headers, timeout=self.config.timeout
            )
        except requests.RequestException as exc:
            raise AdoError(f"{method} {url} failed: {exc}") from exc

        # A bad/expired PAT yields 401, or 203 + an HTML sign-in page.
        resp_type = resp.headers.get("Content-Type", "")
        if resp.status_code in (401, 203) or (resp.ok and "text/html" in resp_type):
            raise AdoError(
                "Authentication failed - check the PAT value, expiry and scopes "
                "(Test Management: Read & write, Work Items: Read & write).",
                resp.status_code,
            )
        if not resp.ok:
            try:
                detail = resp.json().get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise AdoError(f"{method} {url} -> HTTP {resp.status_code}: {detail[:500]}", resp.status_code)
        return resp

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._send(method, path, **kwargs)
        return resp.json() if resp.content else {}

    def _get_all(self, path: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """GET a list endpoint, following ``x-ms-continuationtoken`` paging."""
        items: list[dict[str, Any]] = []
        query = dict(params or {})
        for _ in range(200):  # hard stop against a misbehaving server
            resp = self._send("GET", path, params=query)
            items.extend((resp.json() if resp.content else {}).get("value", []))
            token = resp.headers.get("x-ms-continuationtoken")
            if not token:
                break
            query["continuationToken"] = token
        return items

    def authenticate(self) -> dict[str, Any]:
        """Verify the PAT can read test data in the project."""
        self._request("GET", "test/runs", params={"$top": 1})
        log.info("Authenticated to %s/%s", self.config.organization, self.config.project)
        return {"organization": self.config.organization, "project": self.config.project}

    # ------------------------------------------------------------- browsing
    def list_plans(self, active_only: bool = True) -> list[dict[str, Any]]:
        params = {"filterActivePlans": "true"} if active_only else {}
        plans = self._get_all("testplan/plans", params)
        return sorted(({"id": p["id"], "name": p.get("name", "")} for p in plans), key=lambda p: p["name"].lower())

    def list_suites(self, plan_id: int) -> list[dict[str, Any]]:
        """Flat list of suites: ``{"id", "name", "parent_id", "type"}``."""
        suites = self._get_all(f"testplan/Plans/{plan_id}/suites")
        return [
            {
                "id": s["id"],
                "name": s.get("name", ""),
                "parent_id": (s.get("parentSuite") or {}).get("id"),
                "type": s.get("suiteType", ""),
            }
            for s in suites
        ]

    def list_points(self, plan_id: int, suite_id: int) -> list[TestPoint]:
        raw = self._get_all(f"testplan/Plans/{plan_id}/Suites/{suite_id}/TestPoint", {"includePointDetails": "true"})
        points = []
        for p in raw:
            case = p.get("testCaseReference") or {}
            outcome = str((p.get("results") or {}).get("outcome") or UNSPECIFIED)
            points.append(
                TestPoint(
                    id=int(p["id"]),
                    test_case_id=int(case.get("id", 0)),
                    title=case.get("name", ""),
                    plan_id=plan_id,
                    suite_id=suite_id,
                    configuration=(p.get("configuration") or {}).get("name", ""),
                    tester=(p.get("tester") or {}).get("displayName", ""),
                    outcome=outcome[:1].upper() + outcome[1:],
                )
            )
        return points

    def _work_item_fields(self, work_item_id: int, fields: Iterable[str]) -> dict[str, Any]:
        data = self._request("GET", f"wit/workitems/{work_item_id}", params={"fields": ",".join(fields)})
        return data.get("fields", {})

    def _fetch_shared_steps(self, work_item_id: int) -> tuple[str, Optional[str]]:
        if work_item_id not in self._shared_cache:
            fields = self._work_item_fields(work_item_id, ["System.Title", "Microsoft.VSTS.TCM.Steps"])
            self._shared_cache[work_item_id] = (
                fields.get("System.Title", f"Shared steps #{work_item_id}"),
                fields.get("Microsoft.VSTS.TCM.Steps"),
            )
        return self._shared_cache[work_item_id]

    def get_test_case_steps(self, test_case_id: int) -> list[TestStep]:
        fields = self._work_item_fields(test_case_id, ["System.Title", "Microsoft.VSTS.TCM.Steps"])
        return parse_steps_xml(fields.get("Microsoft.VSTS.TCM.Steps"), self._fetch_shared_steps)

    def load_session(self, point: TestPoint) -> TestSession:
        return TestSession(point=point, steps=self.get_test_case_steps(point.test_case_id))

    # --------------------------------------------------------------- test runs
    def create_test_run(
        self,
        test_point_id: int,
        plan_id: Optional[int] = None,
        name: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a manual, in-progress test run for one test point."""
        plan_id = plan_id or self.config.test_plan_id
        if not plan_id:
            raise AdoError("A test plan id is required to create a run from a test point.")
        body: dict[str, Any] = {
            "name": name or f"{self.config.run_name_prefix} - point {test_point_id}",
            "plan": {"id": str(plan_id)},
            "pointIds": [int(test_point_id)],
            "automated": False,
            "state": "InProgress",
        }
        if comment:
            body["comment"] = comment
        run = self._request("POST", "test/runs", json_body=body)
        log.info("Created test run %s for point %s (plan %s)", run.get("id"), test_point_id, plan_id)
        return run

    def complete_test_run(self, run_id: int, comment: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"state": "Completed", "completedDate": _iso()}
        if comment:
            body["comment"] = comment
        return self._request("PATCH", f"test/runs/{run_id}", json_body=body)

    def abort_test_run(self, run_id: int) -> None:
        try:
            self._request("PATCH", f"test/runs/{run_id}", json_body={"state": "Aborted"})
        except AdoError as exc:
            log.warning("Could not abort run %s: %s", run_id, exc)

    # ------------------------------------------------------------ test results
    def get_test_results(self, run_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"test/Runs/{run_id}/results").get("value", [])

    def _result_id_for_point(self, run_id: int, test_point_id: int) -> int:
        results = self.get_test_results(run_id)
        if not results:
            raise AdoError(f"Run {run_id} has no results - is point {test_point_id} in the plan?")
        match = next(
            (r for r in results if str((r.get("testPoint") or {}).get("id")) == str(test_point_id)),
            results[0],
        )
        return int(match["id"])

    def update_test_result(
        self,
        run_id: int,
        result_id: int,
        outcome: str,
        comment: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: Optional[float] = None,
        started_date: Optional[datetime] = None,
        iteration_details: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        """Set the outcome of a test result (optionally with step results) and complete it."""
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome '{outcome}'. Expected one of {sorted(VALID_OUTCOMES)}")
        result: dict[str, Any] = {
            "id": int(result_id),
            "outcome": outcome,
            "state": "Completed",
            "completedDate": _iso(),
        }
        if started_date:
            result["startedDate"] = _iso(started_date)
        if comment:
            result["comment"] = comment
        if error_message:
            result["errorMessage"] = error_message
        if duration_ms is not None:
            result["durationInMs"] = duration_ms
        if iteration_details:
            result["iterationDetails"] = iteration_details
        data = self._request("PATCH", f"test/Runs/{run_id}/results", json_body=[result])
        log.info("Result %s in run %s set to %s", result_id, run_id, outcome)
        return data.get("value", [])

    @staticmethod
    def build_iteration(session: TestSession, outcome: str, comment: Optional[str] = None) -> dict[str, Any]:
        """Iteration payload carrying one ``actionResult`` per executed step."""
        now = datetime.now(timezone.utc)
        actions = []
        for step in session.steps:
            if step.outcome == UNSPECIFIED:
                continue
            action: dict[str, Any] = {
                "actionPath": step.action_path,
                "iterationId": 1,
                "stepIdentifier": step.step_identifier,
                "outcome": step.outcome,
                "startedDate": _iso(step.started_at or session.started_at),
                "completedDate": _iso(step.completed_at or now),
            }
            if step.comment:
                action["comment"] = step.comment
                if step.outcome == FAILED:
                    action["errorMessage"] = step.comment
            actions.append(action)
        iteration: dict[str, Any] = {
            "id": 1,
            "outcome": outcome,
            "startedDate": _iso(session.started_at),
            "completedDate": _iso(now),
            "durationInMs": session.duration_ms,
            "actionResults": actions,
        }
        if comment:
            iteration["comment"] = comment
        return iteration

    # ------------------------------------------------------------- attachments
    @staticmethod
    def _attachment_body(file_path: str | Path, comment: Optional[str], attachment_type: str) -> dict[str, Any]:
        path = Path(file_path)
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise AdoError(f"{path.name} is {size / 1e6:.1f} MB - over the 100 MB attachment limit.")
        return {
            "stream": base64.b64encode(path.read_bytes()).decode("ascii"),
            "fileName": path.name,
            "comment": comment or "Evidence captured by MAS-QA-Bridge",
            "attachmentType": attachment_type,
        }

    def upload_test_run_attachment(
        self,
        run_id: int,
        file_path: str | Path,
        comment: Optional[str] = None,
        attachment_type: str = "GeneralAttachment",
    ) -> dict[str, Any]:
        """Attach a file to a test run (visible on the run's Attachments tab)."""
        data = self._request(
            "POST",
            f"test/Runs/{run_id}/attachments",
            json_body=self._attachment_body(file_path, comment, attachment_type),
            api_version=self.config.attachment_api_version,
        )
        log.info("Uploaded %s to run %s", Path(file_path).name, run_id)
        return data

    def upload_test_result_attachment(
        self,
        run_id: int,
        result_id: int,
        file_path: str | Path,
        comment: Optional[str] = None,
        attachment_type: str = "GeneralAttachment",
        iteration_id: Optional[int] = None,
        action_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Attach a file to a test result - or to one step of it (iteration + action path)."""
        params = {}
        if iteration_id is not None:
            params["iterationId"] = iteration_id
            if action_path:
                params["actionPath"] = action_path
        data = self._request(
            "POST",
            f"test/Runs/{run_id}/Results/{result_id}/attachments",
            json_body=self._attachment_body(file_path, comment, attachment_type),
            params=params,
            api_version=self.config.attachment_api_version,
        )
        where = f"step {action_path}" if action_path else f"result {result_id}"
        log.info("Uploaded %s to %s of run %s", Path(file_path).name, where, run_id)
        return data

    # ------------------------------------------------------------------ bugs
    def get_work_item_type_fields(self, work_item_type: str = "Bug") -> dict[str, dict[str, Any]]:
        """Fields of a work item type: ``{refName: {"name", "required", "allowed"}}`` (cached)."""
        cache = self._wit_fields_cache
        if work_item_type not in cache:
            try:
                data = self._request(
                    "GET", f"wit/workitemtypes/{quote(work_item_type)}/fields", params={"$expand": "allowedValues"}
                )
            except AdoError as exc:
                if exc.status_code == 404:
                    raise AdoError(
                        f"This project has no '{work_item_type}' work item type. Projects using the Basic "
                        "process track defects as 'Issue' - set bug.work_item_type in config.yaml.",
                        404,
                    ) from exc
                raise
            cache[work_item_type] = {
                f["referenceName"]: {
                    "name": f.get("name", f["referenceName"]),
                    "required": bool(f.get("alwaysRequired")),
                    "allowed": [str(v) for v in (f.get("allowedValues") or [])],
                }
                for f in data.get("value", [])
            }
        return cache[work_item_type]

    def list_classification_paths(self, group: str) -> list[str]:
        """All area (``group="areas"``) or iteration (``"iterations"``) paths of the project."""
        root = self._request("GET", f"wit/classificationnodes/{group}", params={"$depth": 10})
        paths: list[str] = []

        def walk(node: dict[str, Any], prefix: str) -> None:
            path = f"{prefix}\\{node['name']}" if prefix else node["name"]
            paths.append(path)
            for child in node.get("children") or []:
                walk(child, path)

        walk(root, "")
        return paths

    def bug_metadata(self, work_item_type: str = "Bug") -> dict[str, Any]:
        """Everything the bug form needs: severity values, areas, iterations, required fields."""
        fields = self.get_work_item_type_fields(work_item_type)
        severity = fields.get("Microsoft.VSTS.Common.Severity", {}).get("allowed") or []
        return {
            "work_item_type": work_item_type,
            "severities": severity,
            "areas": self.list_classification_paths("areas"),
            "iterations": self.list_classification_paths("iterations"),
            "required": sorted(m["name"] for m in fields.values() if m["required"]),
            "fields": set(fields),
        }

    def upload_work_item_attachment(self, file_path: str | Path) -> str:
        path = Path(file_path)
        size = path.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise AdoError(f"{path.name} is {size / 1e6:.1f} MB - over the attachment limit.")
        data = self._request(
            "POST",
            "wit/attachments",
            data=path.read_bytes(),
            content_type="application/octet-stream",
            params={"fileName": path.name},
        )
        return data["url"]

    def create_bug(self, report: "BugReport", work_item_type: str = "Bug") -> dict[str, Any]:
        """Create a Bug from a ``BugReport``.

        * evidence is uploaded first, screenshots are shown inline in Repro Steps
        * linked to the test case ("Tested By") and to the test run (hyperlink)
        * only fields that exist in this project's process are sent (Agile, Scrum
          and CMMI differ); anything skipped is reported back as ``skipped_fields``
        """
        errors, _ = report.validate()
        if errors:
            raise AdoError("Bug is incomplete: " + " ".join(errors))
        meta = self.get_work_item_type_fields(work_item_type)

        media = {Path(p).name: self.upload_work_item_attachment(p) for p in report.attachments}
        links = {}
        if report.test_case_id:
            links["test_case"] = self.work_item_web_url(report.test_case_id)
        if report.run_id:
            links["run"] = self.run_web_url(report.run_id)
        repro = report.repro_html(media, links)

        severity = report.severity
        allowed = meta.get("Microsoft.VSTS.Common.Severity", {}).get("allowed") or []
        if allowed and severity not in allowed:
            # e.g. "2 - High" vs a customised list: match on the leading number.
            severity = next((v for v in allowed if v[:1] == severity[:1]), allowed[len(allowed) // 2])

        wanted: dict[str, Any] = {
            "System.Title": report.title.strip(),
            "Microsoft.VSTS.TCM.ReproSteps": repro,
            "Microsoft.VSTS.TCM.SystemInfo": report.system_info_html(),
            "Microsoft.VSTS.Common.Severity": severity,
            "Microsoft.VSTS.Common.Priority": int(report.priority),
            "Microsoft.VSTS.Build.FoundIn": report.found_in.strip(),
            "System.AreaPath": report.area_path.strip(),
            "System.IterationPath": report.iteration_path.strip(),
            "System.AssignedTo": report.assigned_to.strip(),
            "System.Tags": "; ".join(t.strip() for t in report.tags if t.strip()),
            **report.extra_fields,
        }
        if "Microsoft.VSTS.TCM.ReproSteps" not in meta:  # e.g. customised processes
            wanted["System.Description"] = wanted.pop("Microsoft.VSTS.TCM.ReproSteps")
        skipped = [k for k, v in wanted.items() if v not in ("", None) and meta and k not in meta]
        ops: list[dict[str, Any]] = [
            {"op": "add", "path": f"/fields/{k}", "value": v}
            for k, v in wanted.items()
            if v not in ("", None) and (not meta or k in meta)
        ]
        if report.test_case_id:
            ops.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "Microsoft.VSTS.Common.TestedBy-Forward",
                        "url": f"{self.org_url}/_apis/wit/workItems/{report.test_case_id}",
                        "attributes": {"comment": "Found while executing this test case"},
                    },
                }
            )
        if links.get("run"):
            ops.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {"rel": "Hyperlink", "url": links["run"], "attributes": {"comment": "Test run"}},
                }
            )
        for name, url in media.items():
            ops.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {"rel": "AttachedFile", "url": url, "attributes": {"comment": f"Evidence: {name}"}},
                }
            )

        bug = self._request(
            "POST",
            f"wit/workitems/${quote(work_item_type)}",
            json_body=ops,
            content_type="application/json-patch+json",
        )
        bug_id = int(bug["id"])
        log.info("Created %s %s: %s", work_item_type, bug_id, report.title)
        if skipped:
            log.warning("Fields not in this project's %s type were skipped: %s", work_item_type, ", ".join(skipped))

        if report.run_id and report.result_id:
            self.associate_bug(report.run_id, report.result_id, bug_id)
        return {
            "id": bug_id,
            "url": self.work_item_web_url(bug_id),
            "title": report.title.strip(),
            "skipped_fields": skipped,
            "attachments": list(media),
        }

    def associate_bug(self, run_id: int, result_id: int, bug_id: int) -> None:
        """Show the bug under the test result's 'Bugs' section (best effort)."""
        try:
            self._request(
                "PATCH",
                f"test/Runs/{run_id}/results",
                json_body=[{"id": result_id, "associatedBugs": [{"id": str(bug_id)}]}],
            )
        except AdoError as exc:
            log.warning("Bug %s created but not associated with the result: %s", bug_id, exc)

    # ------------------------------------------------------------ diagnostics
    def run_diagnostics(self, work_item_type: str = "Bug") -> list[tuple[str, bool, str]]:
        """Read-only pre-flight checks before using the app against a real project."""
        checks: list[tuple[str, bool, str]] = []

        def check(name: str, fn: Callable[[], str]) -> bool:
            try:
                checks.append((name, True, fn()))
                return True
            except Exception as exc:  # reported, not raised
                checks.append((name, False, str(exc)))
                return False

        auth = lambda: (self.authenticate(), f"PAT accepted for {self.config.organization}/{self.config.project}")[1]  # noqa: E731
        if not check("Authentication + test runs readable", auth):
            return checks
        check("Read test plans", lambda: f"{len(self.list_plans())} active plan(s)")
        check(
            f"'{work_item_type}' work item type",
            lambda: f"{len(self.get_work_item_type_fields(work_item_type))} fields; required: "
            + ", ".join(sorted(m['name'] for m in self.get_work_item_type_fields(work_item_type).values() if m['required'])),
        )
        check("Area paths", lambda: f"{len(self.list_classification_paths('areas'))} found")
        check("Iteration paths", lambda: f"{len(self.list_classification_paths('iterations'))} found")
        checks.append(
            (
                "Write access",
                True,
                "Not tested (read-only check). Needs 'Test Management: Read & write' and 'Work Items: Read & write'.",
            )
        )
        return checks

    # ------------------------------------------------------------ orchestration
    def _upload_evidence(self, run_id: int, result_id: int, session: TestSession, comment: Optional[str]) -> list[str]:
        uploaded = []
        target = self.config.attachment_target
        for item in session.attachments():
            note = f"Step {item.step_number}" if item.step_number else (comment or None)
            if target in ("result", "both"):
                if item.step_action_path and self.config.step_attachments:
                    try:
                        self.upload_test_result_attachment(
                            run_id, result_id, item.path, note, iteration_id=1, action_path=item.step_action_path
                        )
                    except AdoError as exc:
                        log.warning("Step attachment rejected (%s) - attaching to the result instead", exc)
                        self.upload_test_result_attachment(run_id, result_id, item.path, note)
                else:
                    self.upload_test_result_attachment(run_id, result_id, item.path, note)
            if target in ("run", "both"):
                self.upload_test_run_attachment(run_id, item.path, note)
            uploaded.append(item.path.name)
        return uploaded

    def record_execution(self, session: TestSession, outcome: str, comment: Optional[str] = None) -> dict[str, Any]:
        """Publish a whole manual execution: run → evidence → step results → complete.

        Bugs already raised for this session are associated with the result.
        If anything fails before the run is completed, the run is aborted so no
        dangling "In progress" runs are left behind.
        """
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome '{outcome}'")
        point = session.point
        run = self.create_test_run(
            point.id,
            plan_id=point.plan_id,
            name=f"{self.config.run_name_prefix} - TC{point.test_case_id} {point.title}"[:256],
        )
        run_id = int(run["id"])
        try:
            result_id = self._result_id_for_point(run_id, point.id)
            uploaded = self._upload_evidence(run_id, result_id, session, comment)
            self.update_test_result(
                run_id,
                result_id,
                outcome,
                comment=comment,
                duration_ms=session.duration_ms,
                started_date=session.started_at,
                iteration_details=[self.build_iteration(session, outcome, comment)] if session.steps else None,
            )
            self.complete_test_run(run_id)
        except Exception:
            self.abort_test_run(run_id)
            raise

        session.run_id, session.result_id = run_id, result_id
        for bug in session.bugs:
            self.associate_bug(run_id, result_id, int(bug["id"]))
        return {
            "run_id": run_id,
            "result_id": result_id,
            "outcome": outcome,
            "attachments": uploaded,
            "url": self.run_web_url(run_id),
            "bugs": list(session.bugs),
        }

    def record_point_outcome(
        self,
        test_point_id: int,
        outcome: str,
        comment: Optional[str] = None,
        attachments: Iterable[str | Path] = (),
        plan_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Quick verdict for a point without step results (no test case loaded)."""
        session = TestSession(
            point=TestPoint(id=int(test_point_id), test_case_id=0, title=f"point {test_point_id}", plan_id=plan_id),
            steps=[],
            evidence=[EvidenceItem(Path(p), "recording") for p in attachments],
        )
        return self.record_execution(session, outcome, comment)
