"""
run_as.py - Launching the application under test with a different security context.

Windows rule of thumb: a process started normally inherits the *token* of the
process that started it. So if MAS-QA-Bridge itself runs as Administrator (or
as another user via "Run as different user"), an app launched with
"Current user" runs as that same administrator / user.

To launch *differently* from how MAS-QA-Bridge is running:

* ``launch_elevated``  - ShellExecuteEx(verb="runas"): UAC prompt. On a standard
  account UAC asks for an administrator's credentials, so the app runs as
  *that* admin, elevated ("over-the-shoulder" elevation).
* ``launch_as_user``   - CreateProcessWithLogonW: the same API as the Explorer
  "Run as different user" / ``runas.exe``. No UAC; the app gets that user's
  normal (non-elevated) token.

Embedding rule (UIPI): a non-elevated MAS-QA-Bridge cannot re-parent or move an
elevated window. ``restart_self_as_admin`` relaunches the dashboard elevated
so elevated apps can be embedded.
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

IS_WINDOWS = sys.platform == "win32"
log = logging.getLogger(__name__)

RUN_AS_CURRENT = "current"
RUN_AS_ADMIN = "admin"
RUN_AS_USER = "user"
_CRED_PREFIX = "MAS-QA-Bridge:"


@dataclass
class Credentials:
    username: str  # "user", "DOMAIN\\user" or "user@domain"
    password: str
    domain: str = ""

    @classmethod
    def parse(cls, account: str, password: str) -> "Credentials":
        account = account.strip()
        if "\\" in account:
            domain, user = account.split("\\", 1)
            return cls(user, password, domain)
        # UPN ("user@domain") must be passed with an empty domain.
        return cls(account, password, "" if "@" in account else ".")

    @property
    def account(self) -> str:
        if self.domain and self.domain != ".":
            return f"{self.domain}\\{self.username}"
        return self.username


@dataclass
class LaunchedProcess:
    pid: int
    handle: object  # PyHANDLE with PROCESS_ALL_ACCESS (or a Popen object)
    run_as_label: str


# ----------------------------------------------------------------------------
# Introspection
# ----------------------------------------------------------------------------
def is_admin() -> bool:
    """True when this process runs elevated."""
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def current_account() -> str:
    if IS_WINDOWS:
        try:
            import win32api

            return win32api.GetUserNameEx(2)  # NameSamCompatible: DOMAIN\\user
        except Exception:
            pass
    return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def is_process_elevated(pid: int) -> Optional[bool]:
    """Elevation of another process, or None if it cannot be queried."""
    if not IS_WINDOWS:
        return None
    try:
        import win32api
        import win32con
        import win32security

        handle = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            token = win32security.OpenProcessToken(handle, win32con.TOKEN_QUERY)
            token_elevation = getattr(win32security, "TokenElevation", 20)
            return bool(win32security.GetTokenInformation(token, token_elevation))
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return None


def process_account(pid: int) -> Optional[str]:
    try:
        import psutil

        return psutil.Process(pid).username()
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Launching
# ----------------------------------------------------------------------------
def _command_line(exe_path: str, args: Sequence[str]) -> str:
    return subprocess.list2cmdline([exe_path, *args])


def launch_current(exe_path: str, args: Sequence[str], cwd: Optional[str]) -> LaunchedProcess:
    proc = subprocess.Popen([exe_path, *args], cwd=cwd)
    label = current_account() + (" (elevated)" if is_admin() else "")
    return LaunchedProcess(proc.pid, proc, label)


def launch_elevated(exe_path: str, args: Sequence[str], cwd: Optional[str]) -> LaunchedProcess:
    """Start elevated through UAC (``runas`` verb). Raises if the user declines."""
    if is_admin():
        # Already elevated: children inherit the elevated token, no prompt needed.
        return launch_current(exe_path, args, cwd)
    import win32con
    import win32process
    from win32com.shell import shell, shellcon

    try:
        info = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | shellcon.SEE_MASK_NOASYNC,
            lpVerb="runas",
            lpFile=exe_path,
            lpParameters=subprocess.list2cmdline(list(args)),
            lpDirectory=cwd or "",
            nShow=win32con.SW_SHOWNORMAL,
        )
    except Exception as exc:  # ERROR_CANCELLED (1223) when UAC is declined
        if getattr(exc, "winerror", None) == 1223 or "1223" in str(exc):
            raise PermissionError("Elevation was cancelled at the UAC prompt.") from exc
        raise
    handle = info["hProcess"]
    pid = win32process.GetProcessId(handle)
    return LaunchedProcess(pid, handle, f"{process_account(pid) or 'administrator'} (elevated)")


def launch_as_user(
    exe_path: str, args: Sequence[str], cwd: Optional[str], credentials: Credentials
) -> LaunchedProcess:
    """Start as another Windows account (like "Run as different user")."""
    import win32process

    logon_with_profile = 0x00000001
    startup = win32process.STARTUPINFO()
    try:
        handle, thread, pid, _tid = win32process.CreateProcessWithLogonW(
            credentials.username,
            credentials.domain or None,
            credentials.password,
            logon_with_profile,
            exe_path,
            _command_line(exe_path, args),
            0,
            None,
            cwd,
            startup,
        )
    except Exception as exc:
        code = getattr(exc, "winerror", None)
        messages = {
            1326: "The user name or password is incorrect.",
            1385: "That account is not allowed to log on to this computer (logon type not granted).",
            1331: "That account is disabled.",
            1907: "That account's password must be changed before logging on.",
            267: "The working directory is not accessible to that account.",
            5: "Access denied - that account cannot read the executable.",
        }
        raise PermissionError(messages.get(code, f"Could not start as {credentials.account}: {exc}")) from exc
    thread.Close()
    return LaunchedProcess(pid, handle, credentials.account)


def launch(
    exe_path: str,
    args: Sequence[str] = (),
    cwd: Optional[str] = None,
    run_as: str = RUN_AS_CURRENT,
    credentials: Optional[Credentials] = None,
) -> LaunchedProcess:
    if run_as == RUN_AS_ADMIN:
        return launch_elevated(exe_path, args, cwd)
    if run_as == RUN_AS_USER:
        if credentials is None:
            raise ValueError("Credentials are required to run as a different user.")
        return launch_as_user(exe_path, args, cwd, credentials)
    return launch_current(exe_path, args, cwd)


def handle_exit_code(handle: object) -> Optional[int]:
    """Exit code of a launched process, or None while it is still running."""
    if isinstance(handle, subprocess.Popen):
        return handle.poll()
    try:
        import win32process

        code = win32process.GetExitCodeProcess(handle)
        return None if code == 259 else code  # STILL_ACTIVE
    except Exception:
        return None


def terminate_handle(handle: object) -> None:
    if isinstance(handle, subprocess.Popen):
        handle.kill()
        return
    import win32api

    win32api.TerminateProcess(handle, 1)


def restart_self_as_admin(extra_args: Sequence[str] = ()) -> bool:
    """Relaunch MAS-QA-Bridge elevated. Returns True if the new instance started."""
    if not IS_WINDOWS:
        return False
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, [*sys.argv[1:], *extra_args]
    else:
        exe, params = sys.executable, [os.path.abspath(sys.argv[0]), *sys.argv[1:], *extra_args]
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, subprocess.list2cmdline(params), os.getcwd(), 1
    )
    return rc > 32  # ShellExecute returns > 32 on success


# ----------------------------------------------------------------------------
# Windows Credential Manager (optional "remember password")
# ----------------------------------------------------------------------------
def save_password(account: str, password: str) -> None:
    import win32cred

    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": _CRED_PREFIX + account.lower(),
            "UserName": account,
            "CredentialBlob": password,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "Comment": "MAS-QA-Bridge: run application under test as this account",
        },
        0,
    )


def load_password(account: str) -> Optional[str]:
    if not IS_WINDOWS or not account:
        return None
    try:
        import win32cred

        cred = win32cred.CredRead(_CRED_PREFIX + account.lower(), win32cred.CRED_TYPE_GENERIC)
    except Exception:
        return None
    blob = cred.get("CredentialBlob") or b""
    return blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)


def forget_password(account: str) -> None:
    try:
        import win32cred

        win32cred.CredDelete(_CRED_PREFIX + account.lower(), win32cred.CRED_TYPE_GENERIC)
    except Exception:
        pass
