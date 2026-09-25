"""
main_window.py - MAS-QA-Bridge dashboard (PyQt6).

    ┌ header: brand · Browse · exe path · mode · Launch/Release/Stop · REC · ADO ┐
    ├──────────────┬──────────────────────────────────────┬─────────────────────┤
    │ Test         │  Application under test              │ Test runner (steps) │
    │ Explorer     │  (external .exe embedded here)       │ Evidence            │
    │ plan/suite/  │                                      │ Result + Publish    │
    │ points       │                                      │                     │
    └──────────────┴──────────────────────────────────────┴─────────────────────┘

Run:  python main_window.py            (uses config.yaml + ADO_PAT)
      python main_window.py --demo     (offline demo with sample ADO data)

Shortcuts: F5 pass step · F6 fail step · F7 screenshot · F9 record · Ctrl+B report bug · Ctrl+Enter publish
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from PyQt6.QtCore import QObject, QPoint, QRunnable, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QMoveEvent, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ado_test_api import AdoConfig, AdoTestClient
from bug_dialog import BugDialog
from bug_report import BugReport
from evidence_capture import EvidenceRecorder, Region
from qa_session import FAILED, PASSED, UNSPECIFIED, EvidenceItem, TestPoint, TestSession
from system_info import collect_environment, get_file_version
from theme import STYLESHEET
from widgets import EvidencePanel, HostFrame, PublishPanel, RunAsDialog, StatusPill, StepRunner, TestExplorer
import run_as
from window_manager import IS_WINDOWS, EmbedError, WindowManager

if IS_WINDOWS:
    import win32gui

APP_NAME = "MAS-QA-Bridge"
# Frozen (PyInstaller) builds keep config.yaml and temp_evidence next to the .exe.
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
MODES = [("Embed (SetParent)", "reparent"), ("Dock (overlay)", "dock")]
RUN_AS_CHOICES = [
    ("Run as: me", run_as.RUN_AS_CURRENT),
    ("Run as: administrator", run_as.RUN_AS_ADMIN),
    ("Run as: different user…", run_as.RUN_AS_USER),
]

log = logging.getLogger(APP_NAME)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        log.warning("Config file %s not found - using defaults", path)
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ----------------------------------------------------------------------------
# Background work helpers
# ----------------------------------------------------------------------------
class _TaskSignals(QObject):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    finished = pyqtSignal()


class Task(QRunnable):
    """Run ``fn(*args, **kwargs)`` on the global thread pool; report via Qt signals."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.signals = _TaskSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # reported to the UI
            log.debug(traceback.format_exc())
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.signals.succeeded.emit(result)
        finally:
            self.signals.finished.emit()


