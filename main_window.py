"""
main_window.py - MAS-QA-Bridge dashboard (PyQt6).

Layout
    +-----------------------------------------------------------------+
    | [Browse Executable...]  C:\\path\\to\\app.exe      [Launch & Embed] |
    +-----------------------------------------------------+-----------+
    |                                                     | Target app|
    |          central QFrame  (hosts the external app)   | Evidence  |
    |                                                     | ADO result|
    |                                                     | Log       |
    +-----------------------------------------------------+-----------+

Run:  python main_window.py
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from PyQt6.QtCore import QObject, QPoint, QRegularExpression, QRunnable, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QRegularExpressionValidator, QResizeEvent
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ado_test_api import AdoConfig, AdoTestClient
from evidence_capture import EvidenceRecorder, Region
from window_manager import IS_WINDOWS, EmbedError, WindowManager

if IS_WINDOWS:
    import win32gui

APP_NAME = "MAS-QA-Bridge"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"

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
    """Forwards log records (from any thread) to a widget via a queued signal."""

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
# Host frame
# ----------------------------------------------------------------------------
class HostFrame(QFrame):
    """Central frame whose native HWND becomes the parent of the external app."""

    resized = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("hostFrame")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumSize(640, 480)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        # Give this frame (only) its own native window handle for SetParent.
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        self.placeholder = QLabel(
            "No application embedded.\n\nUse \"Browse Executable…\" then \"Launch & Embed\".", self
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setObjectName("placeholder")

    def native_handle(self) -> int:
        return int(self.winId())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.placeholder.setGeometry(self.rect())
        self.resized.emit()


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__()
        self.config = config
        self.app_cfg: dict[str, Any] = config.get("app") or {}
        self.ev_cfg: dict[str, Any] = config.get("evidence") or {}
        self.ado_cfg: dict[str, Any] = config.get("ado") or {}

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
        self.last_evidence: Optional[Path] = None
        self.ado_client: Optional[AdoTestClient] = None

        self._build_ui()
        self._install_log_handler()
        self.ado_client = self._build_ado_client()

        # Keep the capture rectangle current (read by the recorder thread).
        self._region_timer = QTimer(self)
        self._region_timer.setInterval(200)
        self._region_timer.timeout.connect(self._refresh_capture_region)
        self._region_timer.start()

        # Notice when the embedded app closes itself; tick the recording clock.
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._on_watchdog)
        self._watchdog.start()

        self._set_exe_path(self.exe_path)
        self._update_controls()

    # ================================================================== UI
    def _build_ui(self) -> None:
        self.setWindowTitle(APP_NAME)
        self.resize(1600, 950)

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        # ---- top bar
        top = QHBoxLayout()
        self.browse_btn = QPushButton("Browse Executable…")
        self.browse_btn.clicked.connect(self.browse_executable)
        self.exe_label = QLabel()
        self.exe_label.setObjectName("exeLabel")
        self.exe_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.exe_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.launch_btn = QPushButton("Launch && Embed")
        self.launch_btn.setObjectName("primary")
        self.launch_btn.clicked.connect(self.launch_and_embed)
        top.addWidget(self.browse_btn)
        top.addWidget(self.exe_label, stretch=1)
        top.addWidget(self.launch_btn)
        root.addLayout(top)

        # ---- body: host frame + sidebar
        body = QHBoxLayout()
        self.host_frame = HostFrame(central)
        self.host_frame.resized.connect(self._on_host_resized)
        body.addWidget(self.host_frame, stretch=1)
        body.addWidget(self._build_sidebar())
        root.addLayout(body, stretch=1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self.setStyleSheet(STYLESHEET)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setFixedWidth(360)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)

        # ---- target app
        app_box = QGroupBox("Target Application")
        app_layout = QHBoxLayout(app_box)
        self.release_btn = QPushButton("Release")
        self.release_btn.setToolTip("Un-embed the window back to the desktop")
        self.release_btn.clicked.connect(self.release_window)
        self.reembed_btn = QPushButton("Re-embed")
        self.reembed_btn.clicked.connect(self.reembed_window)
        self.kill_btn = QPushButton("Terminate")
        self.kill_btn.clicked.connect(self.terminate_app)
        for btn in (self.release_btn, self.reembed_btn, self.kill_btn):
            app_layout.addWidget(btn)
        layout.addWidget(app_box)

        # ---- evidence
        ev_box = QGroupBox("Evidence Capture")
        ev_layout = QVBoxLayout(ev_box)
        form = QFormLayout()
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 30)
        self.fps_spin.setValue(self.recorder.fps)
        form.addRow("Frames / sec", self.fps_spin)
        ev_layout.addLayout(form)
        rec_row = QHBoxLayout()
        self.rec_start_btn = QPushButton("● Start Recording")
        self.rec_start_btn.setObjectName("record")
        self.rec_start_btn.clicked.connect(self.start_recording)
        self.rec_stop_btn = QPushButton("■ Stop && Save GIF")
        self.rec_stop_btn.clicked.connect(self.stop_recording)
        rec_row.addWidget(self.rec_start_btn)
        rec_row.addWidget(self.rec_stop_btn)
        ev_layout.addLayout(rec_row)
        self.rec_status = QLabel("Idle")
        self.evidence_label = QLabel("No evidence yet")
        self.evidence_label.setWordWrap(True)
        self.open_folder_btn = QPushButton("Open Evidence Folder")
        self.open_folder_btn.clicked.connect(self.open_evidence_folder)
        ev_layout.addWidget(self.rec_status)
        ev_layout.addWidget(self.evidence_label)
        ev_layout.addWidget(self.open_folder_btn)
        layout.addWidget(ev_box)

        # ---- ADO
        ado_box = QGroupBox("Azure DevOps Test Plans")
        ado_layout = QVBoxLayout(ado_box)
        ado_form = QFormLayout()
        digits = QRegularExpressionValidator(QRegularExpression(r"\d{1,10}"))
        self.plan_edit = QLineEdit(str(self.ado_cfg.get("test_plan_id") or ""))
        self.plan_edit.setValidator(digits)
        self.point_edit = QLineEdit()
        self.point_edit.setValidator(digits)
        self.point_edit.setPlaceholderText("e.g. 1234")
        ado_form.addRow("Test Plan ID", self.plan_edit)
        ado_form.addRow("Test Point ID", self.point_edit)
        ado_layout.addLayout(ado_form)
        self.comment_edit = QPlainTextEdit()
        self.comment_edit.setPlaceholderText("Result comment / defect notes…")
        self.comment_edit.setFixedHeight(70)
        ado_layout.addWidget(self.comment_edit)
        self.attach_chk = QCheckBox("Attach evidence (GIF)")
        self.attach_chk.setChecked(True)
        ado_layout.addWidget(self.attach_chk)

        verdict_row = QHBoxLayout()
        self.pass_btn = QPushButton("✔ Pass")
        self.pass_btn.setObjectName("pass")
        self.pass_btn.clicked.connect(lambda: self.submit_verdict("Passed"))
        self.fail_btn = QPushButton("✖ Fail")
        self.fail_btn.setObjectName("fail")
        self.fail_btn.clicked.connect(lambda: self.submit_verdict("Failed"))
        self.blocked_btn = QPushButton("Blocked")
        self.blocked_btn.clicked.connect(lambda: self.submit_verdict("Blocked"))
        for btn in (self.pass_btn, self.fail_btn, self.blocked_btn):
            verdict_row.addWidget(btn)
        ado_layout.addLayout(verdict_row)

        self.test_conn_btn = QPushButton("Test ADO Connection")
        self.test_conn_btn.clicked.connect(self.test_ado_connection)
        ado_layout.addWidget(self.test_conn_btn)
        self.ado_status = QLabel("Not connected")
        self.ado_status.setWordWrap(True)
        self.ado_status.setOpenExternalLinks(True)
        self.ado_status.setTextFormat(Qt.TextFormat.RichText)
        ado_layout.addWidget(self.ado_status)
        layout.addWidget(ado_box)

        # ---- log
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_box, stretch=1)
        return sidebar

    def _install_log_handler(self) -> None:
        handler = QtLogHandler()
        handler.setLevel(logging.INFO)
        handler.bridge.message.connect(self.log_view.appendPlainText)
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _build_ado_client(self) -> Optional[AdoTestClient]:
        try:
            client = AdoTestClient(AdoConfig.from_dict(self.ado_cfg))
        except ValueError as exc:
            log.warning("ADO integration disabled: %s", exc)
            self.ado_status.setText(f"<i>ADO disabled - {exc}. Edit config.yaml.</i>")
            return None
        self.ado_status.setText(f"Configured for <b>{client.config.organization}/{client.config.project}</b>")
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
        self.exe_label.setText(path or "No executable selected")
        self.exe_label.setToolTip(path or "")
        self._update_controls()

    def _update_controls(self) -> None:
        wm = self.window_manager
        embedded = wm.is_embedded
        has_window = wm.window_exists()
        recording = self.recorder.is_recording
        self.launch_btn.setEnabled(bool(self.exe_path) and IS_WINDOWS)
        self.release_btn.setEnabled(embedded)
        self.reembed_btn.setEnabled(has_window and not embedded)
        self.kill_btn.setEnabled(wm.pid is not None)
        self.rec_start_btn.setEnabled(not recording)
        self.rec_stop_btn.setEnabled(recording)
        self.fps_spin.setEnabled(not recording)
        self.host_frame.placeholder.setVisible(not embedded)

    def _set_verdict_busy(self, busy: bool) -> None:
        for btn in (self.pass_btn, self.fail_btn, self.blocked_btn, self.test_conn_btn):
            btn.setEnabled(not busy)

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

    # ======================================================= step 1 / 2
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
            answer = QMessageBox.question(
                self, APP_NAME, "An application is already running. Terminate it and launch the new one?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.window_manager.terminate()

        try:
            self.window_manager.launch(self.exe_path, args=self.app_cfg.get("launch_args") or [])
        except Exception as exc:
            self._show_error(f"Could not launch {self.exe_path}: {exc}")
            return

        self.launch_btn.setEnabled(False)
        self.statusBar().showMessage("Waiting for the application window…")
        self._run_task(
            self.window_manager.find_window,
            title_contains=self.app_cfg.get("window_title_contains") or None,
            on_success=self._embed_found_window,
            on_finished=self._update_controls,
        )

    def _embed_found_window(self, hwnd: int) -> None:
        try:
            self.window_manager.embed(self.host_frame.native_handle(), hwnd)
        except EmbedError as exc:
            self._show_error(str(exc))
            return
        self.statusBar().showMessage(f"Embedded: {os.path.basename(self.exe_path or '')}")
        self._update_controls()

    def release_window(self) -> None:
        self.window_manager.release()
        self.statusBar().showMessage("Window released to desktop")
        self._update_controls()

    def reembed_window(self) -> None:
        self._embed_found_window(self.window_manager.hwnd)

    def terminate_app(self) -> None:
        self.window_manager.terminate()
        self.statusBar().showMessage("Application terminated")
        self._update_controls()

    def _on_host_resized(self) -> None:
        self.window_manager.fit_to_host()
        self._refresh_capture_region()

    def _on_watchdog(self) -> None:
        wm = self.window_manager
        if wm.hwnd and not wm.window_exists():
            log.info("Embedded application window was closed")
            wm.forget()
            self.statusBar().showMessage("Application closed")
        if self.recorder.is_recording:
            self.rec_status.setText(
                f"<span style='color:#d9534f'>● REC</span>  {self.recorder.elapsed:5.1f}s  "
                f"({self.recorder.frame_count} frames)"
            )
        self._update_controls()

    # ============================================================= step 3
    def start_recording(self) -> None:
        self._refresh_capture_region()
        self.recorder.fps = self.fps_spin.value()
        try:
            self.recorder.start()
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.rec_status.setText("<span style='color:#d9534f'>● REC</span>  starting…")
        self._update_controls()

    def stop_recording(self) -> None:
        self.rec_stop_btn.setEnabled(False)
        self.rec_status.setText("Saving GIF…")
        self._run_task(
            self.recorder.stop_and_save_gif,
            on_success=self._on_evidence_saved,
            on_finished=self._update_controls,
        )

    def _on_evidence_saved(self, path: Path) -> None:
        self.last_evidence = Path(path)
        size_mb = self.last_evidence.stat().st_size / 1e6
        self.rec_status.setText("Idle")
        self.evidence_label.setText(f"Latest: {self.last_evidence.name} ({size_mb:.1f} MB)")
        self.evidence_label.setToolTip(str(self.last_evidence))

    def open_evidence_folder(self) -> None:
        self.recorder.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.recorder.output_dir)))

    # ============================================================= step 4
    def test_ado_connection(self) -> None:
        if not self._require_ado():
            return
        self._set_verdict_busy(True)
        self.ado_status.setText("Connecting…")
        self._run_task(
            self.ado_client.authenticate,
            on_success=lambda info: self.ado_status.setText(
                f"✔ Connected to <b>{info['organization']}/{info['project']}</b>"
            ),
            on_error=self._on_ado_error,
            on_finished=lambda: self._set_verdict_busy(False),
        )

    def submit_verdict(self, outcome: str) -> None:
        if not self._require_ado():
            return
        if not self.point_edit.text():
            QMessageBox.warning(self, APP_NAME, "Enter the ADO Test Point ID first.")
            self.point_edit.setFocus()
            return
        if not self.plan_edit.text():
            QMessageBox.warning(self, APP_NAME, "Enter the ADO Test Plan ID first.")
            self.plan_edit.setFocus()
            return

        point_id = int(self.point_edit.text())
        plan_id = int(self.plan_edit.text())
        comment = self.comment_edit.toPlainText().strip() or None
        attach = self.attach_chk.isChecked()
        stop_active = self.recorder.is_recording and bool(self.ev_cfg.get("auto_stop_on_verdict", True))
        previous = self.last_evidence

        def job() -> dict[str, Any]:
            files: list[Path] = []
            new_evidence = None
            if stop_active:
                new_evidence = self.recorder.stop_and_save_gif()
            evidence = new_evidence or previous
            if attach and evidence and evidence.exists():
                files.append(evidence)
            report = self.ado_client.record_point_outcome(
                point_id, outcome, comment=comment, attachments=files, plan_id=plan_id
            )
            report["new_evidence"] = new_evidence
            return report

        self._set_verdict_busy(True)
        self.ado_status.setText(f"Publishing <b>{outcome}</b> for point {point_id}…")
        self._run_task(
            job,
            on_success=self._on_verdict_published,
            on_error=self._on_ado_error,
            on_finished=lambda: (self._set_verdict_busy(False), self._update_controls()),
        )

    def _on_verdict_published(self, report: dict[str, Any]) -> None:
        if report.get("new_evidence"):
            self._on_evidence_saved(report["new_evidence"])
        files = ", ".join(report["attachments"]) or "none"
        self.ado_status.setText(
            f"✔ <b>{report['outcome']}</b> → run <a href='{report['url']}'>#{report['run_id']}</a>"
            f"<br>Attachments: {files}"
        )
        log.info("Published %s (run %s, result %s)", report["outcome"], report["run_id"], report["result_id"])
        self.comment_edit.clear()

    def _on_ado_error(self, message: str) -> None:
        self.ado_status.setText(f"<span style='color:#d9534f'>✖ {message}</span>")
        self._show_error(message)

    def _require_ado(self) -> bool:
        if self.ado_client is None:
            QMessageBox.warning(
                self, APP_NAME, "Azure DevOps is not configured. Fill in config.yaml and set ADO_PAT, then restart."
            )
            return False
        return True

    # ============================================================ shutdown
    def closeEvent(self, event: QCloseEvent) -> None:
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


STYLESHEET = """
QFrame#hostFrame { background: #1e1e1e; border: 1px solid #3c3c3c; }
QLabel#placeholder { color: #8a8a8a; font-size: 15px; }
QLabel#exeLabel { padding: 4px 8px; border: 1px solid #c8c8c8; border-radius: 4px; }
QPushButton { padding: 6px 10px; }
QPushButton#primary { font-weight: bold; }
QPushButton#record { color: #c9302c; font-weight: bold; }
QPushButton#pass { background: #2e7d32; color: white; font-weight: bold; }
QPushButton#fail { background: #c62828; color: white; font-weight: bold; }
QPushButton#pass:disabled, QPushButton#fail:disabled { background: #9e9e9e; }
QGroupBox { font-weight: bold; margin-top: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
"""


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow(load_config())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
