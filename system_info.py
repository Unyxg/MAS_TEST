"""
system_info.py - Environment details for bug reports.

Developers can only reproduce a bug when they know *exactly* what was tested:
the application's version (read from the .exe's version resource), the OS
build, the display setup (DPI scaling breaks a lot of desktop UI) and when /
by whom it was tested.
"""
from __future__ import annotations

import getpass
import locale
import os
import platform
import sys
from datetime import datetime
from typing import Optional

IS_WINDOWS = sys.platform == "win32"


def get_file_version(path: Optional[str]) -> Optional[str]:
    """Product/file version from a Windows executable's version resource."""
    if not (IS_WINDOWS and path and os.path.isfile(path)):
        return None
    try:
        import win32api

        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        numeric = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
        product = None
        try:
            lang, codepage = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")[0]
            product = win32api.GetFileVersionInfo(
                path, f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\ProductVersion"
            )
        except Exception:  # no string table - numeric version is enough
            pass
        if product and product.strip() and product.strip() != numeric:
            return f"{product.strip()} (file {numeric})"
        return numeric
    except Exception:
        return None


def os_description() -> str:
    if IS_WINDOWS:
        ver = sys.getwindowsversion()
        edition = platform.win32_edition() if hasattr(platform, "win32_edition") else ""
        name = "Windows 11" if ver.build >= 22000 else f"Windows {platform.release()}"
        return f"{name} {edition or ''} (build {ver.major}.{ver.minor}.{ver.build}), {platform.machine()}".replace("  ", " ")
    return f"{platform.system()} {platform.release()} ({platform.machine()})"


def collect_environment(
    exe_path: Optional[str] = None,
    app_version: Optional[str] = None,
    screen: Optional[str] = None,
    configuration: Optional[str] = None,
    include_machine_name: bool = True,
) -> dict[str, str]:
    """Ordered key/value pairs shown in the bug's Environment table."""
    env: dict[str, str] = {}
    if exe_path:
        env["Application"] = os.path.basename(exe_path)
        env["Application version"] = app_version or get_file_version(exe_path) or "unknown"
        env["Executable path"] = exe_path
    env["Operating system"] = os_description()
    if screen:
        env["Display"] = screen
    if configuration:
        env["Test configuration"] = configuration
    try:
        env["Locale"] = locale.getlocale()[0] or ""
    except ValueError:
        pass
    if include_machine_name:
        env["Machine"] = platform.node()
    env["Tester"] = getpass.getuser()
    env["Tested at"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return {k: v for k, v in env.items() if v}
