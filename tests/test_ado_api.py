"""Offline tests for the ADO client, driven by the demo transport (no network)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ado_test_api import AdoConfig, html_to_text, parse_steps_xml  # noqa: E402
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


def test_record_execution_sends_step_results_and_bug(client, tmp_path):
    session = client.load_session(client.list_points(101, 203)[0])
    for i in range(len(session.steps)):
        session.set_step_outcome(i, "Passed")
    session.set_step_outcome(5, "Failed", "order number missing")
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    session.evidence.append(EvidenceItem(shot, "screenshot", 6, session.steps[5].action_path))

    report = client.record_execution(session, "Failed", "see step 6", create_bug=True)

    assert report["bug"]["id"] and report["attachments"] == ["s.png"]
    update = next(body for m, p, body in client.calls if m == "PATCH" and p.endswith("/results") and "iterationDetails" in body[0])
    actions = update[0]["iterationDetails"][0]["actionResults"]
    assert len(actions) == 8
    failed = [a for a in actions if a["outcome"] == "Failed"]
    assert failed[0]["actionPath"] == "00000006" and failed[0]["errorMessage"] == "order number missing"
    bug_ops = next(body for m, p, body in client.calls if p == "wit/workitems/$Bug")
    rels = [op["value"]["rel"] for op in bug_ops if op["path"] == "/relations/-"]
    assert rels == ["Microsoft.VSTS.Common.TestedBy-Forward", "AttachedFile"]


def test_config_prefers_env_pat(monkeypatch):
    monkeypatch.setenv("ADO_PAT", "from-env")
    cfg = AdoConfig.from_dict({"organization": "o", "project": "p", "pat": "from-file"})
    assert cfg.pat == "from-env"
