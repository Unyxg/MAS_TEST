"""
window_manager.py - Process launching & window "swallowing" for MAS-QA-Bridge.

Launches an external Windows executable with ``subprocess.Popen``, locates its
main top-level window (HWND) by polling ``win32gui.EnumWindows``, and re-parents
that window into a Qt host widget with ``win32gui.SetParent`` after stripping
its native caption/borders so it looks like part of the dashboard.

Only the Windows code paths are functional; the module imports cleanly on other
platforms so the UI can still be opened for layout work.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

import psutil

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import pywintypes
    import win32con
    import win32gui
    import win32process

log = logging.getLogger(__name__)

# Attribute id for DwmGetWindowAttribute: non-zero when the window is "cloaked"
# (hidden UWP frames, windows on other virtual desktops, ...).
_DWMWA_CLOAKED = 14
_MIN_WINDOW_SIDE = 50  # px - ignore tiny helper / message windows


class EmbedError(RuntimeError):
    """Raised when a window cannot be found, embedded or released."""


@dataclass
class _SavedWindowState:
    """Everything needed to put a swallowed window back the way we found it."""

    hwnd: int
    style: int
    ex_style: int
    rect: tuple[int, int, int, int]


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise EmbedError("Window embedding is only supported on Windows.")


def _int32(value: int) -> int:
    """Coerce a style bitmask to the signed 32-bit int SetWindowLong expects."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


if IS_WINDOWS:
    # Frame decorations removed from the target window.
    _STRIP_STYLE = (
        win32con.WS_CAPTION
        | win32con.WS_THICKFRAME
        | win32con.WS_MINIMIZEBOX
        | win32con.WS_MAXIMIZEBOX
        | win32con.WS_SYSMENU
        | win32con.WS_POPUP
        | win32con.WS_MAXIMIZE
        | win32con.WS_MINIMIZE
    )
    _STRIP_EX_STYLE = (
        win32con.WS_EX_DLGMODALFRAME
        | win32con.WS_EX_CLIENTEDGE
        | win32con.WS_EX_STATICEDGE
        | win32con.WS_EX_WINDOWEDGE
        | win32con.WS_EX_APPWINDOW
    )


def _is_cloaked(hwnd: int) -> bool:
    cloaked = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd),
            _DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except (AttributeError, OSError):
        return False
    return bool(cloaked.value)


