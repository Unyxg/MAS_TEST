"""
widgets.py - Reusable UI building blocks for the MAS-QA-Bridge dashboard.

    HostFrame      central frame that hosts the application under test
    TestExplorer   plan -> suite tree -> test points (left column)
    StepRunner     test case header + step cards with Pass/Fail/screenshot
    EvidencePanel  record / screenshot controls and captured evidence list
    PublishPanel   outcome, comment, "file bug" and Publish button
    StatusPill     small coloured status badge
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from qa_session import FAILED, PASSED, UNSPECIFIED, EvidenceItem, TestPoint, TestSession
from theme import OUTCOME_COLORS

OUTCOME_CHOICES = ["Auto (from steps)", "Passed", "Failed", "Blocked", "NotApplicable"]


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property change."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def dot_icon(color: str, size: int = 10) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return QIcon(pixmap)


def panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 12)
    layout.setSpacing(8)
    label = QLabel(title.upper())
    label.setObjectName("panelTitle")
    layout.addWidget(label)
    return frame, layout


class StatusPill(QLabel):
    def __init__(self, text: str = "", kind: str = "idle", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("pill")
        self.set_state(text, kind)

    def set_state(self, text: str, kind: str = "idle") -> None:
        self.setText(text)
        self.setProperty("kind", kind)
        repolish(self)


# ----------------------------------------------------------------------------
class HostFrame(QFrame):
    """Central frame whose native HWND hosts the external application."""

    resized = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("hostFrame")
        self.setMinimumSize(480, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Give this frame (only) its own native window handle for SetParent.
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        self.placeholder = QLabel(
            "<div style='font-size:34px'>⧉</div>"
            "<p><b>No application under test</b></p>"
            "<p>Browse to an executable and press <b>Launch</b>.<br>"
            "Its window will be embedded here.</p>",
            self,
        )
        self.placeholder.setObjectName("placeholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def native_handle(self) -> int:
        return int(self.winId())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.placeholder.setGeometry(self.rect())
        self.resized.emit()


# ----------------------------------------------------------------------------
class TestExplorer(QFrame):
    """Plan selector, suite tree and the test points of the selected suite."""

    plan_selected = pyqtSignal(int)
    suite_selected = pyqtSignal(int, int)  # plan id, suite id
    point_selected = pyqtSignal(object)  # TestPoint
    refresh_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("TEST EXPLORER")
        title.setObjectName("panelTitle")
        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳")
        self.refresh_btn.setToolTip("Reload plans, suites and points from Azure DevOps")
        self.refresh_btn.clicked.connect(self.refresh_requested)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.refresh_btn)
        layout.addLayout(head)

        self.plan_combo = QComboBox()
        self.plan_combo.setPlaceholderText("Select a test plan")
        self.plan_combo.currentIndexChanged.connect(self._on_plan_changed)
        layout.addWidget(self.plan_combo)

        self.suite_tree = QTreeWidget()
        self.suite_tree.setHeaderHidden(True)
        self.suite_tree.setIndentation(14)
        self.suite_tree.setMaximumHeight(170)
        self.suite_tree.currentItemChanged.connect(self._on_suite_changed)
        layout.addWidget(self.suite_tree)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter test cases…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.point_list = QListWidget()
        self.point_list.setIconSize(QSize(10, 10))
        self.point_list.setWordWrap(True)
        self.point_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.point_list.itemActivated.connect(self._on_point_activated)
        self.point_list.itemClicked.connect(self._on_point_activated)
        layout.addWidget(self.point_list, stretch=1)

        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self._points: list[TestPoint] = []
        self._plan_id: Optional[int] = None

    # ------------------------------------------------------------------ data
    def set_message(self, text: str) -> None:
        self.summary.setText(text)

    def set_plans(self, plans: list[dict], select_id: Optional[int] = None) -> None:
        self.plan_combo.blockSignals(True)
        self.plan_combo.clear()
        for plan in plans:
            self.plan_combo.addItem(f"{plan['name']}  (#{plan['id']})", plan["id"])
        index = self.plan_combo.findData(select_id) if select_id else -1
        if index < 0 and plans:
            index = 0
        self.plan_combo.setCurrentIndex(index)
        self.plan_combo.blockSignals(False)
        if index >= 0:
            self._on_plan_changed(index)

    def set_suites(self, plan_id: int, suites: list[dict]) -> None:
        self._plan_id = plan_id
        self.suite_tree.blockSignals(True)
        self.suite_tree.clear()
        items: dict[int, QTreeWidgetItem] = {}
        for suite in suites:
            item = QTreeWidgetItem([suite["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, suite["id"])
            item.setToolTip(0, f"Suite #{suite['id']} · {suite.get('type', '')}")
            items[suite["id"]] = item
        for suite in suites:
            parent = items.get(suite.get("parent_id"))
            if parent is not None:
                parent.addChild(items[suite["id"]])
            else:
                self.suite_tree.addTopLevelItem(items[suite["id"]])
        self.suite_tree.expandAll()
        self.suite_tree.blockSignals(False)
        # Prefer the first child suite (the root suite is usually empty).
        first = self.suite_tree.topLevelItem(0)
        if first is not None:
            self.suite_tree.setCurrentItem(first.child(0) or first)

    def set_points(self, points: list[TestPoint]) -> None:
        self._points = points
        self.point_list.clear()
        for point in points:
            text = f"{point.title}\nTC {point.test_case_id}" + (f" · {point.configuration}" if point.configuration else "")
            item = QListWidgetItem(dot_icon(OUTCOME_COLORS.get(point.outcome, OUTCOME_COLORS[UNSPECIFIED])), text)
            item.setData(Qt.ItemDataRole.UserRole, point)
            item.setToolTip(f"Point #{point.id} · last outcome: {point.outcome}\nTester: {point.tester or '—'}")
            self.point_list.addItem(item)
        self._apply_filter(self.filter_edit.text())
        self._update_summary()

    def set_point_outcome(self, point_id: int, outcome: str) -> None:
        for i in range(self.point_list.count()):
            item = self.point_list.item(i)
            point: TestPoint = item.data(Qt.ItemDataRole.UserRole)
            if point.id == point_id:
                point.outcome = outcome
                item.setIcon(dot_icon(OUTCOME_COLORS.get(outcome, OUTCOME_COLORS[UNSPECIFIED])))
        self._update_summary()

    def select_point(self, point_id: int) -> None:
        for i in range(self.point_list.count()):
            if self.point_list.item(i).data(Qt.ItemDataRole.UserRole).id == point_id:
                self.point_list.setCurrentRow(i)

    # ------------------------------------------------------------- handlers
    def _update_summary(self) -> None:
        counts: dict[str, int] = {}
        for p in self._points:
            counts[p.outcome] = counts.get(p.outcome, 0) + 1
        parts = [f"{len(self._points)} test points"]
        for outcome in ("Passed", "Failed", "Blocked"):
            if counts.get(outcome):
                parts.append(f"<span style='color:{OUTCOME_COLORS[outcome]}'>{counts[outcome]} {outcome.lower()}</span>")
        self.summary.setText(" · ".join(parts))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.point_list.count()):
            item = self.point_list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _on_plan_changed(self, index: int) -> None:
        plan_id = self.plan_combo.itemData(index)
        if plan_id is not None:
            self.plan_selected.emit(int(plan_id))

    def _on_suite_changed(self, current: Optional[QTreeWidgetItem], _previous: object) -> None:
        if current is not None and self._plan_id is not None:
            self.suite_selected.emit(self._plan_id, int(current.data(0, Qt.ItemDataRole.UserRole)))

    def _on_point_activated(self, item: QListWidgetItem) -> None:
        self.point_selected.emit(item.data(Qt.ItemDataRole.UserRole))


# ----------------------------------------------------------------------------
class StepCard(QFrame):
    outcome_clicked = pyqtSignal(int, str)
    comment_edited = pyqtSignal(int, str)
    screenshot_clicked = pyqtSignal(int)
    activated = pyqtSignal(int)

    def __init__(self, index: int, number: int, action: str, expected: str, shared_title: Optional[str]) -> None:
        super().__init__()
        self.index = index
        self.setObjectName("stepCard")
        self.setProperty("current", False)
        self.setProperty("outcome", UNSPECIFIED)

        grid = QHBoxLayout(self)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setSpacing(10)
        badge = QLabel(str(number))
        badge.setObjectName("stepNo")
        grid.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setSpacing(3)
        if shared_title:
            tag = QLabel(f"↳ Shared steps: {shared_title}")
            tag.setObjectName("sharedTag")
            body.addWidget(tag)
        action_label = QLabel(action or "—")
        action_label.setWordWrap(True)
        body.addWidget(action_label)
        if expected:
            expected_label = QLabel(f"Expected: {expected}")
            expected_label.setObjectName("expected")
            expected_label.setWordWrap(True)
            body.addWidget(expected_label)
        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("Actual result (why it failed)…")
        self.comment_edit.setVisible(False)
        self.comment_edit.editingFinished.connect(lambda: self.comment_edited.emit(self.index, self.comment_edit.text()))
        body.addWidget(self.comment_edit)
        grid.addLayout(body, stretch=1)

        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.pass_btn = self._tool("✓", "stepPass", "Pass this step (F5)", checkable=True)
        self.fail_btn = self._tool("✗", "stepFail", "Fail this step (F6)", checkable=True)
        self.shot_btn = self._tool("📷", "stepShot", "Screenshot for this step (F7)")
        self.pass_btn.clicked.connect(lambda: self._toggle(PASSED))
        self.fail_btn.clicked.connect(lambda: self._toggle(FAILED))
        self.shot_btn.clicked.connect(lambda: self.screenshot_clicked.emit(self.index))
        row.addWidget(self.pass_btn)
        row.addWidget(self.fail_btn)
        row.addWidget(self.shot_btn)
        buttons.addLayout(row)
        self.shot_count = QLabel("")
        self.shot_count.setObjectName("muted")
        self.shot_count.setAlignment(Qt.AlignmentFlag.AlignRight)
        buttons.addWidget(self.shot_count)
        buttons.addStretch()
        grid.addLayout(buttons)

    def _tool(self, text: str, name: str, tip: str, checkable: bool = False) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setObjectName(name)
        btn.setToolTip(tip)
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        return btn

    def _toggle(self, outcome: str) -> None:
        current = self.property("outcome")
        self.outcome_clicked.emit(self.index, UNSPECIFIED if current == outcome else outcome)

    def mousePressEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self.activated.emit(self.index)
        super().mousePressEvent(event)

    def set_outcome(self, outcome: str, comment: str = "") -> None:
        self.setProperty("outcome", outcome)
        self.pass_btn.setChecked(outcome == PASSED)
        self.fail_btn.setChecked(outcome == FAILED)
        self.comment_edit.setVisible(outcome == FAILED)
        if comment != self.comment_edit.text():
            self.comment_edit.setText(comment)
        repolish(self)

    def set_current(self, current: bool) -> None:
        self.setProperty("current", current)
        repolish(self)

    def set_evidence_count(self, count: int) -> None:
        self.shot_count.setText(f"{count} 📎" if count else "")


class StepRunner(QFrame):
    step_outcome = pyqtSignal(int, str)
    step_comment = pyqtSignal(int, str)
    screenshot_requested = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(6)
        title = QLabel("TEST RUNNER")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self.case_id = QLabel("")
        self.case_id.setObjectName("caseId")
        self.case_title = QLabel("Select a test point in the Test Explorer")
        self.case_title.setObjectName("caseTitle")
        self.case_title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        self.meta.setWordWrap(True)
        layout.addWidget(self.case_id)
        layout.addWidget(self.case_title)
        layout.addWidget(self.meta)

        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("muted")
        progress_row.addWidget(self.progress, stretch=1)
        progress_row.addWidget(self.progress_label)
        layout.addLayout(progress_row)
        keys = QLabel("F5 pass · F6 fail · F7 screenshot · F9 record · Ctrl+↵ publish")
        keys.setObjectName("muted")
        keys.setStyleSheet("font-size: 11px;")
        layout.addWidget(keys)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 4, 14, 4)
        self.cards_layout.setSpacing(6)
        self.cards_layout.addStretch()
        self.scroll.setWidget(self.cards_host)
        layout.addWidget(self.scroll, stretch=1)

        self.cards: list[StepCard] = []
        self.current = -1
        self.session: Optional[TestSession] = None

    def load(self, session: Optional[TestSession]) -> None:
        self.session = session
        for card in self.cards:
            card.deleteLater()
        self.cards = []
        self.current = -1
        if session is None:
            self.case_id.setText("")
            self.case_title.setText("Select a test point in the Test Explorer")
            self.meta.setText("")
            self.refresh()
            return

        point = session.point
        self.case_id.setText(f"TC {point.test_case_id}  ·  Point {point.id}")
        self.case_title.setText(point.title)
        self.meta.setText(" · ".join(x for x in (point.configuration, point.tester and f"Tester: {point.tester}") if x))
        for index, step in enumerate(session.steps):
            card = StepCard(index, step.number, step.action, step.expected, step.shared_title)
            card.outcome_clicked.connect(self.step_outcome)
            card.comment_edited.connect(self.step_comment)
            card.screenshot_clicked.connect(self.screenshot_requested)
            card.activated.connect(self.set_current)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.cards.append(card)
        if not session.steps:
            self.meta.setText(self.meta.text() + "  ·  (test case has no steps)")
        self.refresh()
        if self.cards:
            self.set_current(0)

    def refresh(self) -> None:
        session = self.session
        total = len(session.steps) if session else 0
        done = session.done_count if session else 0
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)
        self.progress_label.setText(f"{done}/{total} steps" if session else "")
        if not session:
            return
        counts: dict[str, int] = {}
        for item in session.evidence:
            if item.step_action_path:
                counts[item.step_action_path] = counts.get(item.step_action_path, 0) + 1
        for card, step in zip(self.cards, session.steps):
            card.set_outcome(step.outcome, step.comment)
            card.set_evidence_count(counts.get(step.action_path, 0))

    def set_current(self, index: int) -> None:
        if not self.cards:
            return
        index = max(0, min(index, len(self.cards) - 1))
        if 0 <= self.current < len(self.cards):
            self.cards[self.current].set_current(False)
        self.current = index
        self.cards[index].set_current(True)
        self.scroll.ensureWidgetVisible(self.cards[index], 0, 40)

    def advance(self) -> None:
        if self.session and self.current < len(self.cards) - 1:
            self.set_current(self.current + 1)


# ----------------------------------------------------------------------------
class EvidencePanel(QFrame):
    record_toggled = pyqtSignal()
    screenshot_requested = pyqtSignal()
    open_requested = pyqtSignal(object)  # Path
    open_folder_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("EVIDENCE")
        title.setObjectName("panelTitle")
        self.folder_btn = QToolButton()
        self.folder_btn.setText("📁")
        self.folder_btn.setToolTip("Open the evidence folder")
        self.folder_btn.clicked.connect(self.open_folder_requested)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.folder_btn)
        layout.addLayout(head)

        row = QHBoxLayout()
        self.record_btn = QPushButton("● Record")
        self.record_btn.setObjectName("record")
        self.record_btn.setToolTip("Start / stop recording the application area (F9)")
        self.record_btn.clicked.connect(self.record_toggled)
        self.shot_btn = QPushButton("📷 Screenshot")
        self.shot_btn.setToolTip("Screenshot, linked to the current step (F7)")
        self.shot_btn.clicked.connect(self.screenshot_requested)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["GIF", "MP4"])
        self.format_combo.setToolTip("Recording format")
        row.addWidget(self.record_btn, stretch=1)
        row.addWidget(self.shot_btn, stretch=1)
        row.addWidget(self.format_combo)
        layout.addLayout(row)

        self.list = QListWidget()
        self.list.setMinimumHeight(72)
        self.list.setToolTip("Checked items are attached when you publish. Double-click to open.")
        self.list.itemDoubleClicked.connect(lambda item: self.open_requested.emit(item.data(Qt.ItemDataRole.UserRole).path))
        self.list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.list, stretch=1)

    def set_recording(self, recording: bool, text: str = "") -> None:
        self.record_btn.setText(f"■ Stop  {text}" if recording else "● Record")
        self.record_btn.setProperty("recording", recording)
        repolish(self.record_btn)
        self.format_combo.setEnabled(not recording)

    def set_items(self, items: list[EvidenceItem]) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for item in items:
            icon = "📷" if item.kind == "screenshot" else "🎞"
            entry = QListWidgetItem(f"{icon}  {item.label}")
            entry.setData(Qt.ItemDataRole.UserRole, item)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked if item.attach else Qt.CheckState.Unchecked)
            entry.setToolTip(str(item.path))
            self.list.addItem(entry)
        self.list.blockSignals(False)

    def _on_item_changed(self, entry: QListWidgetItem) -> None:
        entry.data(Qt.ItemDataRole.UserRole).attach = entry.checkState() == Qt.CheckState.Checked


# ----------------------------------------------------------------------------
class PublishPanel(QFrame):
    publish_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        title = QLabel("RESULT")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        row = QHBoxLayout()
        self.outcome_combo = QComboBox()
        self.outcome_combo.addItems(OUTCOME_CHOICES)
        self.suggestion = QLabel("")
        self.suggestion.setTextFormat(Qt.TextFormat.RichText)
        row.addWidget(self.outcome_combo, stretch=1)
        row.addWidget(self.suggestion)
        layout.addLayout(row)

        self.comment_edit = QPlainTextEdit()
        self.comment_edit.setPlaceholderText("Result comment (optional)…")
        self.comment_edit.setFixedHeight(54)
        layout.addWidget(self.comment_edit)

        self.bug_chk = QCheckBox("Create bug in ADO when the result is Failed")
        layout.addWidget(self.bug_chk)

        self.publish_btn = QPushButton("Publish to Azure DevOps   Ctrl+↵")
        self.publish_btn.setObjectName("publish")
        self.publish_btn.clicked.connect(self.publish_requested)
        layout.addWidget(self.publish_btn)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.status)

    def chosen_outcome(self, suggested: Optional[str]) -> Optional[str]:
        text = self.outcome_combo.currentText()
        return suggested if text == OUTCOME_CHOICES[0] else text

    def show_suggestion(self, suggested: Optional[str]) -> None:
        if suggested:
            color = OUTCOME_COLORS.get(suggested, "#999")
            self.suggestion.setText(f"<span style='color:{color}'>● {suggested}</span>")
        else:
            self.suggestion.setText("<span style='color:#9199a5'>● in progress</span>")

    def reset(self) -> None:
        self.outcome_combo.setCurrentIndex(0)
        self.comment_edit.clear()
        self.status.clear()
