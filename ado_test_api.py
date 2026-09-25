"""
ado_test_api.py - Azure DevOps Test Plans integration for MAS-QA-Bridge.

Thin client over the Azure DevOps Test Management REST API (api-version 7.1):

    authenticate()                      -> verify PAT + project access
    create_test_run(test_point_id)      -> POST  {project}/_apis/test/runs
    get_test_results(run_id)            -> GET   {project}/_apis/test/Runs/{run}/results
    update_test_result(run_id, ...)     -> PATCH {project}/_apis/test/Runs/{run}/results
    upload_test_run_attachment(...)     -> POST  {project}/_apis/test/Runs/{run}/attachments
    upload_test_result_attachment(...)  -> POST  {project}/_apis/test/Runs/{run}/Results/{id}/attachments
    complete_test_run(run_id)           -> PATCH {project}/_apis/test/runs/{run}

``record_point_outcome`` chains them into the single "Pass / Fail" action the
UI buttons use. Creating a run from a test point makes ADO update that point's
outcome in the Test Plans "Execute" tab once the result is completed.
"""
from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import requests

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
            timeout=float(data.get("timeout_seconds") or cls.timeout),
            verify_ssl=bool(data.get("verify_ssl", True)),
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AdoTestClient:
    """Azure DevOps Test Management client authenticated with a PAT."""

    def __init__(self, config: AdoConfig) -> None:
        self.config = config
        self.session = requests.Session()
        # PAT auth = HTTP Basic with an empty user name.
        self.session.auth = ("", config.pat)
        self.session.headers.update({"Accept": "application/json", "User-Agent": "MAS-QA-Bridge/1.0"})
        self.session.verify = config.verify_ssl

    # ----------------------------------------------------------------- basics
    @property
    def org_url(self) -> str:
        return f"{self.config.base_url}/{quote(self.config.organization, safe='')}"

    @property
    def project_url(self) -> str:
        return f"{self.org_url}/{quote(self.config.project, safe='')}"

    def run_web_url(self, run_id: int) -> str:
        return f"{self.project_url}/_testManagement/runs?_a=runCharts&runId={run_id}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[dict[str, Any]] = None,
        api_version: Optional[str] = None,
    ) -> Any:
        url = f"{self.project_url}/_apis/{path.lstrip('/')}"
        query = {"api-version": api_version or self.config.api_version, **(params or {})}
        try:
            resp = self.session.request(method, url, params=query, json=json, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise AdoError(f"{method} {url} failed: {exc}") from exc

        # A bad/expired PAT yields 401, or 203 + an HTML sign-in page.
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code in (401, 203) or (resp.ok and "text/html" in content_type):
            raise AdoError(
                "Authentication failed - check the PAT value, expiry and "
                "'Test Management (Read & write)' scope.",
                resp.status_code,
            )
        if not resp.ok:
            try:
                detail = resp.json().get("message", resp.text)
            except ValueError:
                detail = resp.text
            raise AdoError(f"{method} {url} -> HTTP {resp.status_code}: {detail[:500]}", resp.status_code)
        return resp.json() if resp.content else {}

    def authenticate(self) -> dict[str, Any]:
        """Verify the PAT can read test runs in the project; returns a small summary."""
        data = self._request("GET", "test/runs", params={"$top": 1})
        log.info("Authenticated to %s/%s", self.config.organization, self.config.project)
        return {"organization": self.config.organization, "project": self.config.project, "sample_runs": data.get("count", 0)}

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
        run = self._request("POST", "test/runs", json=body)
        log.info("Created test run %s for point %s (plan %s)", run.get("id"), test_point_id, plan_id)
        return run

    def complete_test_run(self, run_id: int, comment: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"state": "Completed", "completedDate": _utc_now()}
        if comment:
            body["comment"] = comment
        return self._request("PATCH", f"test/runs/{run_id}", json=body)

    def abort_test_run(self, run_id: int) -> None:
        try:
            self._request("PATCH", f"test/runs/{run_id}", json={"state": "Aborted"})
        except AdoError as exc:
            log.warning("Could not abort run %s: %s", run_id, exc)

    # ------------------------------------------------------------ test results
    def get_test_results(self, run_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"test/Runs/{run_id}/results").get("value", [])

    def update_test_result(
        self,
        run_id: int,
        result_id: int,
        outcome: str,
        comment: Optional[str] = None,
        error_message: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Set the outcome of a test result and mark it Completed."""
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome '{outcome}'. Expected one of {sorted(VALID_OUTCOMES)}")
        result: dict[str, Any] = {
            "id": int(result_id),
            "outcome": outcome,
            "state": "Completed",
            "completedDate": _utc_now(),
        }
        if comment:
            result["comment"] = comment
        if error_message:
            result["errorMessage"] = error_message
        if duration_ms is not None:
            result["durationInMs"] = duration_ms
        data = self._request("PATCH", f"test/Runs/{run_id}/results", json=[result])
        log.info("Result %s in run %s set to %s", result_id, run_id, outcome)
        return data.get("value", [])

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
            json=self._attachment_body(file_path, comment, attachment_type),
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
    ) -> dict[str, Any]:
        """Attach a file to a single test result (shown alongside the outcome)."""
        data = self._request(
            "POST",
            f"test/Runs/{run_id}/Results/{result_id}/attachments",
            json=self._attachment_body(file_path, comment, attachment_type),
            api_version=self.config.attachment_api_version,
        )
        log.info("Uploaded %s to result %s of run %s", Path(file_path).name, result_id, run_id)
        return data

    # ------------------------------------------------------------ orchestration
    def record_point_outcome(
        self,
        test_point_id: int,
        outcome: str,
        comment: Optional[str] = None,
        attachments: Iterable[str | Path] = (),
        plan_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Create run -> upload evidence -> set outcome -> complete run.

        Returns ``{"run_id", "result_id", "outcome", "attachments", "url"}``.
        If anything fails after the run was created, the run is aborted so no
        dangling "In progress" runs are left behind.
        """
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Invalid outcome '{outcome}'")

        run = self.create_test_run(test_point_id, plan_id=plan_id)
        run_id = int(run["id"])
        try:
            results = self.get_test_results(run_id)
            if not results:
                raise AdoError(f"Run {run_id} has no results - is point {test_point_id} in the plan?")
            result = next(
                (r for r in results if str((r.get("testPoint") or {}).get("id")) == str(test_point_id)),
                results[0],
            )
            result_id = int(result["id"])

            uploaded = []
            target = self.config.attachment_target
            for file_path in attachments:
                if target in ("result", "both"):
                    self.upload_test_result_attachment(run_id, result_id, file_path, comment)
                if target in ("run", "both"):
                    self.upload_test_run_attachment(run_id, file_path, comment)
                uploaded.append(Path(file_path).name)

            self.update_test_result(run_id, result_id, outcome, comment=comment)
            self.complete_test_run(run_id)
        except Exception:
            self.abort_test_run(run_id)
            raise

        return {
            "run_id": run_id,
            "result_id": result_id,
            "outcome": outcome,
            "attachments": uploaded,
            "url": self.run_web_url(run_id),
        }
