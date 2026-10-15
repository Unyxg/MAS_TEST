"""
window_manager.py - Process launching & window "swallowing" for MAS-QA-Bridge.

Launches an external Windows executable with ``subprocess.Popen``, locates its
main top-level window (HWND) by polling ``win32gui.EnumWindows``, and re-parents
that window into a Qt host widget with ``win32gui.SetParent`` after stripping
its native caption/borders so it looks like part of the dashboard.

Two embedding modes are supported:

* ``reparent`` - true child window via ``SetParent`` (what the brief asked for).
  Cross-process parent/child windows share an input queue, which some apps
  (Chromium/Electron, some WPF) handle badly.
* ``dock`` - the window stays top-level but becomes borderless, *owned* by the
  dashboard (so it stays above it and minimises with it) and is kept glued on
  top of the host frame. Less invasive; works with more applications.

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
from typing import Callable, Optional, Sequence

import psutil

import run_as

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
_GWLP_HWNDPARENT = -8  # owner slot of a top-level window

EMBED_MODES = ("reparent", "dock")


class EmbedError(RuntimeError):
    """Raised when a window cannot be found, embedded or released."""


@dataclass
class _SavedWindowState:
    """Everything needed to put a swallowed window back the way we found it."""

    hwnd: int
    style: int
    ex_style: int
    rect: tuple[int, int, int, int]
    owner: int = 0


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


def _set_owner(hwnd: int, owner: int) -> int:
    """Set a top-level window's owner (GWLP_HWNDPARENT); returns the previous one."""
    user32 = ctypes.windll.user32
    setter = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    setter.restype = ctypes.c_void_p
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    return setter(hwnd, _GWLP_HWNDPARENT, owner) or 0


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
        self.process: Optional[run_as.LaunchedProcess] = None
        self.pid: Optional[int] = None
        self.run_as_label: str = ""
        self.hwnd: Optional[int] = None
        self.host_hwnd: Optional[int] = None
        self.mode: str = "reparent"

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
        run_as_mode: str = run_as.RUN_AS_CURRENT,
        credentials: Optional[run_as.Credentials] = None,
        environment: Optional[dict] = None,
        method: str = "direct",
    ) -> int:
        """Start ``exe_path`` (as the current user, elevated, or another user); return its PID."""
        _require_windows()
        if not os.path.isfile(exe_path):
            raise FileNotFoundError(exe_path)

        self.exe_path = os.path.abspath(exe_path)
        self.hwnd = None
        self._saved = None
        self._launch_time = time.time()
        self.process = run_as.launch(
            self.exe_path,
            list(args or []),
            cwd or os.path.dirname(self.exe_path) or None,
            run_as_mode,
            credentials,
            environment,
            method,
        )
        self.pid = self.process.pid
        self.run_as_label = self.process.run_as_label
        log.info("Launched %s (PID %s) as %s", self.exe_path, self.pid, self.run_as_label)
        return self.pid

    @property
    def target_elevated(self) -> Optional[bool]:
        return run_as.is_process_elevated(self.pid) if self.pid else None

    def can_embed(self) -> tuple[bool, str]:
        """UIPI check: a non-elevated dashboard cannot re-parent an elevated window."""
        if self.target_elevated and not run_as.is_admin():
            return False, (
                "The application runs elevated (as administrator) but MAS-QA-Bridge does not. "
                "Windows blocks embedding across that boundary - restart MAS-QA-Bridge as administrator."
            )
        return True, ""

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
                code = run_as.handle_exit_code(self.process.handle) if self.process else None
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
    def embed(
        self,
        host_hwnd: int,
        hwnd: Optional[int] = None,
        mode: str = "reparent",
        owner_hwnd: Optional[int] = None,
    ) -> None:
        """Strip ``hwnd``'s borders and put it inside / on top of ``host_hwnd``.

        ``mode="dock"`` requires ``owner_hwnd`` (the dashboard's top-level HWND).
        Must be called from the Qt GUI thread.
        """
        _require_windows()
        if mode not in EMBED_MODES:
            raise ValueError(f"mode must be one of {EMBED_MODES}")
        hwnd = hwnd or self.hwnd
        if not hwnd or not win32gui.IsWindow(hwnd):
            raise EmbedError("Target window handle is not valid.")
        allowed, reason = self.can_embed()
        if not allowed:
            raise EmbedError(reason)
        if self._saved is not None:
            self.release()

        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if win32gui.IsIconic(hwnd) or style & win32con.WS_MAXIMIZE:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        self._saved = _SavedWindowState(hwnd, style, ex_style, win32gui.GetWindowRect(hwnd))
        new_ex_style = ex_style & ~_STRIP_EX_STYLE

        try:
            if mode == "reparent":
                # Per the SetParent docs: clear WS_POPUP and set WS_CHILD *before* re-parenting.
                new_style = (style & ~_STRIP_STYLE) | win32con.WS_CHILD | win32con.WS_VISIBLE
                win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _int32(new_style))
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _int32(new_ex_style))
                win32gui.SetParent(hwnd, host_hwnd)
            else:
                if not owner_hwnd:
                    raise EmbedError("Dock mode needs the dashboard window handle.")
                new_style = (style & ~_STRIP_STYLE) | win32con.WS_POPUP | win32con.WS_VISIBLE
                win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, _int32(new_style))
                # Hide from the taskbar while docked.
                win32gui.SetWindowLong(
                    hwnd, win32con.GWL_EXSTYLE, _int32(new_ex_style | win32con.WS_EX_TOOLWINDOW)
                )
                self._saved.owner = _set_owner(hwnd, owner_hwnd)
        except (pywintypes.error, OSError) as exc:
            # Typical cause: target runs elevated (UIPI) and we don't.
            self._restore_styles()
            self._saved = None
            raise EmbedError(f"Embedding failed: {exc}. Is the target running elevated?") from exc

        self.mode = mode
        self.hwnd = hwnd
        self.host_hwnd = host_hwnd
        self.fit_to_host()
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        log.info("Embedded window 0x%08X into host 0x%08X (%s mode)", hwnd, host_hwnd, mode)

    def fit_to_host(self) -> None:
        """Make the embedded window exactly cover the host's client area."""
        if not (IS_WINDOWS and self.host_hwnd and self.window_exists() and self._saved):
            return
        left, top, right, bottom = win32gui.GetClientRect(self.host_hwnd)
        width, height = max(1, right - left), max(1, bottom - top)
        x, y = 0, 0
        if self.mode == "dock":
            x, y = win32gui.ClientToScreen(self.host_hwnd, (0, 0))
        try:
            win32gui.SetWindowPos(
                self.hwnd,
                win32con.HWND_TOP,
                x,
                y,
                width,
                height,
                win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE
                | win32con.SWP_FRAMECHANGED
                | win32con.SWP_SHOWWINDOW,
            )
        except pywintypes.error as exc:
            log.debug("fit_to_host failed: %s", exc)

    # ------------------------------------------------------------ diagnostics
    _STYLE_FLAGS = {
        0x40000000: "CHILD", 0x80000000: "POPUP", 0x10000000: "VISIBLE", 0x08000000: "DISABLED",
        0x00C00000: "CAPTION", 0x00040000: "THICKFRAME", 0x02000000: "CLIPCHILDREN", 0x04000000: "CLIPSIBLINGS",
        0x01000000: "MAXIMIZE", 0x20000000: "MINIMIZE", 0x00100000: "VSCROLL", 0x00200000: "HSCROLL",
    }
    _EX_FLAGS = {
        0x00080000: "LAYERED", 0x00000020: "TRANSPARENT", 0x00000080: "TOOLWINDOW", 0x08000000: "NOACTIVATE",
        0x02000000: "COMPOSITED", 0x00200000: "NOREDIRECTIONBITMAP", 0x00040000: "APPWINDOW", 0x00000008: "TOPMOST",
    }

    @staticmethod
    def _flags(value: int, table: dict[int, str]) -> str:
        value &= 0xFFFFFFFF
        return "|".join(name for bit, name in table.items() if value & bit == bit) or "-"

    @staticmethod
    def _dpi(hwnd: int) -> str:
        """DPI awareness of the process that owns ``hwnd`` and the DPI the window runs at."""
        user32 = ctypes.windll.user32
        names = {-1: "invalid", 0: "UNAWARE", 1: "SYSTEM-aware", 2: "PER-MONITOR-aware"}
        try:
            user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
            user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            context = user32.GetWindowDpiAwarenessContext(ctypes.c_void_p(hwnd))
            awareness = names.get(user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(context)), "?")
            dpi = user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
            return f"{awareness}, {dpi} dpi ({round(dpi * 100 / 96)}%)"
        except (AttributeError, OSError):
            return "unknown"

    # Environment variables whose values are safe and useful to show; every other variable is listed
    # by NAME only (values can hold secrets, e.g. a personal access token in ADO_PAT).
    _SAFE_ENV = ("USERNAME", "USERDOMAIN", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "COMPUTERNAME",
                 "SESSIONNAME", "PROCESSOR_ARCHITECTURE", "ProgramData", "HOMEDRIVE", "HOMEPATH", "LOGONSERVER")

    @staticmethod
    def _token_facts(pid: int) -> list[str]:
        """Elevation, integrity level and group membership of a process token (read-only)."""
        import win32api
        import win32security

        try:
            handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        except pywintypes.error as exc:
            return [f"token: not readable ({exc.strerror})"]
        try:
            token = win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY)
            out = []
            elevated = bool(win32security.GetTokenInformation(token, 20))  # TokenElevation
            kind = {1: "default (UAC off or standard user)", 2: "FULL (elevated)", 3: "LIMITED (filtered admin token)"}
            out.append(f"elevated: {elevated}   elevation type: "
                       f"{kind.get(win32security.GetTokenInformation(token, 18), '?')}")  # TokenElevationType
            sid, _ = win32security.GetTokenInformation(token, 25)  # TokenIntegrityLevel
            rid = sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
            levels = {0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium+", 0x3000: "High", 0x4000: "System"}
            out.append(f"integrity level: {levels.get(rid, hex(rid))}")
            groups, admin = [], False
            for gsid, attrs in win32security.GetTokenInformation(token, 2):  # TokenGroups
                try:
                    name, domain, _kind = win32security.LookupAccountSid(None, gsid)
                except pywintypes.error:
                    continue
                enabled = bool(attrs & 0x4) and not attrs & 0x10  # SE_GROUP_ENABLED, not DENY_ONLY
                if name == "Administrators" and enabled:
                    admin = True
                if enabled and not attrs & 0x20:  # skip integrity labels
                    groups.append(f"{domain}\\{name}")
            out.append(f"administrators group enabled in this token: {admin}")
            out.append(f"groups ({len(groups)}): " + "; ".join(groups[:30]))
            return out
        except pywintypes.error as exc:
            return [f"token: not readable ({exc.strerror})"]
        finally:
            win32api.CloseHandle(handle)

    def describe_process(self, pid: int) -> list[str]:
        """How a process was started: account, parent, command line, folder, session, token, environment."""
        _require_windows()
        out = [f"--- process {pid} ---"]
        try:
            proc = psutil.Process(pid)
        except psutil.Error as exc:
            return out + [f"(not available: {exc})"]

        def attempt(label: str, fn: Callable[[], object]) -> None:
            try:
                out.append(f"{label}: {fn()}")
            except (psutil.Error, OSError, pywintypes.error) as exc:
                out.append(f"{label}: (not readable: {type(exc).__name__})")

        attempt("name / exe", lambda: f"{proc.name()}   {proc.exe()}")
        attempt("account", proc.username)
        attempt("started", lambda: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(proc.create_time())))
        attempt("command line", lambda: subprocess.list2cmdline(proc.cmdline()))
        attempt("working folder", proc.cwd)
        attempt("parent", lambda: f"{proc.ppid()} {psutil.Process(proc.ppid()).name()} "
                                  f"({psutil.Process(proc.ppid()).username()})")
        try:
            import win32ts

            out.append(f"session id: {win32ts.ProcessIdToSessionId(pid)}   (this dashboard: "
                       f"{win32ts.ProcessIdToSessionId(os.getpid())})")
        except Exception:
            pass
        try:
            out += self._token_facts(pid)
        except Exception as exc:
            out.append(f"token: (error {exc})")
        try:
            env = proc.environ()
            out.append(f"environment: {len(env)} variables")
            for key in self._SAFE_ENV:
                if key in env:
                    out.append(f"  {key}={env[key]}")
            out.append("  other variable names: " + ", ".join(sorted(k for k in env if k not in self._SAFE_ENV)))
        except (psutil.Error, OSError):
            out.append("environment: (not readable from this account)")
        return out

    def process_facts_report(self, image_name: Optional[str] = None) -> str:
        """Facts for every running process named like the application (and the dashboard), to compare how
        an instance started by the dashboard differs from one you started yourself."""
        _require_windows()
        name = (image_name or (os.path.basename(self.exe_path) if self.exe_path else "")).lower()
        lines = [f"MAS-QA-Bridge process facts - {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"looking for: {name or '(none)'}   started by the dashboard: pid {self.pid or '-'}"
                 f" ({self.run_as_label or 'n/a'})", ""]
        lines += self.describe_process(os.getpid())
        lines.append("(that was the dashboard itself)")
        found = [p for p in psutil.process_iter(["pid", "name"]) if name and (p.info.get("name") or "").lower() == name]
        if not found:
            lines += ["", f"No running process named {name}."]
        for proc in found:
            lines.append("")
            tag = "STARTED BY THE DASHBOARD" if proc.info["pid"] in self.candidate_pids() else "started some other way"
            lines.append(f"##### {tag} #####")
            lines += self.describe_process(proc.info["pid"])
        return "\n".join(lines)

    def diagnostics_report(self, dashboard_hwnd: Optional[int] = None) -> str:
        """A plain-text description of the embedded application's windows, for troubleshooting.

        Records what Windows reports: sizes, DPI awareness, styles and every child window - enough to
        tell a layout/size problem from a re-parenting or scaling problem when a page of the
        application draws incompletely.
        """
        _require_windows()
        out: list[str] = [f"MAS-QA-Bridge embed diagnostics - {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        pids = self.candidate_pids()

        def rect(hwnd: int) -> str:
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                return f"({l},{t}) {r - l}x{b - t}"
            except pywintypes.error:
                return "?"

        def describe(hwnd: int) -> str:
            try:
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                text = win32gui.GetWindowText(hwnd)[:40]
                return (
                    f"0x{hwnd:X} {win32gui.GetClassName(hwnd)!r} {text!r} {rect(hwnd)} "
                    f"vis={bool(win32gui.IsWindowVisible(hwnd))} en={bool(win32gui.IsWindowEnabled(hwnd))} "
                    f"style={self._flags(style, self._STYLE_FLAGS)} ex={self._flags(ex_style, self._EX_FLAGS)}"
                )
            except pywintypes.error:
                return f"0x{hwnd:X} (gone)"

        out += ["", "== Dashboard =="]
        if dashboard_hwnd:
            out.append(f"window: {describe(dashboard_hwnd)}")
            out.append(f"DPI: {self._dpi(dashboard_hwnd)}")
        out.append(f"run as: {run_as.current_account()} elevated={run_as.is_admin()}")

        out += ["", "== Application under test =="]
        out.append(f"exe: {self.exe_path}   pid: {self.pid}   mode: {self.mode}   started as: {self.run_as_label or '?'}")
        out.append(f"elevated: {self.target_elevated}   all pids: {sorted(pids)}")
        for pid in sorted(pids):
            try:
                proc = psutil.Process(pid)
                out.append(f"  process {pid}: {proc.name()} user={proc.username()}")
            except psutil.Error:
                pass
        if not (self.hwnd and win32gui.IsWindow(self.hwnd)):
            out.append("(the application window no longer exists)")
            return "\n".join(out)

        hwnd = self.hwnd
        out += self.describe_process(self.pid) if self.pid else []
        out.append(f"main window: {describe(hwnd)}")
        out.append(f"  DPI: {self._dpi(hwnd)}")
        try:
            out.append(f"  client area: {win32gui.GetClientRect(hwnd)}   parent: 0x{win32gui.GetParent(hwnd):X}"
                       f"   owner: 0x{win32gui.GetWindow(hwnd, win32con.GW_OWNER):X}")
        except pywintypes.error:
            pass
        if self.host_hwnd:
            out.append(f"host frame (the dashboard's application area): {describe(self.host_hwnd)}")
            try:
                hl, ht, hr, hb = win32gui.GetClientRect(self.host_hwnd)
                out.append(f"  host client area: {hr - hl}x{hb - ht}")
            except pywintypes.error:
                pass
        if self._saved is not None:
            l, t, r, b = self._saved.rect
            out.append(f"size before embedding: {r - l}x{b - t}   original style: "
                       f"{self._flags(self._saved.style, self._STYLE_FLAGS)}")

        children: list[int] = []
        try:
            win32gui.EnumChildWindows(hwnd, lambda h, _: children.append(h) or True, None)
        except pywintypes.error:
            pass
        # ---- pages stacked on top of each other: is the one on top empty?
        controls = ("STATIC", "BUTTON", "EDIT", "COMBOBOX", "LISTBOX", "RICHEDIT", "SYSLISTVIEW", "SYSTREEVIEW", "SYSTABCONTROL")
        kids: dict[int, list[int]] = {}
        for h in children:  # EnumChildWindows lists siblings top-most first
            kids.setdefault(win32gui.GetParent(h), []).append(h)

        def has_controls(h: int) -> bool:
            stack = list(kids.get(h, []))
            while stack:
                cur = stack.pop()
                if any(key in win32gui.GetClassName(cur).upper() for key in controls):
                    return True
                stack.extend(kids.get(cur, []))
            return False

        out += ["", "== Stacked pages (top-most first) =="]
        stacks = 0
        for parent, siblings in kids.items():
            big = []
            for h in siblings:
                try:
                    l, t, r, b = win32gui.GetWindowRect(h)
                except pywintypes.error:
                    continue
                if r - l >= 600 and b - t >= 300 and win32gui.IsWindowVisible(h):
                    big.append(h)
            if len(big) >= 3:
                stacks += 1
                out.append(f"in 0x{parent:X}:")
                for position, h in enumerate(big):
                    state = "has controls" if has_controls(h) else "EMPTY - no controls inside"
                    out.append(f"  {position + 1}. 0x{h:X} {rect(h)}  {state}{'   <- on top' if position == 0 else ''}")
        if not stacks:
            out.append("(none found)")

        out += ["", f"== Child windows of the main window ({len(children)}; first 120 shown) =="]

        def depth(h: int) -> int:
            d, cur = 0, h
            while d < 30:
                cur = win32gui.GetParent(cur)
                if not cur or cur == hwnd:
                    return d
                d += 1
            return d

        for h in children[:120]:
            try:
                out.append("  " * (depth(h) + 1) + describe(h))
            except pywintypes.error:
                pass
        if len(children) > 120:
            out.append(f"  ... {len(children) - 120} more")

        others: list[int] = []

        def collect(h: int, _: object) -> bool:
            try:
                if h != hwnd and win32process.GetWindowThreadProcessId(h)[1] in pids:
                    others.append(h)
            except pywintypes.error:
                pass
            return True

        win32gui.EnumWindows(collect, None)
        out += ["", f"== Other top-level windows of the application ({len(others)}) =="]
        for h in others[:60]:
            out.append("  " + describe(h) + f" owner=0x{win32gui.GetWindow(h, win32con.GW_OWNER):X}")
        return "\n".join(out)

    def keep_dashboard_enabled(self) -> bool:
        """Undo the "disabled dashboard" caused by the embedded application's modal dialogs.

        A modal dialog disables its owner's first non-child ancestor (MFC, Win32 ``DialogBox`` and
        ``MessageBox`` do this). With the application embedded as a child, that ancestor is the
        dashboard itself: it stops accepting clicks and beeps. The dialog stays modal for the
        *application* (its own window remains disabled); only the dashboard is re-enabled.
        Returns True when something had to be re-enabled. Only needed in embed (SetParent) mode:
        in dock mode the application is a separate window, so the dashboard is never disabled.
        """
        if not (IS_WINDOWS and self.mode == "reparent" and self._saved and self.host_hwnd):
            return False
        changed = False
        try:
            top = win32gui.GetAncestor(self.host_hwnd, win32con.GA_ROOT)
            for hwnd in (top, self.host_hwnd):
                if hwnd and not win32gui.IsWindowEnabled(hwnd):
                    win32gui.EnableWindow(hwnd, True)
                    changed = True
        except pywintypes.error:
            pass
        return changed

    def set_visible(self, visible: bool) -> None:
        """Hide/show a docked window (e.g. while the host frame is hidden)."""
        if self.window_exists() and self._saved:
            win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE if visible else win32con.SW_HIDE)

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
                if self.mode == "reparent":
                    win32gui.SetParent(saved.hwnd, 0)
                else:
                    _set_owner(saved.hwnd, saved.owner)
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
        denied = False
        for proc in procs:
            try:
                proc.terminate()
            except psutil.AccessDenied:
                denied = True  # another user's / elevated process
            except psutil.Error:
                pass
        if denied and self.process is not None:
            # The handle we got at launch has full access even across users.
            try:
                run_as.terminate_handle(self.process.handle)
            except Exception as exc:
                log.warning("Could not terminate PID %s: %s", self.pid, exc)
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
        self.run_as_label = ""