class _LogBridge(QObject):
    message = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    """Forwards log records (from any thread) to widgets via a queued signal."""

    def __init__(self) -> None:
        super().__init__()
        self.bridge = _LogBridge()
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.message.emit(self.format(record))
        except RuntimeError:
            pass  # widget already destroyed during shutdown


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, config: dict[str, Any], ado_client: Optional[AdoTestClient] = None, demo: bool = False) -> None:
        super().__init__()
        self.config = config
        self.demo = demo
        self.app_cfg: dict[str, Any] = config.get("app") or {}
        self.ev_cfg: dict[str, Any] = config.get("evidence") or {}
        self.ado_cfg: dict[str, Any] = config.get("ado") or {}
        self.bug_cfg: dict[str, Any] = config.get("bug") or {}
        self.bug_meta: Optional[dict[str, Any]] = None  # severities, areas, iterations (loaded from ADO)
        self._app_version: Optional[str] = None

        self.exe_path: Optional[str] = self.app_cfg.get("default_executable") or None
        self.pool = QThreadPool.globalInstance()
        self._tasks: set[Task] = set()

        self.window_manager = WindowManager(find_timeout=float(self.app_cfg.get("window_find_timeout_seconds", 20)))

        output_dir = Path(self.ev_cfg.get("output_dir") or "temp_evidence")
        if not output_dir.is_absolute():
            output_dir = BASE_DIR / output_dir
        self._capture_region: Optional[Region] = None
        self.recorder = EvidenceRecorder(
            region_provider=lambda: self._capture_region,
            output_dir=output_dir,
            fps=int(self.ev_cfg.get("fps", 8)),
            max_width=int(self.ev_cfg.get("max_width", 1280)),
            max_seconds=int(self.ev_cfg.get("max_seconds", 180)),
            jpeg_quality=int(self.ev_cfg.get("jpeg_quality", 90)),
            memory_budget_mb=int(self.ev_cfg.get("memory_budget_mb", 512)),
        )

        self.session: Optional[TestSession] = None
        self.session_published = False
        self.loose_evidence: list[EvidenceItem] = []  # captured before a test was selected
        self._recording_step: Optional[int] = None

        self._build_ui()
        self._install_log_handler()
        self._install_shortcuts()
        self.ado_client = ado_client or self._build_ado_client()

        self._region_timer = QTimer(self)
        self._region_timer.setInterval(200)
        self._region_timer.timeout.connect(self._refresh_capture_region)
        self._region_timer.start()

        self._watchdog = QTimer(self)
        self._watchdog.setInterval(500)
        self._watchdog.timeout.connect(self._on_watchdog)
        self._watchdog.start()

        self._set_exe_path(self.exe_path)
        self._update_controls()
        if self.ado_client is not None:
            QTimer.singleShot(0, self.load_plans)

    # ================================================================== UI
    def _build_ui(self) -> None:
        self.is_admin = run_as.is_admin()
        self._force_close = False
        self.setWindowTitle(
            APP_NAME + (" — DEMO" if self.demo else "") + (" (Administrator)" if self.is_admin else "")
        )
        self.resize(1680, 980)
        self.setStyleSheet(STYLESHEET)

        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(8)
        splitter.setChildrenCollapsible(False)
        splitter.splitterMoved.connect(lambda *_: self._on_host_resized())

        # left: test explorer
        self.explorer = TestExplorer()
        self.explorer.setMinimumWidth(250)
        self.explorer.plan_selected.connect(self.load_suites)
        self.explorer.suite_selected.connect(self.load_points)
        self.explorer.point_selected.connect(self.open_point)
        self.explorer.refresh_requested.connect(self.load_plans)
        splitter.addWidget(self.explorer)

        # centre: application under test
        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.setSpacing(6)
        strip = QHBoxLayout()
        caption = QLabel("APPLICATION UNDER TEST")
        caption.setObjectName("panelTitle")
        self.app_name = QLabel("—")
        self.app_state = StatusPill("Not running", "idle")
        strip.addWidget(caption)
        strip.addWidget(self.app_name)
        strip.addStretch()
        strip.addWidget(self.app_state)
        centre_layout.addLayout(strip)
        self.host_frame = HostFrame()
        self.host_frame.resized.connect(self._on_host_resized)
        centre_layout.addWidget(self.host_frame, stretch=1)
        splitter.addWidget(centre)

        # right: runner, evidence, result
        right = QWidget()
        right.setMinimumWidth(380)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        self.runner = StepRunner()
        self.runner.step_outcome.connect(self.set_step_outcome)
        self.runner.step_comment.connect(self._on_step_comment)
        self.runner.screenshot_requested.connect(self.take_screenshot)
        self.evidence_panel = EvidencePanel()
        self.evidence_panel.record_toggled.connect(self.toggle_recording)
        self.evidence_panel.screenshot_requested.connect(lambda: self.take_screenshot(self.runner.current))
        self.evidence_panel.open_requested.connect(lambda p: QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))))
        self.evidence_panel.open_folder_requested.connect(self.open_evidence_folder)
        default_format = str(self.ev_cfg.get("format", "GIF")).upper()
        self.evidence_panel.format_combo.setCurrentText(default_format if default_format in ("GIF", "MP4") else "GIF")
        self.evidence_panel.setMaximumHeight(180)
        self.publish_panel = PublishPanel()
        self.publish_panel.bug_requested.connect(lambda: self.open_bug_dialog())
        self.runner.bug_requested.connect(self.open_bug_dialog)
        self.publish_panel.publish_requested.connect(self.publish)
        right_layout.addWidget(self.runner, stretch=1)
        right_layout.addWidget(self.evidence_panel)
        right_layout.addWidget(self.publish_panel)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([290, 960, 420])

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 10)
        body_layout.addWidget(splitter)
        outer.addWidget(body, stretch=1)
        self.setCentralWidget(root)

        # log dock (toggle from the header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setWidget(self.log_view)
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()
        self.log_dock.visibilityChanged.connect(lambda _v: QTimer.singleShot(0, self._on_host_resized))

        self.statusBar().showMessage("Ready")

    def _build_header(self) -> QFrame:
        header = QFrame()
        header.setObjectName("header")
        row = QHBoxLayout(header)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(8)

        brand = QLabel("MAS-QA")
        brand.setObjectName("brand")
        accent = QLabel("Bridge")
        accent.setObjectName("brandAccent")
        row.addWidget(brand)
        row.addWidget(accent)
        row.addSpacing(16)

        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setToolTip("Select the executable under test")
        self.browse_btn.clicked.connect(self.browse_executable)
        self.exe_label = QLabel()
        self.exe_label.setObjectName("exePath")
        self.exe_label.setMinimumWidth(260)
        self.mode_combo = QComboBox()
        for label, mode in MODES:
            self.mode_combo.addItem(label, mode)
        configured = str(self.app_cfg.get("embed_mode", "reparent"))
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(configured)))
        self.mode_combo.setToolTip(
            "Embed: true child window (SetParent).\nDock: borderless window glued over the frame - "
            "use for apps that misbehave when re-parented (Electron, some WPF)."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.run_as_combo = QComboBox()
        for label, mode in RUN_AS_CHOICES:
            self.run_as_combo.addItem(label, mode)
        configured_run_as = str(self.app_cfg.get("run_as", run_as.RUN_AS_CURRENT))
        self.run_as_combo.setCurrentIndex(max(0, self.run_as_combo.findData(configured_run_as)))
        self.run_as_combo.setToolTip(
            f"MAS-QA-Bridge is running as {run_as.current_account()}"
            + (" (elevated)" if self.is_admin else "")
            + ".\n'Run as: me' launches the application with this same account and elevation."
        )
        self.launch_btn = QPushButton("▶ Launch")
        self.launch_btn.setObjectName("primary")
        self.launch_btn.clicked.connect(self.launch_and_embed)
        self.release_btn = QPushButton("Pop out")
        self.release_btn.setToolTip("Give the window back to the desktop / re-embed it")
        self.release_btn.clicked.connect(self.toggle_release)
        self.kill_btn = QPushButton("■ Stop app")
        self.kill_btn.clicked.connect(self.terminate_app)
        for combo in (self.mode_combo, self.run_as_combo):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(13)
        self.exe_label.setMinimumWidth(160)
        for widget in (self.browse_btn, self.exe_label, self.mode_combo, self.run_as_combo, self.launch_btn, self.release_btn, self.kill_btn):
            row.addWidget(widget, stretch=1 if widget is self.exe_label else 0)

        row.addSpacing(12)
        self.rec_pill = StatusPill("", "idle")
        self.rec_pill.hide()
        self.ado_pill = StatusPill("ADO: not configured", "warn")
        self.ado_pill.setMaximumWidth(300)
        self.bug_btn = QPushButton("🐞 Report bug")
        self.bug_btn.setObjectName("bugLink")
        self.bug_btn.setToolTip("Raise a bug for the development team (Ctrl+B)")
        self.bug_btn.clicked.connect(lambda: self.open_bug_dialog())
        self.check_btn = QPushButton("Check ADO")
        self.check_btn.setToolTip("Read-only checks: PAT, test plans, Bug fields, area/iteration paths")
        self.check_btn.clicked.connect(self.run_diagnostics)
        row.addWidget(self.bug_btn)
        self.log_btn = QPushButton("Log")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(lambda on: self.log_dock.setVisible(on))
        row.addWidget(self.rec_pill)
        row.addWidget(self.ado_pill)
        row.addWidget(self.check_btn)
        row.addWidget(self.log_btn)
        return header

    def _install_log_handler(self) -> None:
        handler = QtLogHandler()
        handler.setLevel(logging.INFO)
        handler.bridge.message.connect(self.log_view.appendPlainText)
        handler.bridge.message.connect(lambda m: self.statusBar().showMessage(m.split("  ", 1)[-1], 8000))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _install_shortcuts(self) -> None:
        bindings = {
            "F5": lambda: self._shortcut_step(PASSED),
            "F6": lambda: self._shortcut_step(FAILED),
            "F7": lambda: self.take_screenshot(self.runner.current),
            "F9": self.toggle_recording,
            "Ctrl+Return": self.publish,
            "Ctrl+Enter": self.publish,
            "Ctrl+L": lambda: self.log_btn.toggle(),
            "Ctrl+B": lambda: self.open_bug_dialog(),
        }
        for keys, slot in bindings.items():
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(slot)

    def _build_ado_client(self) -> Optional[AdoTestClient]:
        try:
            client = AdoTestClient(AdoConfig.from_dict(self.ado_cfg))
        except ValueError as exc:
            log.warning("ADO integration disabled: %s", exc)
            self.explorer.set_message(f"Azure DevOps is not configured.<br><i>{exc}</i><br>Edit config.yaml and set ADO_PAT.")
            return None
        return client

    # ============================================================ helpers
    def _run_task(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_success: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_finished: Optional[Callable[[], None]] = None,
        **kwargs: Any,
    ) -> None:
        task = Task(fn, *args, **kwargs)
        if on_success:
            task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(on_error or self._show_error)
        task.signals.finished.connect(lambda: self._tasks.discard(task))
        if on_finished:
            task.signals.finished.connect(on_finished)
        self._tasks.add(task)  # keep a Python reference while it runs
        self.pool.start(task)

    def _show_error(self, message: str) -> None:
        log.error(message)
        QMessageBox.critical(self, APP_NAME, message)

    def _set_exe_path(self, path: Optional[str]) -> None:
        self.exe_path = path
        self._app_version = get_file_version(path)
        self.exe_label.setText(path or "No executable selected")
        self.exe_label.setToolTip(path or "")
        self.app_name.setText(os.path.basename(path) if path else ("Contoso Orders (demo)" if self.demo else "—"))
        self._update_controls()

    @property
    def embed_mode(self) -> str:
        return self.mode_combo.currentData()

    def _update_controls(self) -> None:
        wm = self.window_manager
        embedded = wm.is_embedded
        has_window = wm.window_exists()
        self.launch_btn.setEnabled(bool(self.exe_path) and IS_WINDOWS)
        self.release_btn.setEnabled(has_window)
        self.release_btn.setText("Pop out" if embedded or not has_window else "Re-embed")
        self.kill_btn.setEnabled(wm.pid is not None)
        self.host_frame.placeholder.setVisible(not embedded and not self.demo)
        has_session = self.session is not None
        self.publish_panel.publish_btn.setEnabled(has_session and self.ado_client is not None)
        self.publish_panel.bug_btn.setEnabled(self.ado_client is not None)
        self.bug_btn.setEnabled(self.ado_client is not None)
        self.check_btn.setEnabled(self.ado_client is not None)
        who = f" · {wm.run_as_label}" if wm.run_as_label else ""
        if embedded:
            self.app_state.set_state(f"{'Embedded' if wm.mode == 'reparent' else 'Docked'} · PID {wm.pid}{who}", "ok")
        elif has_window:
            self.app_state.set_state(f"Separate window · PID {wm.pid}{who}", "info")
        elif wm.pid is not None:
            self.app_state.set_state("Starting…", "warn")
        else:
            self.app_state.set_state("Demo app" if self.demo else "Not running", "info" if self.demo else "idle")

    def _refresh_capture_region(self) -> None:
        """Compute the host frame's rectangle in physical screen pixels."""
        frame = self.host_frame
        if not frame.isVisible():
            self._capture_region = None
            return
        if IS_WINDOWS:
            hwnd = frame.native_handle()
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            x, y = win32gui.ClientToScreen(hwnd, (0, 0))
            region = {"left": x, "top": y, "width": right - left, "height": bottom - top}
        else:
            ratio = frame.devicePixelRatioF()
            origin = frame.mapToGlobal(QPoint(0, 0))
            region = {
                "left": round(origin.x() * ratio),
                "top": round(origin.y() * ratio),
                "width": round(frame.width() * ratio),
                "height": round(frame.height() * ratio),
            }
        self._capture_region = region

    # ================================================== application under test
    def browse_executable(self) -> None:
        start_dir = os.path.dirname(self.exe_path) if self.exe_path else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Select application to test", start_dir, "Executables (*.exe);;All files (*)"
        )
        if path:
            self._set_exe_path(os.path.normpath(path))
            log.info("Selected executable: %s", self.exe_path)

    def launch_and_embed(self) -> None:
        if not IS_WINDOWS:
            QMessageBox.warning(self, APP_NAME, "Launching and embedding apps requires Windows.")
            return
        if not self.exe_path:
            self.browse_executable()
            if not self.exe_path:
                return
        if self.window_manager.pid is not None and self.window_manager.is_running:
            answer = QMessageBox.question(self, APP_NAME, "An application is already running. Stop it and launch again?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.window_manager.terminate()

        mode = self.run_as_combo.currentData()
        credentials = None
        if mode == run_as.RUN_AS_ADMIN and not self.is_admin:
            choice = self._ask_elevation()
            if choice == "restart":
                self.restart_as_admin()
                return
            if choice is None:
                return
        elif mode == run_as.RUN_AS_USER:
            credentials = self._ask_credentials()
            if credentials is None:
                return

        try:
            self.window_manager.launch(
                self.exe_path,
                args=self.app_cfg.get("launch_args") or [],
                run_as_mode=mode,
                credentials=credentials,
            )
        except PermissionError as exc:
            self._show_error(str(exc))
            return
        except Exception as exc:
            self._show_error(f"Could not launch {self.exe_path}: {exc}")
            return

        self.launch_btn.setEnabled(False)
        self._update_controls()
        self._run_task(
            self.window_manager.find_window,
            title_contains=self.app_cfg.get("window_title_contains") or None,
            on_success=self._embed_window,
            on_finished=self._update_controls,
        )

    def _ask_elevation(self) -> Optional[str]:
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Run the application as administrator")
        box.setInformativeText(
            "MAS-QA-Bridge is not running as administrator. Windows does not allow a non-elevated "
            "program to embed an elevated window.\n\n"
            "• Restart MAS-QA-Bridge as administrator (recommended): the app is embedded as usual.\n"
            "• Launch elevated in a separate window: works, but the app stays outside the dashboard "
            "(recording still captures the screen area)."
        )
        restart = box.addButton("Restart as administrator", QMessageBox.ButtonRole.AcceptRole)
        separate = box.addButton("Separate window", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is restart:
            return "restart"
        if box.clickedButton() is separate:
            return "separate"
        return None

    def restart_as_admin(self) -> None:
        if self._session_dirty():
            answer = QMessageBox.question(self, APP_NAME, "Unpublished results will be lost. Restart anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        extra = ["--demo"] if self.demo and "--demo" not in sys.argv else []
        if run_as.restart_self_as_admin(extra):
            self._force_close = True
            self.close()
        else:
            self._show_error("Could not restart as administrator (the UAC prompt was cancelled or failed).")

    def _ask_credentials(self) -> Optional[run_as.Credentials]:
        account = str(self.app_cfg.get("run_as_account") or getattr(self, "_last_account", "") or "")
        dialog = RunAsDialog(account, run_as.load_password(account) or "", self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        self._last_account = dialog.account
        try:
            if dialog.remember.isChecked():
                run_as.save_password(dialog.account, dialog.password)
            else:
                run_as.forget_password(dialog.account)
        except Exception as exc:
            log.warning("Credential Manager not available: %s", exc)
        return run_as.Credentials.parse(dialog.account, dialog.password)

    def _embed_window(self, hwnd: Optional[int] = None) -> None:
        allowed, reason = self.window_manager.can_embed()
        if not allowed:
            # Elevated app with a non-elevated dashboard: leave it as its own window.
            log.info("Not embedding: %s", reason)
            self.statusBar().showMessage("Running elevated in a separate window (restart as administrator to embed)")
            self._update_controls()
            return
        try:
            self.window_manager.embed(
                self.host_frame.native_handle(),
                hwnd or self.window_manager.hwnd,
                mode=self.embed_mode,
                owner_hwnd=int(self.winId()),
            )
        except EmbedError as exc:
            self._show_error(str(exc))
        self._update_controls()

    def toggle_release(self) -> None:
        if self.window_manager.is_embedded:
            self.window_manager.release()
        elif self.window_manager.window_exists():
            self._embed_window()
        self._update_controls()

    def _on_mode_changed(self) -> None:
        if self.window_manager.is_embedded:
            self._embed_window()  # re-embed in the new mode

    def terminate_app(self) -> None:
        self.window_manager.terminate()
        self._update_controls()

    def _on_host_resized(self) -> None:
        self.window_manager.fit_to_host()
        self._refresh_capture_region()

    def moveEvent(self, event: QMoveEvent) -> None:  # keeps a docked window glued to the frame
        super().moveEvent(event)
        if self.window_manager.mode == "dock":
            self.window_manager.fit_to_host()

    def _on_watchdog(self) -> None:
        wm = self.window_manager
        if wm.hwnd and not wm.window_exists():
            log.info("Application window was closed")
            wm.forget()
            wm.pid = None
            self._update_controls()
        if self.recorder.is_recording:
            text = f"{int(self.recorder.elapsed // 60):02d}:{int(self.recorder.elapsed % 60):02d}"
            self.rec_pill.set_state(f"● REC {text}", "rec")
            self.rec_pill.show()
            self.evidence_panel.set_recording(True, text)
        elif self.rec_pill.isVisible() and self.rec_pill.property("kind") == "rec":
            # Capture stopped by itself (max length reached) - save what we have.
            self.stop_recording()

    # ===================================================== ADO: browse
    def load_plans(self) -> None:
        self.ado_pill.set_state("ADO: connecting…", "warn")
        self._run_task(self.ado_client.list_plans, on_success=self._on_plans, on_error=self._on_ado_error)

    def _on_plans(self, plans: list[dict]) -> None:
        cfg = self.ado_client.config
        self.ado_pill.set_state(f"ADO ✓ {cfg.organization}/{cfg.project}" + (" · demo" if self.demo else ""), "ok")
        if not plans:
            self.explorer.set_message("No active test plans in this project.")
        self.explorer.set_plans(plans, select_id=cfg.test_plan_id)
        if self.bug_meta is None:
            self._run_task(
                self.ado_client.bug_metadata,
                self.bug_work_item_type,
                on_success=lambda meta: setattr(self, "bug_meta", meta),
                on_error=lambda msg: log.warning("Bug form will use defaults - could not read bug metadata: %s", msg),
            )

    def load_suites(self, plan_id: int) -> None:
        self.explorer.set_message("Loading suites…")
        self._run_task(
            self.ado_client.list_suites,
            plan_id,
            on_success=lambda suites: self.explorer.set_suites(plan_id, suites),
            on_error=self._on_ado_error,
        )

    def load_points(self, plan_id: int, suite_id: int) -> None:
        self.explorer.set_message("Loading test points…")
        self._run_task(
            self.ado_client.list_points, plan_id, suite_id, on_success=self.explorer.set_points, on_error=self._on_ado_error
        )

    def open_point(self, point: TestPoint) -> None:
        if self.session and self.session.point.id == point.id:
            return
        if self._session_dirty():
            answer = QMessageBox.question(
                self, APP_NAME, f"Discard the unpublished results for TC {self.session.point.test_case_id}?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.explorer.select_point(self.session.point.id)
                return
        self.statusBar().showMessage(f"Loading steps for TC {point.test_case_id}…")
        self._run_task(self.ado_client.load_session, point, on_success=self._on_session_loaded, on_error=self._on_ado_error)

    def _on_session_loaded(self, session: TestSession) -> None:
        carried = self.loose_evidence if self.session is None else []
        self.session = session
        self.session.evidence.extend(carried)
        self.loose_evidence = []
        self.session_published = False
        self.runner.load(session)
        self.publish_panel.reset()
        self._refresh_session_views()
        log.info("Loaded TC %s '%s' (%d steps)", session.point.test_case_id, session.point.title, len(session.steps))

    def _session_dirty(self) -> bool:
        return bool(self.session and not self.session_published and (self.session.done_count or self.session.evidence))

    def _on_ado_error(self, message: str) -> None:
        self.ado_pill.set_state("ADO: error", "err")
        self._show_error(message)

    # ===================================================== steps
    def set_step_outcome(self, index: int, outcome: str) -> None:
        if not self.session or not (0 <= index < len(self.session.steps)):
            return
        self.session.set_step_outcome(index, outcome)
        self.runner.set_current(index)
        self._refresh_session_views()
        if outcome == PASSED:
            self.runner.advance()
        elif outcome == FAILED:
            self.runner.cards[index].comment_edit.setFocus()

    def _on_step_comment(self, index: int, text: str) -> None:
        if self.session and 0 <= index < len(self.session.steps):
            self.session.steps[index].comment = text.strip()

    def _shortcut_step(self, outcome: str) -> None:
        if self.session and self.runner.current >= 0:
            self.set_step_outcome(self.runner.current, outcome)

    def _refresh_session_views(self) -> None:
        self.runner.refresh()
        items = self.session.evidence if self.session else self.loose_evidence
        self.evidence_panel.set_items(items)
        self.publish_panel.show_suggestion(self.session.suggested_outcome() if self.session else None)
        self._update_controls()

    # ===================================================== evidence
    def _evidence_name(self, suffix: str) -> str:
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        if self.session:
            return f"TC{self.session.point.test_case_id}_{suffix}_{stamp}"
        return f"{suffix}_{stamp}"

    def _add_evidence(self, item: EvidenceItem) -> None:
        (self.session.evidence if self.session else self.loose_evidence).append(item)
        self._refresh_session_views()

    def _step_ref(self, index: Optional[int]) -> tuple[Optional[int], Optional[str]]:
        if self.session and index is not None and 0 <= index < len(self.session.steps):
            step = self.session.steps[index]
            return step.number, step.action_path
        return None, None

    def take_screenshot(self, step_index: Optional[int] = None) -> None:
        self._refresh_capture_region()
        number, path_ref = self._step_ref(step_index)
        try:
            path = self.recorder.screenshot(self._evidence_name(f"step{number}" if number else "shot"))
        except Exception as exc:
            self._show_error(f"Screenshot failed: {exc}")
            return
        self._add_evidence(EvidenceItem(path, "screenshot", number, path_ref))

    def toggle_recording(self) -> None:
        if self.recorder.is_recording:
            self.stop_recording()
            return
        self._refresh_capture_region()
        try:
            self.recorder.start()
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._recording_step = None  # recordings cover the whole test
        self.evidence_panel.set_recording(True, "00:00")

    def _save_recording(self) -> EvidenceItem:
        name = self._evidence_name("recording")
        if self.evidence_panel.format_combo.currentText() == "MP4":
            path = self.recorder.stop_and_save_mp4(name)
        else:
            path = self.recorder.stop_and_save_gif(name)
        return EvidenceItem(path, "recording")

    def stop_recording(self) -> None:
        self.evidence_panel.record_btn.setEnabled(False)
        self.evidence_panel.set_recording(False)
        self.rec_pill.set_state("Saving…", "warn")
        self._run_task(
            self._save_recording,
            on_success=self._add_evidence,
            on_finished=lambda: (self.evidence_panel.record_btn.setEnabled(True), self.rec_pill.hide()),
        )

    def open_evidence_folder(self) -> None:
        self.recorder.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.recorder.output_dir)))

    # ===================================================== publish
    def publish(self) -> None:
        if self.session is None or self.ado_client is None:
            return
        session = self.session
        suggested = session.suggested_outcome()
        outcome = self.publish_panel.chosen_outcome(suggested)
        if outcome is None:
            QMessageBox.information(
                self,
                APP_NAME,
                "Not every step has a result yet.\nMark the remaining steps, or pick an outcome explicitly.",
            )
            return
        unmarked = sum(1 for s in session.steps if s.outcome == UNSPECIFIED)
        if outcome == PASSED and unmarked:
            answer = QMessageBox.question(self, APP_NAME, f"{unmarked} step(s) have no result. Publish as Passed anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return

        if outcome == FAILED and not session.bugs:
            box = QMessageBox(QMessageBox.Icon.Question, APP_NAME, "This test failed but no bug has been raised yet.", parent=self)
            report_btn = box.addButton("Report bug first", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Publish without bug", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is cancel_btn:
                return
            if box.clickedButton() is report_btn:
                self.open_bug_dialog()
                if not session.bugs:
                    return  # bug form was cancelled

        comment = self.publish_panel.comment_edit.toPlainText().strip() or None
        stop_recording = self.recorder.is_recording and bool(self.ev_cfg.get("auto_stop_on_publish", True))

        def job() -> dict[str, Any]:
            if stop_recording:
                session.evidence.append(self._save_recording())
            return self.ado_client.record_execution(session, outcome, comment)

        self.publish_panel.publish_btn.setEnabled(False)
        self.publish_panel.status.setText(f"Publishing <b>{outcome}</b>…")
        self._run_task(
            job,
            on_success=self._on_published,
            on_error=self._on_publish_error,
            on_finished=self._refresh_session_views,
        )

    def _on_published(self, report: dict[str, Any]) -> None:
        self.session_published = True
        if self.session:
            self.explorer.set_point_outcome(self.session.point.id, report["outcome"])
        lines = [f"✔ <b>{report['outcome']}</b> published → <a style='color:#79b0ff' href='{report['url']}'>run #{report['run_id']}</a>"]
        if report["attachments"]:
            lines.append(f"{len(report['attachments'])} attachment(s) uploaded")
        for bug in report.get("bugs", []):
            lines.append(f"🐞 <a style='color:#79b0ff' href='{bug['url']}'>Bug #{bug['id']}</a> linked to this result")
        self.publish_panel.status.setText("<br>".join(lines))
        log.info("Published %s (run %s, result %s)", report["outcome"], report["run_id"], report["result_id"])

    def _on_publish_error(self, message: str) -> None:
        self.publish_panel.status.setText(f"<span style='color:#ff7b72'>✖ {message}</span>")
        self._show_error(message)

    # ===================================================== bugs
    @property
    def bug_work_item_type(self) -> str:
        return str(self.bug_cfg.get("work_item_type") or "Bug")

    def _screen_description(self) -> str:
        screen = self.screen()
        if screen is None:
            return ""
        size, ratio = screen.size(), screen.devicePixelRatio()
        return f"{round(size.width() * ratio)}×{round(size.height() * ratio)} @ {round(ratio * 100)}% scaling"

    def _new_bug_report(self, step_index: Optional[int]) -> BugReport:
        session = self.session
        environment = collect_environment(
            exe_path=self.exe_path,
            app_version=self.bug_cfg.get("app_version") or self._app_version,
            screen=self._screen_description(),
            configuration=session.point.configuration if session else None,
            include_machine_name=bool(self.bug_cfg.get("include_machine_name", True)),
        )
        if self.window_manager.run_as_label:
            environment["Application ran as"] = self.window_manager.run_as_label
        found_in = str(self.bug_cfg.get("app_version") or self._app_version or "")
        report = BugReport.from_session(session, step_index, environment, found_in)
        if session is None:
            report.attachments = [e.path for e in self.loose_evidence if e.attach and e.path.exists()]
        report.area_path = str(self.bug_cfg.get("default_area_path") or "")
        report.iteration_path = str(self.bug_cfg.get("default_iteration_path") or "")
        report.assigned_to = str(self.bug_cfg.get("default_assigned_to") or "")
        report.severity = str(self.bug_cfg.get("default_severity") or report.severity)
        report.extra_fields = {str(k): str(v) for k, v in (self.bug_cfg.get("extra_fields") or {}).items()}
        report.tags = list(dict.fromkeys([*(self.bug_cfg.get("tags") or ["MAS-QA-Bridge"]), *(
            [session.point.configuration] if session and session.point.configuration else [])]))
        return report

    def open_bug_dialog(self, step_index: Optional[int] = None) -> None:
        """Open the bug form, pre-filled from the current test (or blank for exploratory testing)."""
        if self.ado_client is None:
            QMessageBox.warning(self, APP_NAME, "Azure DevOps is not configured - see config.yaml.")
            return
        if self.session and step_index is not None:
            self._on_step_comment(step_index, self.runner.cards[step_index].comment_edit.text())

        dialog = BugDialog(self._new_bug_report(step_index), self.bug_meta, self.recorder.output_dir / "bug_drafts", self)

        def on_screenshot() -> None:
            dialog.hide()  # don't capture the form itself

            def capture() -> None:
                self._refresh_capture_region()
                try:
                    path = self.recorder.screenshot(self._evidence_name("bug"))
                    self._add_evidence(EvidenceItem(path, "screenshot"))
                    dialog.add_attachment(path)
                except Exception as exc:
                    log.error("Screenshot failed: %s", exc)
                dialog.show()

            QTimer.singleShot(350, capture)

        def on_submit(report: BugReport) -> None:
            self._run_task(
                self.ado_client.create_bug,
                report,
                self.bug_work_item_type,
                on_success=dialog.submission_succeeded,
                on_error=dialog.submission_failed,
            )

        dialog.screenshot_requested.connect(on_screenshot)
        dialog.submit_requested.connect(on_submit)
        dialog.exec()
        if dialog.created_bug:
            self._on_bug_created(dialog.created_bug)

    def _on_bug_created(self, bug: dict[str, Any]) -> None:
        if self.session:
            self.session.bugs.append(bug)
            self.publish_panel.show_bugs(self.session.bugs)
        skipped = f"<br><i>Skipped fields not in your process: {', '.join(bug['skipped_fields'])}</i>" if bug.get("skipped_fields") else ""
        linked = " and linked to the test result" if self.session and self.session.result_id else ""
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"🐞 <b>Bug #{bug['id']}</b> created{linked}.<br><a style='color:#79b0ff' href='{bug['url']}'>{bug['title']}</a>"
            f"<br>{len(bug.get('attachments', []))} attachment(s) uploaded.{skipped}"
        )
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def run_diagnostics(self) -> None:
        if self.ado_client is None:
            return
        self.check_btn.setEnabled(False)
        self.ado_pill.set_state("ADO: checking…", "warn")

        def show(checks: list[tuple[str, bool, str]]) -> None:
            ok = all(passed for _, passed, _ in checks)
            self.ado_pill.set_state("ADO: ready" if ok else "ADO: problems", "ok" if ok else "err")
            rows = "".join(
                f"<tr><td>{'✅' if passed else '❌'}</td><td><b>{name}</b></td><td>{detail}</td></tr>" for name, passed, detail in checks
            )
            box = QMessageBox(self)
            box.setWindowTitle("Azure DevOps check")
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(f"<table cellpadding='4'>{rows}</table>")
            box.exec()

        self._run_task(
            self.ado_client.run_diagnostics,
            self.bug_work_item_type,
            on_success=show,
            on_finished=lambda: self.check_btn.setEnabled(True),
        )

    # ============================================================ shutdown
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._session_dirty() and not self._force_close:
            answer = QMessageBox.question(self, APP_NAME, "There are unpublished results. Quit anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._region_timer.stop()
        self._watchdog.stop()
        if self.recorder.is_recording:
            self.recorder.stop()
        if str(self.app_cfg.get("on_exit", "release")).lower() == "terminate":
            self.window_manager.terminate()
        else:
            self.window_manager.release()
        self.pool.waitForDone(5000)
        logging.getLogger().removeHandler(self._log_handler)
        super().closeEvent(event)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--demo", action="store_true", help="offline demo with sample Azure DevOps data")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="path to config.yaml")
    parser.add_argument("--self-test", action="store_true", help="check the installation (no window) and exit")
    args, qt_args = parser.parse_known_args(argv)

    if args.self_test:
        from self_test import run_self_test

        return run_self_test(BASE_DIR, args.config)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)

    client = None
    if args.demo:
        from demo_data import DemoAdoClient

        client = DemoAdoClient()
    window = MainWindow(load_config(args.config), ado_client=client, demo=args.demo)
    if args.demo:
        from demo_data import DemoAppWidget

        demo_app = DemoAppWidget(window.host_frame)
        demo_app.setGeometry(window.host_frame.rect().adjusted(1, 1, -1, -1))
        window.host_frame.resized.connect(
            lambda: demo_app.setGeometry(window.host_frame.rect().adjusted(1, 1, -1, -1))
        )
        demo_app.show()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
