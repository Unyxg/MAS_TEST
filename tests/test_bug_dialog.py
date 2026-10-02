"""The bug form must recover from a rejected required field (the real 'Created by Team' error)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication, QComboBox  # noqa: E402

from bug_dialog import BugDialog  # noqa: E402
from bug_report import BugReport  # noqa: E402
from demo_data import DemoAdoClient  # noqa: E402
from test_ado_api import REAL_ERROR  # noqa: E402

app = QApplication.instance() or QApplication([])


@pytest.fixture
def client():
    return DemoAdoClient(latency=0)


def _report():
    return BugReport(title="Open log - unexpected behaviour", steps=["open"], actual="error window", assigned_to="a@b.c",
                     found_in="1.0", test_case_id=383128)


def test_form_asks_for_every_required_field_up_front(client, tmp_path):
    dialog = BugDialog(_report(), client.bug_metadata(), tmp_path)
    assert set(dialog._extra_widgets) == {"Custom.CreatedByTeam", "Custom.DetectedInPhase", "Custom.RequirementAddOn"}
    assert dialog._extra_widgets["Custom.CreatedByTeam"].currentText() == "Platform"  # the default is pre-selected


def test_rejected_field_is_added_flagged_and_sent_on_retry(client, tmp_path):
    meta = client.bug_metadata()
    meta["required_custom"] = []  # simulate detection that missed the field
    meta["all_required"] = [f for f in client.required_user_fields() if f["name"] != "Created by Team"]
    dialog = BugDialog(_report(), meta, tmp_path)
    assert dialog._extra_widgets == {}

    dialog.submission_failed(REAL_ERROR)

    assert "Custom.CreatedByTeam" in dialog._extra_widgets
    assert {"Custom.DetectedInPhase", "Custom.RequirementAddOn"} <= set(dialog._extra_widgets)  # the hidden second error
    assert "Created by Team" in dialog.message.text() and "Draft saved" in dialog.message.text()
    assert list(tmp_path.glob("bug_draft_*.json"))

    widget = dialog._extra_widgets["Custom.CreatedByTeam"]
    assert isinstance(widget, QComboBox) and widget.currentText() == "Platform"
    widget.setCurrentText("Checkout")
    dialog._extra_widgets["Custom.DetectedInPhase"].setCurrentText("UAT")
    dialog._extra_widgets["Custom.RequirementAddOn"].setText("n/a")
    report = dialog.collect()
    assert report.extra_fields["Custom.CreatedByTeam"] == "Checkout"

    client.create_bug(report)
    ops = next(body for m, p, body in client.calls if p == "wit/workitems/$Bug")
    fields = {op["path"]: op["value"] for op in ops if op["path"].startswith("/fields/")}
    assert fields["/fields/Custom.CreatedByTeam"] == "Checkout"


def test_unknown_field_in_error_is_explained(client, tmp_path):
    dialog = BugDialog(_report(), client.bug_metadata(), tmp_path)
    dialog.submission_failed(REAL_ERROR.replace("Created by Team", "Some Unknown Field"))
    assert "extra_fields" in dialog.message.text()