class WindowManager:
    """Owns the lifecycle of one launched + embedded external application."""

    def __init__(self, find_timeout: float = 20.0, poll_interval: float = 0.25) -> None:
        self.find_timeout = find_timeout
        self.poll_interval = poll_interval

        self.exe_path: Optional[str] = None
        self.process: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.hwnd: Optional[int] = None
        self.host_hwnd: Optional[int] = None

        self._launch_time = 0.0
        self._saved: Optional[_SavedWindowState] = None

    # ------------------------------------------------------------------ state
    @property
    def is_running(self) -> bool:
        return bool(self._live_processes())

    @property
    def is_embedded(self) -> bool:
        return self._saved is not None and self.window_exists()

    def window_exists(self) -> bool:
        return bool(IS_WINDOWS and self.hwnd and win32gui.IsWindow(self.hwnd))

    # ---------------------------------------------------------------- launch
    def launch(
        self,
        exe_path: str,
        args: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
    ) -> int:
        """Start ``exe_path`` and return its PID."""
        _require_windows()
        if not os.path.isfile(exe_path):
            raise FileNotFoundError(exe_path)

        self.exe_path = os.path.abspath(exe_path)
        self.hwnd = None
        self._saved = None
        self._launch_time = time.time()
        self.process = subprocess.Popen(
            [self.exe_path, *(args or [])],
            cwd=cwd or os.path.dirname(self.exe_path) or None,
        )
        self.pid = self.process.pid
        log.info("Launched %s (PID %s)", self.exe_path, self.pid)
        return self.pid

    def candidate_pids(self) -> set[int]:
        """PIDs whose windows may belong to the launched app.

        Includes the launched process, all of its descendants, and any process
        with the same image name started since launch (covers launcher stubs
        that spawn the real app and exit immediately).
        """
        pids: set[int] = set()
        if self.pid is None:
            return pids

        try:
            root = psutil.Process(self.pid)
            pids.add(root.pid)
            pids.update(child.pid for child in root.children(recursive=True))
        except psutil.Error:
            pass

        if self.exe_path:
            image = os.path.basename(self.exe_path).lower()
            for proc in psutil.process_iter(["pid", "name", "create_time"]):
                info = proc.info
                if (info.get("name") or "").lower() == image and (
                    info.get("create_time") or 0
                ) >= self._launch_time - 1:
                    pids.add(info["pid"])

        pids.discard(os.getpid())
        return pids

    # ----------------------------------------------------------- find window
    def find_window(
        self,
        timeout: Optional[float] = None,
        title_contains: Optional[str] = None,
        cancel: Optional[threading.Event] = None,
    ) -> int:
        """Poll ``EnumWindows`` until the app's main window appears; return its HWND.

        Blocking - call it from a worker thread, not the Qt GUI thread.
        """
        _require_windows()
        if self.pid is None:
            raise EmbedError("No process has been launched.")

        deadline = time.monotonic() + (timeout if timeout is not None else self.find_timeout)
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise EmbedError("Window search cancelled.")
            pids = self.candidate_pids()
            if not pids:
                code = self.process.poll() if self.process else None
                raise EmbedError(f"Target process exited before showing a window (exit code {code}).")
            hwnd = self._best_window(pids, title_contains)
            if hwnd:
                self.hwnd = hwnd
                log.info(
                    "Found window 0x%08X '%s' (PID %s)",
                    hwnd,
                    win32gui.GetWindowText(hwnd),
                    win32process.GetWindowThreadProcessId(hwnd)[1],
                )
                return hwnd
            time.sleep(self.poll_interval)

        raise EmbedError(
            f"No visible top-level window appeared for PID {self.pid} within the timeout."
        )

    def _best_window(self, pids: set[int], title_contains: Optional[str]) -> Optional[int]:
        """Pick the largest visible, unowned, titled top-level window of ``pids``."""
        candidates: list[tuple[bool, int, int]] = []
        needle = title_contains.lower() if title_contains else None

        def _callback(hwnd: int, _extra: object) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd) or win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in pids:
                    return True
                if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                    return True
                if _is_cloaked(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if needle and needle not in title.lower():
                    return True
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width, height = right - left, bottom - top
                if width < _MIN_WINDOW_SIDE or height < _MIN_WINDOW_SIDE:
                    return True
                candidates.append((bool(title), width * height, hwnd))
            except pywintypes.error:
                pass  # window vanished mid-enumeration
            return True

        win32gui.EnumWindows(_callback, None)
        if not candidates:
            return None
        candidates.sort(reverse=True)  # titled first, then largest area
        return candidates[0][2]

    # ----------------------------------------------------------------- embed
    def embed(self, host_hwnd: int, hwnd: Optional[int] = None) -> None:
        """Strip ``hwnd``'s borders and re-parent it into ``host_hwnd``.

        Must be called from the Qt GUI thread.
        """
        _require_windows()
        hwnd = hwnd or self.hwnd
        if not hwnd or not win32gui.IsWindow(hwnd):
            raise EmbedError("Target window handle is not valid.")
        if self._saved is not None and self._saved.hwnd != hwnd:
            self.release()

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if self._saved is None:
            self._saved = _SavedWindowState(hwnd, style, ex_style, win32gui.GetWindowRect(hwnd))

        if win32gui.IsIconic(hwnd) or style & win32con.WS_MAXIMIZE:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)

        # Per the SetParent docs: clear WS_POPUP and set WS_CHILD *before* re-parenting.
        new_style = (style & ~_STRIP_STYLE) | win32con.WS_CHILD | win32con.WS_VISIBLE
        new_ex_style = ex_style & ~_STRIP_EX_STYLE
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _int32(new_style))
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _int32(new_ex_style))

        try:
            win32gui.SetParent(hwnd, host_hwnd)
        except pywintypes.error as exc:
            # Typical cause: target runs elevated (UIPI) and we don't.
            self._restore_styles()
            raise EmbedError(f"SetParent failed: {exc}. Is the target running elevated?") from exc

        self.hwnd = hwnd
        self.host_hwnd = host_hwnd
        self.fit_to_host()
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        log.info("Embedded window 0x%08X into host 0x%08X", hwnd, host_hwnd)

    def fit_to_host(self) -> None:
        """Resize the embedded window to fill the host's client area."""
        if not (IS_WINDOWS and self.host_hwnd and self.window_exists() and self._saved):
            return
        left, top, right, bottom = win32gui.GetClientRect(self.host_hwnd)
        win32gui.SetWindowPos(
            self.hwnd,
            win32con.HWND_TOP,
            0,
            0,
            max(1, right - left),
            max(1, bottom - top),
            win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_FRAMECHANGED
            | win32con.SWP_SHOWWINDOW,
        )

    def focus(self) -> None:
        """Give keyboard focus to the embedded window."""
        if self.window_exists():
            try:
                win32gui.SetFocus(self.hwnd)
            except pywintypes.error:
                pass  # needs AttachThreadInput for some apps; clicking inside works regardless

    # --------------------------------------------------------------- release
    def release(self) -> None:
        """Un-embed the window and restore its original parent/styles/position."""
        if not IS_WINDOWS or self._saved is None:
            self._saved = None
            self.host_hwnd = None
            return

        saved = self._saved
        if win32gui.IsWindow(saved.hwnd):
            try:
                win32gui.SetParent(saved.hwnd, 0)
                self._restore_styles()
                left, top, right, bottom = saved.rect
                win32gui.SetWindowPos(
                    saved.hwnd,
                    win32con.HWND_TOP,
                    left,
                    top,
                    right - left,
                    bottom - top,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW,
                )
                log.info("Released window 0x%08X back to the desktop", saved.hwnd)
            except pywintypes.error as exc:
                log.warning("Could not fully release window 0x%08X: %s", saved.hwnd, exc)
        self._saved = None
        self.host_hwnd = None

    def _restore_styles(self) -> None:
        if self._saved and win32gui.IsWindow(self._saved.hwnd):
            win32gui.SetWindowLong(self._saved.hwnd, win32con.GWL_STYLE, _int32(self._saved.style))
            win32gui.SetWindowLong(self._saved.hwnd, win32con.GWL_EXSTYLE, _int32(self._saved.ex_style))

    def forget(self) -> None:
        """Drop references to a window that has already been destroyed."""
        self._saved = None
        self.host_hwnd = None
        self.hwnd = None

    # ------------------------------------------------------------- terminate
    def _live_processes(self) -> list[psutil.Process]:
        procs = []
        for pid in self.candidate_pids():
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    procs.append(proc)
            except psutil.Error:
                continue
        return procs

    def terminate(self, timeout: float = 5.0) -> None:
        """Kill the launched app (and its child processes)."""
        procs = self._live_processes()
        for proc in procs:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=timeout)
        for proc in alive:
            try:
                proc.kill()
            except psutil.Error:
                pass
        if procs:
            log.info("Terminated %d process(es) for %s", len(procs), self.exe_path)
        self.forget()
        self.process = None
        self.pid = None
