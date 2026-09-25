"""
app.py - Entry point of the packaged MAS-QA-Bridge.exe.

Wraps start-up so that *any* failure (including import errors) is written to
``mas_qa_bridge_crash.log`` next to the executable instead of disappearing -
and, except in --self-test mode, is shown in a message box.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path


def _base_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def run() -> int:
    try:
        from main_window import main

        return main()
    except SystemExit as exc:
        return int(exc.code or 0)
    except BaseException:
        details = traceback.format_exc()
        log = _base_dir() / "mas_qa_bridge_crash.log"
        try:
            log.write_text(details, encoding="utf-8")
        except OSError:
            pass
        if "--self-test" not in sys.argv and sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, f"MAS-QA-Bridge could not start.\n\n{details[-1500:]}\n\nDetails: {log}", "MAS-QA-Bridge", 0x10
            )
        elif sys.stderr:
            print(details, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
