# MAS-QA-Bridge

A PyQt6 desktop dashboard for manual QA on Windows. It can:

1. Launch any Windows `.exe` and embed its window inside the dashboard.
2. Record GIF (or MP4) evidence of only the embedded app's area.
3. Publish Pass / Fail / Blocked results, with the evidence attached, to **Azure DevOps Test Plans**.

## Files

| File | Role |
|---|---|
| `main_window.py` | Entry point. The PyQt6 UI: top bar (Browse / Launch), central host `QFrame`, QA sidebar. Connects the modules below. |
| `window_manager.py` | Starts the app with `subprocess.Popen`, finds its main HWND with `EnumWindows` (PID + child processes via `psutil`), removes its borders and embeds it with `SetParent`. |
| `evidence_capture.py` | `EvidenceRecorder`: `mss` capture of the host frame's screen rectangle on a background thread, saved to `temp_evidence/` as a GIF (`imageio`) or MP4 (`cv2`). |
| `ado_test_api.py` | `AdoTestClient`: PAT auth, `create_test_run`, `update_test_result`, `upload_test_run_attachment` / `upload_test_result_attachment`, `complete_test_run`, plus `record_point_outcome`, which chains them for the Pass/Fail buttons. |
| `config.yaml` | ADO, launch and capture settings. |

Keep all five files in the same folder.

## Setup (Windows, Python 3.10+)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

1. Create a PAT in Azure DevOps (*User settings → Personal access tokens*) with the **Test Management: Read & write** scope.
2. Store it in an environment variable rather than in the file:
   `setx ADO_PAT "<your-token>"`. Open a new terminal afterwards.
3. Edit `config.yaml` and set `ado.organization`, `ado.project` and `ado.test_plan_id`. Optionally set `app.default_executable`.

## Run

```powershell
python main_window.py
```

1. **Browse Executable…** → **Launch & Embed**. The app starts, and its window is embedded in the central frame.
2. **Start Recording**, run the test steps in the embedded app, then **Stop & Save GIF**. The file is saved to `temp_evidence/`.
3. Enter the **Test Plan ID** and **Test Point ID**. The point ID is in Test Plans → *Execute* tab → the *ID* column of the test point (not the test case). Add a comment and click **Pass**, **Fail** or **Blocked**.
   The app creates a run for that point, uploads the GIF, sets the outcome, completes the run and shows a link to it. If you are still recording, it first stops the recording and uses the new GIF.

Use **Test ADO Connection** to check the PAT and project settings.

## How the ADO flow works (api-version 7.1)

```
POST  {org}/{project}/_apis/test/runs                           plan + pointIds → run (InProgress)
GET   {org}/{project}/_apis/test/Runs/{runId}/results           → result id for the point
POST  {org}/{project}/_apis/test/Runs/{runId}/Results/{id}/attachments   (7.1-preview.1, base64)
PATCH {org}/{project}/_apis/test/Runs/{runId}/results           outcome + state=Completed
PATCH {org}/{project}/_apis/test/runs/{runId}                   state=Completed
```

If any step fails after the run is created, the run is set to `Aborted`. Set `attachment_target: run` or `both` to attach files to the run as well as, or instead of, the result.

## Known limitations of window embedding

- **Elevation:** a non-elevated dashboard cannot embed an app that runs as Administrator (UIPI). Run both at the same level.
- **UWP / Store apps** (for example the Windows 11 Notepad and Calculator) and some Chromium/Electron apps do not embed reliably. Classic Win32, WinForms and WPF apps work best.
- **Splash screens:** if the first window found is a splash screen, set `app.window_title_contains` to part of the main window's title.
- **Menus:** embedded windows get the `WS_CHILD` style. Some apps then stop drawing their classic menu bar.
- **Evidence:** recording is a screen grab. Anything drawn on top of the frame (tooltips, other windows) is captured too. GIFs get large, so use `save_mp4()` for long sessions. Keep attachments under ADO's 100 MB limit.
- The UI starts on Linux and macOS for layout work, but launching and embedding apps only works on Windows.
