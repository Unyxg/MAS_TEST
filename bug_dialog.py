"""
bug_dialog.py - "Report a bug" form for MAS-QA-Bridge.

Pre-filled from the test being executed (steps up to the failing one, expected
vs. actual, environment, evidence) so the tester only reviews and adds context.
Validates before submitting, shows a live preview of exactly what developers
will see in Azure DevOps, and keeps a local draft if submission fails.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QPixmap, QShowEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ado_test_api import hidden_error_count, rule_error_fields
from bug_report import IMAGE_SUFFIXES, PRIORITIES, REPRODUCIBILITY, SEVERITIES, BugReport


def _text_edit(placeholder: str, height: int) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setPlaceholderText(placeholder)
    edit.setMinimumHeight(height)
    return edit


class BugDialog(QDialog):
    submit_requested = pyqtSignal(object)  # BugReport
    screenshot_requested = pyqtSignal()

    def __init__(
        self,
        report: BugReport,
        meta: Optional[dict[str, Any]],
        drafts_dir: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Report a bug")
        self.setObjectName("bugDialog")
        # Fit the screen the tester actually has (laptops at 125-150% scaling are short), stay resizable.
        self.setSizeGripEnabled(True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setMinimumSize(560, 420)
        self._fit_to_screen()
        self.report = report
        self.meta = meta or {}
        self.drafts_dir = drafts_dir
        self.created_bug: Optional[dict[str, Any]] = None

        # Layout: [ scrollable form ............ ]  <- grows / scrolls on short screens
        #         [ validation message + buttons ]  <- always visible
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        form_page = QWidget()
        root = QVBoxLayout(form_page)
        root.setContentsMargins(18, 16, 18, 8)
        root.setSpacing(10)
        self._scroll.setWidget(form_page)
        outer.addWidget(self._scroll, stretch=1)
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(18, 6, 18, 14)
        footer_layout.setSpacing(8)
        outer.addWidget(footer)

        # ---- heading
        heading = QLabel("🐞  Report a bug")
        heading.setObjectName("caseTitle")
        context = []
        if report.test_case_id:
            context.append(f"Linked to test case <b>#{report.test_case_id}</b> {report.test_case_title}")
        if report.run_id:
            context.append(f"test run <b>#{report.run_id}</b>")
        if not context:
            context.append("Exploratory bug (not linked to a test case)")
        sub = QLabel(" · ".join(context))
        sub.setObjectName("muted")
        root.addWidget(heading)
        root.addWidget(sub)

        # ---- classification
        self.title_edit = QLineEdit(report.title)
        self.title_edit.setPlaceholderText("<Screen / feature> – <symptom>   e.g. 'New order – confirmation has no order number'")
        self.title_edit.setObjectName("bugTitle")
        root.addWidget(self.title_edit)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.severity = QComboBox()
        self.severity.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.severity.setMinimumContentsLength(8)
        self.severity.addItems(self.meta.get("severities") or SEVERITIES)
        self.severity.setCurrentText(report.severity)
        self.severity.setToolTip("Impact on the user: 1 crash/data loss · 2 major feature broken · 3 workaround exists · 4 cosmetic")
        self.priority = QComboBox()
        self.priority.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.priority.setMinimumContentsLength(8)
        self.priority.addItems([str(p) for p in PRIORITIES])
        self.priority.setCurrentText(str(report.priority))
        self.priority.setToolTip("How soon it should be fixed (1 = immediately)")
        self.repro = QComboBox()
        self.repro.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.repro.setMinimumContentsLength(8)
        self.repro.addItems(REPRODUCIBILITY)
        self.repro.setCurrentText(report.reproducibility)
        self.found_in = QLineEdit(report.found_in)
        self.found_in.setPlaceholderText("Application version / build number")
        self.area = self._path_combo(self.meta.get("areas"), report.area_path)
        self.iteration = self._path_combo(self.meta.get("iterations"), report.iteration_path)
        self.assigned = QLineEdit(report.assigned_to)
        self.assigned.setPlaceholderText("developer@company.com (optional)")
        self.tags = QLineEdit(", ".join(report.tags))
        self.tags.setPlaceholderText("comma separated")

        cells = [
            ("Severity", self.severity), ("Priority", self.priority),
            ("Reproducibility", self.repro), ("Found in build", self.found_in),
            ("Area path", self.area), ("Iteration", self.iteration),
            ("Assigned to", self.assigned), ("Tags", self.tags),
        ]
        for i, (label, widget) in enumerate(cells):
            caption = QLabel(label)
            caption.setObjectName("muted")
            grid.addWidget(caption, (i // 4) * 2, i % 4)
            grid.addWidget(widget, (i // 4) * 2 + 1, i % 4)
        root.addLayout(grid)

        # ---- fields the project's rules require (custom fields: e.g. "Detected in Phase")
        self._required: list[dict[str, Any]] = []
        self._config_extras = dict(report.extra_fields)
        self._extra_widgets: dict[str, QWidget] = {}
        self._extra_title = QLabel("REQUIRED BY YOUR PROJECT")
        self._extra_title.setObjectName("panelTitle")
        self._extra_title.hide()
        root.addWidget(self._extra_title)
        self._extra_grid = QGridLayout()
        self._extra_grid.setHorizontalSpacing(12)
        self._extra_grid.setVerticalSpacing(6)
        root.addLayout(self._extra_grid)
        for spec in self.meta.get("required_custom") or []:
            self.add_required_field(spec)

        # ---- tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_details_tab(), "Description")
        self.tabs.addTab(self._build_environment_tab(), "Environment")
        self.tabs.addTab(self._build_evidence_tab(), "Evidence")
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(True)
        self.tabs.addTab(self.preview, "Preview")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # Never squeeze the tab pages (rows would overlap): the whole form scrolls instead.
        self.tabs.setMinimumHeight(self.tabs.sizeHint().height())
        root.addWidget(self.tabs, stretch=1)

        # ---- footer
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.RichText)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.message.setOpenExternalLinks(True)
        footer_layout.addWidget(self.message)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Save draft")
        self.save_btn.clicked.connect(self.save_draft)
        self.load_btn = QPushButton("Load draft…")
        self.load_btn.clicked.connect(self.load_draft)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        self.submit_btn = QPushButton("Submit bug to Azure DevOps")
        self.submit_btn.setObjectName("primary")
        self.submit_btn.clicked.connect(self.submit)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.load_btn)
        buttons.addStretch()
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.submit_btn)
        footer_layout.addLayout(buttons)

        self._fill_details(report)
        self._update_validation()
        for edit in (self.title_edit, self.found_in):
            edit.textChanged.connect(self._update_validation)
        for edit in (self.steps_edit, self.actual_edit, self.expected_edit):
            edit.textChanged.connect(self._update_validation)
        self._update_validation()

    def add_required_field(self, spec: dict[str, Any], highlight: bool = False) -> bool:
        """Add an input for a field the project requires. Returns False if it is already shown."""
        ref = spec["ref"]
        if ref in self._extra_widgets:
            if highlight:
                self._mark(self._extra_widgets[ref])
            return False
        i = len(self._required)
        self._required.append(spec)
        caption = QLabel(spec["name"] + " *")
        caption.setObjectName("muted")
        current = self._config_extras.get(ref, "") or spec.get("default", "")
        if spec["allowed"]:
            widget: QWidget = QComboBox()
            widget.addItem("")  # no silent guess: an empty choice is flagged before submitting
            widget.addItems(spec["allowed"])
            widget.setCurrentText(current)
            widget.currentTextChanged.connect(self._update_validation)
        else:
            widget = QLineEdit(current)
            widget.textChanged.connect(self._update_validation)
        widget.setToolTip((spec.get("help") or "") + (f"\n[{ref}]" if ref else ""))
        self._extra_widgets[ref] = widget
        self._extra_grid.addWidget(caption, (i // 4) * 2, i % 4)
        self._extra_grid.addWidget(widget, (i // 4) * 2 + 1, i % 4)
        self._extra_title.show()
        if highlight:
            self._mark(widget)
        return True

    @staticmethod
    def _mark(widget: QWidget) -> None:
        """Flag a field Azure DevOps rejected (red border until the next edit)."""
        widget.setStyleSheet("border: 1px solid #ff7b72;")
        if isinstance(widget, QComboBox):
            widget.currentTextChanged.connect(lambda _t: widget.setStyleSheet(""))
        else:
            widget.textChanged.connect(lambda _t: widget.setStyleSheet(""))
        widget.setFocus()

    def _fit_to_screen(self) -> None:
        screen = (self.parent().screen() if self.parent() is not None else None) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        self.resize(min(980, int(avail.width() * 0.92)), min(820, int(avail.height() * 0.90)))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        # Keep the whole window (title bar + buttons) on screen.
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        frame = self.frameGeometry()
        x = min(max(frame.x(), avail.left()), max(avail.left(), avail.right() - frame.width() + 1))
        y = min(max(frame.y(), avail.top()), max(avail.top(), avail.bottom() - frame.height() + 1))
        if (x, y) != (frame.x(), frame.y()):
            self.move(x, y)

    # ------------------------------------------------------------ building
    @staticmethod
    def _path_combo(paths: Optional[list[str]], current: str) -> QComboBox:
        combo = QComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(14)  # long area/iteration paths must not widen the form
        combo.setEditable(True)
        combo.addItems(paths or [])
        combo.setCurrentText(current or (paths[0] if paths else ""))
        if combo.lineEdit() is not None:
            combo.lineEdit().setCursorPosition(0)  # show the start of long paths
        combo.setToolTip("Leave the project root to let the team triage it")
        return combo

    def _build_details_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(10, 12, 10, 10)
        form.setVerticalSpacing(8)
        self.summary_edit = _text_edit("One or two sentences: what is broken and what it affects.", 44)
        self.summary_edit.setMaximumHeight(90)
        self.pre_edit = _text_edit("Data, account, settings or state needed before step 1 (e.g. 'Customer Fabrikam exists').", 44)
        self.pre_edit.setMaximumHeight(90)
        self.steps_edit = _text_edit("One step per line. Keep them short and concrete.", 110)
        self.fail_spin = QSpinBox()
        self.fail_spin.setRange(0, 999)
        self.fail_spin.setSpecialValueText("—")
        self.fail_spin.setToolTip("Which step shows the problem (highlighted for developers)")
        self.expected_edit = _text_edit("What should happen", 60)
        self.actual_edit = _text_edit("What actually happened (messages, wrong values, crash…)", 60)
        results = QHBoxLayout()
        exp_box, act_box = QVBoxLayout(), QVBoxLayout()
        exp_box.addWidget(QLabel("Expected result"))
        exp_box.addWidget(self.expected_edit)
        act_box.addWidget(QLabel("Actual result  <span style='color:#ff7b72'>*</span>"))
        act_box.addWidget(self.actual_edit)
        results.addLayout(exp_box)
        results.addLayout(act_box)

        form.addRow("Summary", self.summary_edit)
        form.addRow("Preconditions", self.pre_edit)
        form.addRow("Steps to\nreproduce *", self.steps_edit)
        form.addRow("Fails at step", self.fail_spin)
        form.addRow(results)
        return page

    def _build_environment_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 10)
        hint = QLabel("Collected automatically. Edit or add rows (e.g. database, server URL, user role).")
        hint.setObjectName("muted")
        self.env_table = QTableWidget(0, 2)
        self.env_table.setHorizontalHeaderLabels(["Property", "Value"])
        self.env_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.env_table.setColumnWidth(0, 200)
        self.env_table.verticalHeader().setVisible(False)
        add = QPushButton("+ Add row")
        add.clicked.connect(lambda: self.env_table.insertRow(self.env_table.rowCount()))
        layout.addWidget(hint)
        layout.addWidget(self.env_table, stretch=1)
        layout.addWidget(add, alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def _build_evidence_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(10, 12, 10, 10)
        left = QVBoxLayout()
        self.evidence_list = QListWidget()
        self.evidence_list.currentItemChanged.connect(self._show_thumbnail)
        row = QHBoxLayout()
        add = QPushButton("Add file…")
        add.clicked.connect(self._add_files)
        shot = QPushButton("📷 Screenshot now")
        shot.setToolTip("Hides this form for a moment and captures the application")
        shot.clicked.connect(self.screenshot_requested)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self.evidence_list.takeItem(self.evidence_list.currentRow()))
        row.addWidget(add)
        row.addWidget(shot)
        row.addWidget(remove)
        left.addWidget(self.evidence_list, stretch=1)
        left.addLayout(row)
        self.thumbnail = QLabel("Select an item to preview")
        self.thumbnail.setObjectName("muted")
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setMinimumWidth(220)
        layout.addLayout(left, stretch=1)
        layout.addWidget(self.thumbnail, stretch=1)
        return page

    def _fill_details(self, report: BugReport) -> None:
        self.title_edit.setText(report.title)
        self.title_edit.setCursorPosition(0)
        self.summary_edit.setPlainText(report.summary)
        self.pre_edit.setPlainText(report.preconditions)
        self.steps_edit.setPlainText("\n".join(s.replace("\n", " ") for s in report.steps))
        self.fail_spin.setValue(report.failing_step or 0)
        self.expected_edit.setPlainText(report.expected)
        self.actual_edit.setPlainText(report.actual)
        self.env_table.setRowCount(0)
        for key, value in report.environment.items():
            row = self.env_table.rowCount()
            self.env_table.insertRow(row)
            self.env_table.setItem(row, 0, QTableWidgetItem(key))
            self.env_table.setItem(row, 1, QTableWidgetItem(value))
        self.evidence_list.clear()
        for path in report.attachments:
            self.add_attachment(Path(path))

    # ------------------------------------------------------------ evidence
    def add_attachment(self, path: Path) -> None:
        icon = "📷" if path.suffix.lower() in IMAGE_SUFFIXES else "🎞"
        item = QListWidgetItem(f"{icon}  {path.name}")
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(str(path))
        self.evidence_list.addItem(item)
        self.evidence_list.setCurrentItem(item)
        self.tabs.setTabText(2, f"Evidence ({self.evidence_list.count()})")
        self._update_validation()

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "Attach files", "", "Evidence (*.png *.jpg *.jpeg *.gif *.mp4 *.log *.txt *.zip);;All files (*)"
        )
        for f in files:
            self.add_attachment(Path(f))

    def _show_thumbnail(self, item: Optional[QListWidgetItem], _prev: object = None) -> None:
        if item is None:
            self.thumbnail.setText("Select an item to preview")
            return
        path: Path = item.data(Qt.ItemDataRole.UserRole)
        pixmap = QPixmap(str(path)) if path.suffix.lower() in IMAGE_SUFFIXES else QPixmap()
        if pixmap.isNull():
            self.thumbnail.setText(f"{path.name}\n{path.stat().st_size / 1e6:.1f} MB" if path.exists() else path.name)
        else:
            self.thumbnail.setPixmap(
                pixmap.scaled(380, 300, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )

    # ------------------------------------------------------------ report
    def collect(self) -> BugReport:
        r = self.report
        r.title = self.title_edit.text().strip()
        r.severity = self.severity.currentText()
        r.priority = int(self.priority.currentText())
        r.reproducibility = self.repro.currentText()
        r.found_in = self.found_in.text().strip()
        r.area_path = self.area.currentText().strip()
        r.iteration_path = self.iteration.currentText().strip()
        r.assigned_to = self.assigned.text().strip()
        r.tags = [t.strip() for t in self.tags.text().split(",") if t.strip()]
        r.summary = self.summary_edit.toPlainText().strip()
        r.preconditions = self.pre_edit.toPlainText().strip()
        r.steps = [line.strip() for line in self.steps_edit.toPlainText().splitlines() if line.strip()]
        r.failing_step = self.fail_spin.value() or None
        r.expected = self.expected_edit.toPlainText().strip()
        r.actual = self.actual_edit.toPlainText().strip()
        env = {}
        for row in range(self.env_table.rowCount()):
            key, value = self.env_table.item(row, 0), self.env_table.item(row, 1)
            if key and key.text().strip() and value and value.text().strip():
                env[key.text().strip()] = value.text().strip()
        r.environment = env
        r.attachments = [
            self.evidence_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.evidence_list.count())
        ]
        extras = dict(self._config_extras)
        for ref, widget in self._extra_widgets.items():
            extras[ref] = (widget.currentText() if isinstance(widget, QComboBox) else widget.text()).strip()
        r.extra_fields = {k: v for k, v in extras.items() if v}
        r.required_extra = {spec["ref"]: spec["name"] for spec in self._required}
        return r

    def _update_validation(self) -> None:
        errors, warnings = self.collect().validate()
        parts = [f"<span style='color:#ff7b72'>✖ {e}</span>" for e in errors]
        if warnings:  # one compact line: full list on hover and again when submitting
            parts.append(
                f"<span style='color:#e3b341'>⚠ {len(warnings)} thing(s) to review - hover here, "
                "or see the list when you submit</span>"
            )
        self.message.setToolTip("\n".join("• " + w for w in warnings))
        self.message.setText("<br>".join(parts) if parts else "<span style='color:#56d364'>✔ Ready to submit</span>")
        self.submit_btn.setEnabled(not errors)

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.preview:
            report = self.collect()
            media = {p.name: QUrl.fromLocalFile(str(p)).toString() for p in report.attachments if p.exists()}
            self.preview.setHtml(
                f"<h2 style='margin:0'>{report.title}</h2>{report.repro_html(media)}"
                "<p style='color:#999'><i>Preview - images are uploaded and embedded when you submit.</i></p>"
            )

    # ------------------------------------------------------------ actions
    def submit(self) -> None:
        report = self.collect()
        errors, warnings = report.validate()
        if errors:
            self._update_validation()
            return
        if warnings:
            answer = QMessageBox.question(
                self, "Submit bug", "Submit anyway?\n\n" + "\n".join(f"• {w}" for w in warnings)
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.set_busy(True)
        self.submit_requested.emit(report)

    def set_busy(self, busy: bool) -> None:
        for widget in (self.submit_btn, self.cancel_btn, self.save_btn, self.load_btn):
            widget.setEnabled(not busy)
        if busy:
            self.message.setText("Uploading evidence and creating the bug…")

    def submission_failed(self, error: str) -> None:
        """Keep the form open, explain the error, add any field Azure DevOps rejected, keep a draft."""
        draft = self.collect().save_draft(self.drafts_dir, error)
        self.set_busy(False)
        named = rule_error_fields(error)
        hidden = hidden_error_count(error)
        hint = ""
        if named:
            added, unknown = [], []
            for name in named:
                spec = (self.meta.get("fields_by_name") or {}).get(name.lower())
                if spec is None:
                    unknown.append(name)
                elif self.add_required_field(spec, highlight=True):
                    added.append(spec["name"])
                else:
                    self.add_required_field(spec, highlight=True)  # already shown: just flag it
            if hidden:
                # Azure DevOps reports only the first problem: show every required field so none stays hidden.
                for spec in (self.meta.get("all_required") or []):
                    if self.add_required_field(spec):
                        added.append(spec["name"])
            parts = [f"Azure DevOps rejected <b>{', '.join(named)}</b>."]
            if added:
                parts.append(f"Added to the form: <b>{', '.join(dict.fromkeys(added))}</b> - fill in the highlighted field(s) and submit again.")
            if unknown:
                parts.append(f"'{', '.join(unknown)}' is not a field this app knows - add it under bug.extra_fields in config.yaml (run Check ADO to see the field names).")
            if hidden and not added:
                parts.append(f"{hidden} more problem(s) were not named by Azure DevOps; check Area path, Iteration and the other fields.")
            hint = "<br><span style='color:#ff7b72'>" + " ".join(parts) + "</span>"
            self.tabs.setCurrentIndex(0)
            self._scroll.verticalScrollBar().setValue(0)
        elif "Rule Error" in error or "TF401320" in error:
            hint = "<br><span style='color:#ff7b72'>A field value was rejected by your project's rules - check Area path, Iteration, Severity and the required fields.</span>"
        elif "Authentication" in error or "rejected the sign-in" in error:
            hint = "<br><span style='color:#ff7b72'>Check the token has <b>Work Items: Read &amp; write</b>.</span>"
        technical = error.split("\n")[0]
        if len(technical) > 230:
            technical = technical[:230] + "…"
        self.message.setToolTip(error)
        self.message.setText(
            "<span style='color:#ff7b72'><b>✖ Bug not created.</b></span>"
            + (hint or "<br><span style='color:#ff7b72'>Azure DevOps refused it - details below.</span>")
            + f"<br><span style='color:#9199a5'>Draft saved: {draft}</span>"
            + f"<br><span style='color:#9199a5'>Technical detail: {technical}</span>"
        )
        self.message.adjustSize()

    def submission_succeeded(self, result: dict[str, Any]) -> None:
        self.created_bug = result
        self.accept()

    def save_draft(self) -> None:
        path = self.collect().save_draft(self.drafts_dir)
        self.message.setText(f"<span style='color:#56d364'>Draft saved: {path}</span>")

    def load_draft(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load bug draft", str(self.drafts_dir), "Bug drafts (*.json)")
        if path:
            loaded = BugReport.load_draft(Path(path))
            self.report = loaded
            self._fill_details(loaded)
            self.severity.setCurrentText(loaded.severity)
            self.priority.setCurrentText(str(loaded.priority))
            self.repro.setCurrentText(loaded.reproducibility)
            self.found_in.setText(loaded.found_in)
            self.area.setCurrentText(loaded.area_path)
            self.iteration.setCurrentText(loaded.iteration_path)
            self.assigned.setText(loaded.assigned_to)
            self.tags.setText(", ".join(loaded.tags))
            self._config_extras = dict(loaded.extra_fields)
            for ref, widget in self._extra_widgets.items():
                value = loaded.extra_fields.get(ref, "")
                widget.setCurrentText(value) if isinstance(widget, QComboBox) else widget.setText(value)
            self._update_validation()
