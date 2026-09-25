"""
demo_data.py - Offline demo for MAS-QA-Bridge (``python main_window.py --demo``).

* ``DemoAdoClient`` is a real ``AdoTestClient`` whose HTTP layer is replaced by
  canned Azure DevOps responses, so the whole UI flow (browse plans, run steps,
  publish, file a bug) works without an organization or PAT - and exercises the
  same parsing/payload code as production.
* ``DemoAppWidget`` is a stand-in "application under test" shown in the host
  frame, so the layout can be previewed on any OS.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional
from xml.sax.saxutils import escape

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ado_test_api import AdoConfig, AdoError, AdoTestClient

log = logging.getLogger(__name__)


def steps_xml(steps: list[tuple[str, str]], shared: Optional[tuple[int, int]] = None) -> str:
    """Build a Microsoft.VSTS.TCM.Steps document (HTML-in-XML, like ADO stores it)."""
    parts = []
    step_id = 2
    for i, (action, expected) in enumerate(steps):
        if shared and i == shared[0]:
            parts.append(f'<compref id="{step_id}" ref="{shared[1]}" />')
            step_id += 1
        kind = "ValidateStep" if expected else "ActionStep"
        parts.append(
            f'<step id="{step_id}" type="{kind}">'
            f'<parameterizedString isformatted="true">{escape(f"<DIV><P>{action}</P></DIV>")}</parameterizedString>'
            f'<parameterizedString isformatted="true">{escape(f"<P>{expected}</P>" if expected else "")}</parameterizedString>'
            "<description/></step>"
        )
        step_id += 1
    return f'<steps id="0" last="{step_id - 1}">{"".join(parts)}</steps>'


SHARED_LOGIN = (
    9001,
    "Log in as a standard user",
    steps_xml(
        [
            ("Launch Contoso Orders", "The login screen is shown"),
            ("Enter user <b>qa.user</b> and the password from the vault, press Sign in", "Dashboard opens with the user's name in the header"),
        ]
    ),
)

TEST_CASES: dict[int, tuple[str, str]] = {
    4101: (
        "Create a new order with valid data",
        steps_xml(
            [
                ("Open <i>Orders → New order</i>", "Empty order form is displayed"),
                ("Select customer <b>Fabrikam Ltd</b>", "Customer address is auto-filled"),
                ("Select product <b>Widget Pro</b> and quantity <b>5</b>", "Line total shows 5 × unit price"),
                ("Pick a delivery date 3 days from today", "Date is accepted, no warning"),
                ("Click <b>Submit order</b>", "Confirmation banner with a new order number appears"),
                ("Open <i>Recent orders</i>", "The new order is listed with status <b>Pending</b>"),
            ],
            shared=(0, SHARED_LOGIN[0]),
        ),
    ),
    4102: (
        "Order quantity above stock shows a warning",
        steps_xml(
            [
                ("Open a new order for <b>Widget Pro</b>", "Form is displayed"),
                ("Enter quantity 999", "Inline warning 'Only 120 in stock'"),
                ("Click Submit order", "Submission is blocked"),
            ]
        ),
    ),
    4103: ("Delivery date in the past is rejected", steps_xml([("Pick yesterday as delivery date", "Validation error shown")])),
    4104: ("Cancel a pending order", steps_xml([("Open a pending order", ""), ("Click Cancel order and confirm", "Status becomes Cancelled")])),
    4201: ("Login with invalid password is rejected", steps_xml([("Enter a wrong password", "Error 'Invalid credentials'")])),
    4202: ("Account locks after 5 failed attempts", steps_xml([("Fail login 5 times", "Account locked message")])),
    4301: ("Export monthly sales report to Excel", steps_xml([("Open Reports → Sales", ""), ("Click Export", "An .xlsx file is downloaded")])),
}

PLANS = [{"id": 101, "name": "Release 2.4 – Regression"}, {"id": 102, "name": "Hotfix 2.3.7"}]
SUITES = {
    101: [
        {"id": 201, "name": "Release 2.4 – Regression", "parentSuite": None, "suiteType": "staticTestSuite"},
        {"id": 203, "name": "Order Entry", "parentSuite": {"id": 201}, "suiteType": "staticTestSuite"},
        {"id": 202, "name": "Login & Security", "parentSuite": {"id": 201}, "suiteType": "requirementTestSuite"},
        {"id": 204, "name": "Reporting", "parentSuite": {"id": 201}, "suiteType": "queryBasedSuite"},
    ],
    102: [{"id": 301, "name": "Hotfix 2.3.7", "parentSuite": None, "suiteType": "staticTestSuite"}],
}
SUITE_CASES = {203: [(4101, "passed"), (4102, "failed"), (4103, "unspecified"), (4104, "blocked")],
               202: [(4201, "passed"), (4202, "unspecified")], 204: [(4301, "unspecified")], 201: [], 301: []}


BUG_FIELDS = [
    {"referenceName": "System.Title", "name": "Title", "alwaysRequired": True},
    {"referenceName": "System.State", "name": "State", "alwaysRequired": True},
    {"referenceName": "System.AreaPath", "name": "Area Path", "alwaysRequired": True},
    {"referenceName": "System.IterationPath", "name": "Iteration Path", "alwaysRequired": True},
    {"referenceName": "System.AssignedTo", "name": "Assigned To"},
    {"referenceName": "System.Tags", "name": "Tags"},
    {"referenceName": "Microsoft.VSTS.TCM.ReproSteps", "name": "Repro Steps"},
    {"referenceName": "Microsoft.VSTS.TCM.SystemInfo", "name": "System Info"},
    {"referenceName": "Microsoft.VSTS.Build.FoundIn", "name": "Found In"},
    {"referenceName": "Microsoft.VSTS.Common.Priority", "name": "Priority", "allowedValues": ["1", "2", "3", "4"]},
    {
        "referenceName": "Microsoft.VSTS.Common.Severity",
        "name": "Severity",
        "allowedValues": ["1 - Critical", "2 - High", "3 - Medium", "4 - Low"],
    },
]
AREAS = {
    "name": "Orders Portal",
    "children": [{"name": "Order Entry"}, {"name": "Reporting"}, {"name": "Security", "children": [{"name": "Login"}]}],
}
ITERATIONS = {
    "name": "Orders Portal",
    "children": [{"name": "Release 2.4", "children": [{"name": "Sprint 41"}, {"name": "Sprint 42"}]}, {"name": "Backlog"}],
}
SIMULATED_ERROR = "[simulate error]"


class _FakeResponse:
    def __init__(self, payload: Any, headers: Optional[dict[str, str]] = None) -> None:
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.status_code = 200
        self.ok = True

    def json(self) -> Any:
        return self._payload


class DemoAdoClient(AdoTestClient):
    """AdoTestClient backed by canned responses instead of dev.azure.com."""

    def __init__(self, latency: float = 0.25) -> None:
        super().__init__(
            AdoConfig(organization="contoso", project="Orders Portal", pat="demo", test_plan_id=101)
        )
        self.latency = latency
        self.calls: list[tuple[str, str, Any]] = []
        self._runs: dict[int, int] = {}  # run id -> point id
        self._next_run = 5100

    def _send(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:  # type: ignore[override]
        time.sleep(self.latency)
        body = kwargs.get("json_body")
        self.calls.append((method, path, body))
        parts = path.strip("/").split("/")

        if path == "test/runs" and method == "GET":
            return _FakeResponse({"count": 0, "value": []})
        if path == "testplan/plans":
            return _FakeResponse({"value": PLANS})
        if parts[:2] == ["testplan", "Plans"] and parts[-1] == "suites":
            return _FakeResponse({"value": SUITES.get(int(parts[2]), [])})
        if parts[:2] == ["testplan", "Plans"] and parts[-1] == "TestPoint":
            plan_id, suite_id = int(parts[2]), int(parts[4])
            points = [
                {
                    "id": 70000 + case_id,
                    "testCaseReference": {"id": case_id, "name": TEST_CASES[case_id][0]},
                    "configuration": {"name": "Windows 11"},
                    "tester": {"displayName": "QA Engineer"},
                    "results": {"outcome": outcome},
                }
                for case_id, outcome in SUITE_CASES.get(suite_id, [])
            ]
            return _FakeResponse({"value": points})
        if parts[:2] == ["wit", "workitems"] and method == "GET":
            wid = int(parts[2])
            if wid == SHARED_LOGIN[0]:
                title, xml = SHARED_LOGIN[1], SHARED_LOGIN[2]
            else:
                title, xml = TEST_CASES[wid]
            return _FakeResponse({"id": wid, "fields": {"System.Title": title, "Microsoft.VSTS.TCM.Steps": xml}})
        if path == "test/runs" and method == "POST":
            self._next_run += 1
            self._runs[self._next_run] = body["pointIds"][0]
            return _FakeResponse({"id": self._next_run, "state": "InProgress"})
        if parts[0] == "test" and parts[-1] == "results" and method == "GET":
            run_id = int(parts[2])
            return _FakeResponse({"value": [{"id": 100000, "testPoint": {"id": str(self._runs.get(run_id))}}]})
        if parts[-1] == "attachments" and parts[0] == "wit":
            return _FakeResponse({"id": "demo", "url": f"{self.project_url}/_apis/wit/attachments/demo"})
        if parts[-1] == "attachments":
            return _FakeResponse({"id": len(self.calls), "url": f"{self.project_url}/_apis/test/attachments/demo"})
        if path == "wit/workitemtypes/Bug/fields":
            return _FakeResponse({"value": BUG_FIELDS})
        if path == "wit/classificationnodes/areas":
            return _FakeResponse(AREAS)
        if path == "wit/classificationnodes/iterations":
            return _FakeResponse(ITERATIONS)
        if path == "wit/workitems/$Bug":
            title = next((op["value"] for op in body if op["path"] == "/fields/System.Title"), "")
            if SIMULATED_ERROR in title:
                # What ADO returns when a process rule rejects a field value.
                raise AdoError(
                    "POST wit/workitems/$Bug -> HTTP 400: TF401320: Rule Error for field Area Path. "
                    "Error code: Required, HasValues, LimitedToValues, AllowsOldValue, InvalidEmpty.",
                    400,
                )
            return _FakeResponse({"id": 8800 + len(self.calls)})
        return _FakeResponse({"value": []})


# ----------------------------------------------------------------------------
class DemoAppWidget(QWidget):
    """A fake line-of-business app ("Contoso Orders") shown in the host frame."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("demoApp")
        self.setStyleSheet(
            """
            QWidget#demoApp { background: #f5f6f8; }
            QWidget#demoApp QLabel { color: #1f2328; background: transparent; font-size: 13px; }
            QWidget#demoApp QFrame#nav { background: #0f4c81; }
            QWidget#demoApp QFrame#nav QLabel { color: #dce9f7; padding: 7px 14px; }
            QWidget#demoApp QFrame#nav QLabel#navActive { background: #1a64a8; color: white; font-weight: 600; }
            QWidget#demoApp QFrame#nav QLabel#appTitle { color: white; font-weight: 700; font-size: 15px; padding: 14px; }
            QWidget#demoApp QFrame#card { background: white; border: 1px solid #d8dde3; border-radius: 6px; }
            QWidget#demoApp QLineEdit, QWidget#demoApp QComboBox, QWidget#demoApp QSpinBox, QWidget#demoApp QDateEdit {
                background: white; color: #1f2328; border: 1px solid #c3cad3; border-radius: 4px; padding: 4px; }
            QWidget#demoApp QPushButton#submit { background: #0f6cbd; color: white; border: none; border-radius: 4px;
                padding: 7px 18px; font-weight: 600; }
            QWidget#demoApp QLabel#banner { background: #dff6dd; color: #0e700e; border: 1px solid #9fd89f;
                border-radius: 4px; padding: 6px 10px; }
            QWidget#demoApp QTableWidget { background: white; color: #1f2328; border: 1px solid #d8dde3; gridline-color: #eceff2; }
            QWidget#demoApp QHeaderView::section { background: #eef1f4; color: #1f2328; border: none; padding: 5px; font-weight: 600; }
            """
        )
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(170)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        title = QLabel("Contoso Orders")
        title.setObjectName("appTitle")
        nav_layout.addWidget(title)
        for name in ("Dashboard", "New order", "Recent orders", "Customers", "Reports", "Settings"):
            label = QLabel(name)
            if name == "New order":
                label.setObjectName("navActive")
            nav_layout.addWidget(label)
        nav_layout.addStretch()
        root.addWidget(nav)

        content = QVBoxLayout()
        content.setContentsMargins(22, 18, 22, 18)
        content.setSpacing(12)
        header = QLabel("<span style='font-size:18px; font-weight:600'>New order</span>")
        content.addWidget(header)
        banner = QLabel("✔ Order SO-24817 created successfully.")
        banner.setObjectName("banner")
        content.addWidget(banner)

        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(16, 14, 16, 14)
        form.setHorizontalSpacing(18)
        customer = QComboBox()
        customer.addItems(["Fabrikam Ltd", "Northwind Traders", "Adventure Works"])
        form.addRow("Customer", customer)
        form.addRow("Address", QLineEdit("12 Harbour Road, Seattle, WA"))
        product = QComboBox()
        product.addItems(["Widget Pro", "Widget Lite", "Gizmo X"])
        form.addRow("Product", product)
        qty = QSpinBox()
        qty.setRange(1, 999)
        qty.setValue(5)
        form.addRow("Quantity", qty)
        date = QDateEdit(QDate.currentDate().addDays(3))
        date.setCalendarPopup(True)
        form.addRow("Delivery date", date)
        total = QLabel("<b>$ 1,245.00</b>  (5 × $249.00)")
        form.addRow("Line total", total)
        submit = QPushButton("Submit order")
        submit.setObjectName("submit")
        submit.setFixedWidth(150)
        form.addRow("", submit)
        content.addWidget(card)

        content.addWidget(QLabel("<b>Recent orders</b>"))
        table = QTableWidget(4, 4)
        table.setHorizontalHeaderLabels(["Order", "Customer", "Total", "Status"])
        rows = [
            ("SO-24817", "Fabrikam Ltd", "$1,245.00", "Pending"),
            ("SO-24816", "Northwind Traders", "$310.50", "Shipped"),
            ("SO-24815", "Adventure Works", "$89.99", "Delivered"),
            ("SO-24814", "Fabrikam Ltd", "$2,040.00", "Delivered"),
        ]
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(value))
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(1, 180)
        content.addWidget(table, stretch=1)
        root.addLayout(content, stretch=1)

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
