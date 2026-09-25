"""
self_test.py - ``MAS-QA-Bridge.exe --self-test``: verify an installation without opening a window.

Checks that every packaged dependency loads and works (screen capture, GIF/MP4
encoding, the Azure DevOps client against the offline demo data, Windows APIs)
and writes the results to ``self_test.log`` next to the executable.
Exit code 0 = all good.
"""
from __future__ import annotations

import platform
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable


def run_self_test(base_dir: Path, config_path: Path) -> int:
    lines: list[str] = []
    failures = 0

    def check(name: str, fn: Callable[[], str]) -> None:
        nonlocal failures
        try:
            lines.append(f"PASS  {name}: {fn()}")
        except Exception as exc:
            failures += 1
            lines.append(f"FAIL  {name}: {exc}\n{traceback.format_exc()}")

    lines.append(f"MAS-QA-Bridge self-test · Python {platform.python_version()} · {platform.platform()}")
    lines.append(f"frozen={getattr(sys, 'frozen', False)} base_dir={base_dir}")

    def config() -> str:
        import yaml

        if not config_path.exists():
            return f"{config_path} not found (defaults will be used)"
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return f"{config_path.name} sections: {', '.join(data)}"

    def qt() -> str:
        from PyQt6.QtCore import QT_VERSION_STR

        return f"Qt {QT_VERSION_STR}"

    def capture() -> str:
        import mss
        import numpy as np

        with mss.mss() as sct:
            mon = sct.monitors[1]
            shot = np.asarray(sct.grab({"left": mon["left"], "top": mon["top"], "width": 200, "height": 120}))
        return f"grabbed {shot.shape[1]}x{shot.shape[0]} from a {mon['width']}x{mon['height']} monitor"

    def encode() -> str:
        import numpy as np

        from evidence_capture import EvidenceRecorder

        out = Path(tempfile.mkdtemp(prefix="masqa_"))
        rec = EvidenceRecorder(lambda: None, output_dir=out, fps=5, max_width=320)
        for i in range(10):
            frame = np.zeros((180, 320, 4), np.uint8)
            frame[:, i * 30 : i * 30 + 30] = (0, 200, 0, 255)
            rec._store(frame)
        rec._started_at, rec._stopped_at = time.monotonic() - 2, time.monotonic()
        gif, mp4 = rec.save_gif("t"), rec.save_mp4("t")
        return f"GIF {gif.stat().st_size} B, MP4 {mp4.stat().st_size} B"

    def ado() -> str:
        from bug_report import BugReport
        from demo_data import DemoAdoClient

        client = DemoAdoClient(latency=0)
        session = client.load_session(client.list_points(101, 203)[0])
        for i in range(len(session.steps)):
            session.set_step_outcome(i, "Passed")
        session.set_step_outcome(3, "Failed", "self-test failure")
        report = client.record_execution(session, "Failed")
        bug = client.create_bug(BugReport.from_session(session, 3, {"OS": platform.platform()}, "1.0"))
        return f"run {report['run_id']} with {len(session.steps)} step results, bug {bug['id']}"

    def windows() -> str:
        if sys.platform != "win32":
            return "skipped (not Windows)"
        import run_as
        from system_info import get_file_version

        notepad = r"C:\Windows\System32\notepad.exe"
        return (
            f"account {run_as.current_account()}, elevated={run_as.is_admin()}, "
            f"notepad version {get_file_version(notepad)}"
        )

    for name, fn in [
        ("config", config),
        ("Qt", qt),
        ("screen capture (mss)", capture),
        ("GIF/MP4 encoding", encode),
        ("Azure DevOps client (demo data)", ado),
        ("Windows APIs", windows),
    ]:
        check(name, fn)

    lines.append("RESULT: " + ("OK" if not failures else f"{failures} check(s) failed"))
    text = "\n".join(lines)
    try:
        (base_dir / "self_test.log").write_text(text, encoding="utf-8")
    except OSError:
        pass
    if sys.stdout:
        print(text)
    return 1 if failures else 0
