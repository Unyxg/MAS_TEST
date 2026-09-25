"""Offline tests for the ADO client, driven by the demo transport (no network)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ado_test_api import AdoConfig, AdoError, html_to_text, parse_steps_xml  # noqa: E402
from bug_report import BugReport  # noqa: E402
from demo_data import DemoAdoClient  # noqa: E402
from qa_session import EvidenceItem  # noqa: E402


@pytest.fixture
def client():
    return DemoAdoClient(latency=0)


def test_html_to_text_strips_markup():
    assert html_to_text("<DIV><P>Click <b>Save</b>&nbsp;now</P></DIV>") == "Click Save now"


def test_shared_steps_are_expanded_with_compound_action_path(client):
    steps = client.get_test_case_steps(4101)
    assert len(steps) == 8
    assert steps[0].action_path == "0000000200000002"
    assert steps[0].step_identifier == "2;2"
    assert steps[0].shared_title == "Log in as a standard user"
    assert steps[2].action_path == "00000003" and steps[2].expected == "Empty order form is displayed"


def test_unexpanded_compref_becomes_placeholder_step():
    xml = '<steps id="0" last="3"><compref id="2" ref="77" /><step id="3" type="ActionStep">' \
          '<parameterizedString isformatted="true">Go</parameterizedString><parameterizedString/></step></steps>'
    steps = parse_steps_xml(xml)
    assert [s.action for s in steps] == ["Shared steps #77", "Go"]


def test_browse_hierarchy(client):
    assert client.list_plans()[0]["id"] in (101, 102)
    suites = client.list_suites(101)
    assert {s["parent_id"] for s in suites} == {None, 201}
    points = client.list_points(101, 203)
    assert points[1].outcome == "Failed" and points[0].test_case_id == 4101


def _executed_session(client, tmp_path):
    session = client.load_session(client.list_points(101, 203)[0])
    for i in range(len(session.steps)):
        session.set_step_outcome(i, "Passed")
    session.set_step_outcome(5, "Failed", "order number missing")
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    session.evidence.append(EvidenceItem(shot, "screenshot", 6, session.steps[5].action_path))
    return session


def test_record_execution_sends_step_results(client, tmp_path):
    session = _executed_session(client, tmp_path)
    session.bugs.append({"id": 42, "url": "u", "title": "t"})

    report = client.record_execution(session, "Failed", "see step 6")

    assert report["attachments"] == ["s.png"] and session.run_id == report["run_id"]
    update = next(body for m, p, body in client.calls if m == "PATCH" and p.endswith("/results") and "iterationDetails" in body[0])
    actions = update[0]["iterationDetails"][0]["actionResults"]
    assert len(actions) == 8
    failed = [a for a in actions if a["outcome"] == "Failed"]
    assert failed[0]["actionPath"] == "00000006" and failed[0]["errorMessage"] == "order number missing"
    assert any(isinstance(body, list) and body[0].get("associatedBugs") == [{"id": "42"}] for m, p, body in client.calls if m == "PATCH")


def test_bug_report_prefilled_from_failing_step(client, tmp_path):
    session = _executed_session(client, tmp_path)
    report = BugReport.from_session(session, None, {"Application version": "2.4.1"}, "2.4.1")
    assert report.failing_step == 6 and len(report.steps) == 6
    assert report.actual == "order number missing"
    assert report.expected == "Date is accepted, no warning"
    assert report.attachments[0].name == "s.png"
    assert report.validate()[0] == []
    html = report.repro_html({"s.png": "https://img"})
    assert "fails here" in html and "<img src='https://img'" in html and "2.4.1" in html


def test_create_bug_payload(client, tmp_path):
    session = _executed_session(client, tmp_path)
    client.record_execution(session, "Failed")
    report = BugReport.from_session(session, 5, {"OS": "Windows 11"}, "2.4.1")
    report.area_path = "Orders Portal\\Order Entry"
    bug = client.create_bug(report)

    ops = next(body for m, p, body in client.calls if p == "wit/workitems/$Bug")
    fields = {op["path"]: op["value"] for op in ops if op["path"].startswith("/fields/")}
    assert fields["/fields/Microsoft.VSTS.Build.FoundIn"] == "2.4.1"
    assert fields["/fields/Microsoft.VSTS.Common.Severity"] == "3 - Medium"
    assert fields["/fields/System.AreaPath"] == "Orders Portal\\Order Entry"
    rels = [op["value"]["rel"] for op in ops if op["path"] == "/relations/-"]
    assert rels == ["Microsoft.VSTS.Common.TestedBy-Forward", "Hyperlink", "AttachedFile"]
    assert bug["id"] and bug["skipped_fields"] == []
    # created after publishing -> associated with the result
    assert any(isinstance(body, list) and body[0].get("associatedBugs") for m, p, body in client.calls if m == "PATCH")


def test_create_bug_rejects_incomplete_report(client):
    with pytest.raises(AdoError, match="Title is required"):
        client.create_bug(BugReport(title="x", steps=["a"], actual="b"))


def test_create_bug_surfaces_server_rule_errors(client, tmp_path):
    report = BugReport(title="[simulate error] something broke", steps=["open"], actual="crash")
    with pytest.raises(AdoError, match="TF401320"):
        client.create_bug(report)
    draft = report.save_draft(tmp_path, "TF401320")
    assert BugReport.load_draft(draft).title == report.title


def test_diagnostics_all_green(client):
    checks = client.run_diagnostics()
    assert all(ok for _, ok, _ in checks), checks


def test_config_prefers_env_pat(monkeypatch):
    monkeypatch.setenv("ADO_PAT", "from-env")
    cfg = AdoConfig.from_dict({"organization": "o", "project": "p", "pat": "from-file"})
    assert cfg.pat == "from-env"
