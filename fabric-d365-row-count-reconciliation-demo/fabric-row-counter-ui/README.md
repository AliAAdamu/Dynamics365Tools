# Fabric Row Counter — Streamlit UI

A small web app that wraps the original `fabric-row-counter` CLI in a
user-friendly form. No code from the CLI is duplicated — `count_rows.py`
in `..\fabric-row-counter` is imported as a library, so any improvement
to the CLI is automatically picked up here.

## Features
- Form-based connection settings (Fabric endpoint, database, D365 URI, auth, etc.)
- Choose D365 service version (**v1** Direct SQL or **v2** X++ / Fabric Link)
- Live progress bar while counting tables
- Sortable / filterable results grid showing all comparison columns:
  - **Fabric Rows** / **D365 Rows** — raw counts from each side
  - **Fabric RowVersion** — `MAX(SYSROWVERSION)` from the Fabric table
  - **Fabric Last Modified** — `MAX(MODIFIEDDATETIME)` from Fabric when the column is present; otherwise an estimate is provided and marked `(est.)`
  - **D365 RowVersion** — `MAX(SYSROWVERSION)` from D365 (0 = empty / unavailable)
  - **D365 Last Modified** — last modification timestamp from D365; marked `(est.)` when not directly available
  - **Latency** — `D365 Last Modified − Fabric Last Modified`; how far behind the Fabric mirror is (e.g. `3d 4h`, `45m 12s`, `0s`, `N/A`)
  - **Delta** — signed row-count difference (D365 − Fabric)
  - **Status** badge — **Match** / **Drift** / **Anomaly** / **N/A** / **Error** with color coding
  - **Last SinkModifiedOn** — most recent Fabric mirror timestamp (for reference)
- App version displayed in the sidebar and page subtitle
- Summary chips per status
- One-click **Download CSV** and **Download HTML report** buttons (timestamped)

## Setup
```powershell
cd fabric-row-counter-ui
.\install.ps1
```
The original `fabric-row-counter` folder must sit next to this one (sibling).

## Run
```powershell
.\run.ps1
```
Streamlit opens automatically in your default browser at
http://localhost:8501. Fill in the sidebar, click **Run comparison**.

## Notes
- The first run authenticates twice via browser popup (Fabric, then D365).
  Subsequent runs in the same session reuse the cached token.
- For unattended/CI use, switch the auth mode to **serviceprincipal** and
  fill in the **Service principal** expander.
- Pre-fill defaults via environment variables (same names as the CLI's
  `.env`: `FABRIC_SQL_ENDPOINT`, `FABRIC_DATABASE`, `D365_URI`, etc.).
