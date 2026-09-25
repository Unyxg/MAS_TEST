"""
windows_smoke.py - End-to-end smoke test on a real Windows desktop (run in CI).

    python tests/windows_smoke.py

Launches a classic Win32 app, embeds it in both modes, captures evidence, and
exercises "run as administrator" and "run as different user" (the account comes
from SMOKE_USER / SMOKE_PASS). Prints PASS/FAIL per check, writes screenshots
to SMOKE_OUT and exits non-zero if anything failed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mss  # noqa: E402
import psutil  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from mss import tools as mss_tools  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import main_window as mw  # noqa: E402
import run_as  # noqa: E402
from demo_data import DemoAdoClient  # noqa: E402
from system_info import get_version_info  # noqa: E402

OUT = Path(os.environ.get("SMOKE_OUT", "smoke_out")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
TARGET = r"C:\Windows\System32\notepad.exe"
results: list[dict] = []


def check(name: str, ok: object, detail: str = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)
    return bool(ok)


def pump(app: QApplication, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def desktop_png(name: str) -> None:
    with (getattr(mss, "MSS", None) or mss.mss)() as sct:
        shot = sct.grab(sct.monitors[1])
        mss_tools.to_png(shot.rgb, shot.size, output=str(OUT / f"{name}.png"))


def section(title: str):
    def wrap(fn):
        def run(*args):
            print(f"\n=== {title} ===", flush=True)
            try:
                fn(*args)
            except Exception as exc:
                check(f"{title}: no exception", False, f"{exc}\n{traceback.format_exc()}")
        return run
    return wrap


def main() -> int:
    app = QApplication(sys.argv)
    cfg = mw.load_config()
    cfg.setdefault("evidence", {})["output_dir"] = str(OUT / "evidence")
    window = mw.MainWindow(cfg, ado_client=DemoAdoClient(latency=0), demo=False)
    window._show_error = lambda message: print("UI ERROR:", message, flush=True)  # never block on a dialog
    window.resize(1500, 900)
    window.move(20, 20)
    window.show()
    pump(app, 1.5)
    wm = window.window_manager
    host = window.host_frame.native_handle()

    product, numeric = get_version_info(TARGET)
    check("read .exe version", numeric, f"product={product} file={numeric}")
    check("dashboard account", True, f"{run_as.current_account()} elevated={run_as.is_admin()}")

    @section("current user: reparent + capture + dock")
    def current_user() -> None:
        pid = wm.launch(TARGET)
        hwnd = wm.find_window(timeout=30)
        check("find window", hwnd, f"'{win32gui.GetWindowText(hwnd)}' pid={pid}")
        window._embed_window(hwnd)
        pump(app, 1.0)
        check("reparent: parent is host frame", win32gui.GetParent(hwnd) == host)
        hl, ht, hr, hb = win32gui.GetClientRect(host)
        wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
        check("reparent: fills host", (wr - wl, wb - wt) == (hr - hl, hb - ht), f"{wr - wl}x{wb - wt} vs {hr - hl}x{hb - ht}")
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        check("reparent: caption removed", not style & win32con.WS_CAPTION)
        desktop_png("1_reparent")

        window._refresh_capture_region()
        shot = window.recorder.screenshot("embedded_screenshot")
        check("screenshot of host area", shot.stat().st_size > 1000, f"{shot.name} {shot.stat().st_size} B")
        window.recorder.start()
        pump(app, 2.0)
        gif = window.recorder.stop_and_save_gif("embedded_recording")
        check("GIF recording of host area", gif.stat().st_size > 1000, f"{window.recorder.frame_count} frames")

        window.mode_combo.setCurrentIndex(1)  # Dock (overlay) -> re-embeds
        pump(app, 1.0)
        owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
        check("dock: owned by dashboard", owner == int(window.winId()), f"owner=0x{owner:X}")
        check("dock: top-level (not a child)", win32gui.GetParent(hwnd) in (0, owner))
        x, y = win32gui.ClientToScreen(host, (0, 0))
        wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
        check("dock: positioned over host", (wl, wt) == (x, y), f"{(wl, wt)} vs {(x, y)}")
        window.move(window.x() + 60, window.y() + 40)
        pump(app, 0.8)
        x, y = win32gui.ClientToScreen(host, (0, 0))
        wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
        check("dock: follows dashboard move", (wl, wt) == (x, y), f"{(wl, wt)} vs {(x, y)}")
        desktop_png("2_dock")

        wm.release()
        pump(app, 0.5)
        check("release: back on desktop", win32gui.GetParent(hwnd) == 0 and win32gui.GetWindow(hwnd, win32con.GW_OWNER) == 0)
        check("release: caption restored", win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & win32con.WS_CAPTION)
        window.mode_combo.setCurrentIndex(0)
        wm.terminate()
        pump(app, 1.0)
        check("terminate", not psutil.pid_exists(pid))

    @section("run as administrator (ShellExecuteEx runas)")
    def administrator() -> None:
        real_is_admin = run_as.is_admin
        run_as.is_admin = lambda: False  # force the UAC code path (UAC is off on CI runners)
        try:
            launched = run_as.launch_elevated(TARGET, [], None)
        finally:
            run_as.is_admin = real_is_admin
        check("elevated launch returns a PID", launched.pid, launched.run_as_label)
        time.sleep(1.5)
        check("process is elevated", run_as.is_process_elevated(launched.pid) is True)
        run_as.terminate_handle(launched.handle)
        time.sleep(0.5)
        check("terminate elevated process via handle", run_as.handle_exit_code(launched.handle) is not None)

    @section("run as different user (CreateProcessWithLogonW)")
    def other_user() -> None:
        user, password = os.environ.get("SMOKE_USER"), os.environ.get("SMOKE_PASS")
        if not (user and password):
            check("different user: SMOKE_USER/SMOKE_PASS set", False, "skipped")
            return
        creds = run_as.Credentials.parse(f".\\{user}", password)
        pid = wm.launch(TARGET, run_as_mode=run_as.RUN_AS_USER, credentials=creds)
        check("launch as other user", pid, wm.run_as_label)
        hwnd = wm.find_window(timeout=45)
        account = run_as.process_account(pid) or ""
        check("process runs as that account", account.lower().endswith(user.lower()), account)
        window._embed_window(hwnd)
        pump(app, 1.0)
        check("other user's window embedded", win32gui.GetParent(hwnd) == host)
        desktop_png("3_other_user")
        wm.terminate()
        pump(app, 1.0)
        check("terminate other user's process", not psutil.pid_exists(pid))

    @section("wrong password is reported clearly")
    def wrong_password() -> None:
        user = os.environ.get("SMOKE_USER") or "nobody"
        try:
            run_as.launch_as_user(TARGET, [], None, run_as.Credentials.parse(f".\\{user}", "wrong-password"))
            check("wrong password rejected", False)
        except PermissionError as exc:
            check("wrong password rejected", "incorrect" in str(exc), str(exc))

    current_user()
    administrator()
    other_user()
    wrong_password()

    window._force_close = True
    window.close()
    pump(app, 0.5)
    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
