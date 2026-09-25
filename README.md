# MAS-QA-Bridge

A PyQt6 desktop workbench for **manual testing of Windows applications with Azure DevOps Test Plans**. You can:

- browse **Plan → Suite → Test point** from Azure DevOps,
- run the test case **step by step**, marking each step Pass/Fail next to the application under test, which is **embedded in the same window**,
- capture **screenshots per step** and **GIF/MP4 recordings** of only the application's area,
- **publish** the result with step outcomes and attached evidence. On failure, it can also **file a linked Bug** with the repro steps filled in.

![Test runner](docs/ui_runner.png)

*After publishing: the run and bug links appear and the point's status updates in the explorer.*

![Published](docs/ui_published.png)

## Quick look (no Azure DevOps needed)

```powershell
pip install -r requirements.txt
python main_window.py --demo
```

Demo mode uses sample ADO data (a plan, suites, test cases with shared steps) and a sample "Contoso Orders" app. Everything except launching a real `.exe` works, including publishing and bug creation, which are simulated. Demo mode runs on Linux and macOS as well.

## Setup (Windows, Python 3.10+)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
setx ADO_PAT "<personal-access-token>"      # then open a new terminal
python main_window.py
```

- The PAT needs these scopes: **Test Management: Read & write** and **Work Items: Read & write**. Work Items is used to read test steps and to create bugs.
- In `config.yaml`, set `ado.organization`, `ado.project` and, optionally, `ado.test_plan_id` (the plan to preselect) and `app.default_executable`.

## Workflow

1. **Browse… → ▶ Launch.** The application's window is embedded in the centre panel. *Pop out* returns it to the desktop, and *Re-embed* brings it back.
2. In the **Test Explorer**, pick a plan and a suite, then click a test point. The test case's steps load into the **Test Runner**. Shared steps are expanded.
3. For each step, click **✓** or **✗** (or press F5/F6). When you fail a step, you can type the actual result. Take a screenshot for the current step with **📷** or F7, and start or stop a recording with **● Record** or F9.
4. Check the **Result** panel. The outcome is worked out from the steps: any failed step makes it Failed, and all passed makes it Passed. You can override it with Blocked or N/A. Optionally check **Create bug**, then click **Publish** (Ctrl+Enter).

Publishing does the following:

```
POST  _apis/test/runs                                    run for the test point
GET   _apis/test/Runs/{run}/results                      result id
POST  _apis/test/Runs/{run}/Results/{id}/attachments     evidence (?iterationId&actionPath for step screenshots)
PATCH _apis/test/Runs/{run}/results                      outcome + iterationDetails.actionResults (per-step)
PATCH _apis/test/runs/{run}                              Completed
POST  _apis/wit/workitems/$Bug                           optional: bug with repro steps, evidence, "Tested By" link
```

If a step fails before the run is completed, the run is set to **Aborted**, so no runs are left "In progress".

## Files

| File | Role |
|---|---|
| `main_window.py` | Entry point. Builds the window and connects all the pieces. Slow work runs on background threads. |
| `widgets.py` | UI parts: Test Explorer, Step Runner (step cards), Evidence panel, Result panel, host frame. |
| `theme.py` | Dark theme stylesheet and colours for each outcome. |
| `window_manager.py` | Launches the app, finds its window (`EnumWindows` + `psutil` process tree) and embeds it using either **reparent** (`SetParent`) or **dock** mode. |
| `evidence_capture.py` | `mss` capture of the host area: PNG screenshots, GIF (`imageio`) or MP4 (`cv2`). |
| `ado_test_api.py` | Azure DevOps REST client (7.1): browsing test plans, step parsing, runs/results/attachments, bugs. |
| `qa_session.py` | Data model of one test execution: steps, their outcomes, evidence and timing. |
| `demo_data.py` | `--demo`: sample ADO data plus the sample app. |
| `tests/` | `pytest` tests that run offline against the demo data. |

## Embedding modes (header drop-down)

| Mode | How | Best for |
|---|---|---|
| **Embed (SetParent)** | The window becomes a real child of the dashboard. | Classic Win32, WinForms, Delphi and VB6 apps. |
| **Dock (overlay)** | A borderless window, *owned* by the dashboard, positioned exactly over the centre panel. | Apps that misbehave when re-parented: Chromium/Electron, some WPF, apps that lose their menu bar. |

Microsoft doesn't recommend cross-process `SetParent`: the two apps' input queues get linked, so if one freezes the other can too. Dock mode avoids this, so try it if an app behaves oddly when embedded.

## Known limitations

- A dashboard that isn't running as Administrator cannot embed an elevated app (UIPI). Run both at the same privilege level.
- UWP/Store apps (for example the Windows 11 Calculator) cannot be embedded.
- Recording captures the screen, so anything covering the application area is recorded too.
- Step-level attachments use the `iterationId`/`actionPath` query parameters. If a server rejects them, the file is attached to the result instead and a warning is logged.
