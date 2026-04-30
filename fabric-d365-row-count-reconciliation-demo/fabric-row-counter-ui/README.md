# Fabric Row Counter — Streamlit UI

A small web app that wraps the original `fabric-row-counter` CLI in a
user-friendly form. No code from the CLI is duplicated — `count_rows.py`
in `..\fabric-row-counter` is imported as a library, so any improvement
to the CLI is automatically picked up here.

## Features
- Form-based connection settings (Fabric endpoint, database, D365 URI, auth, etc.)
- Choose D365 service version (**v1** Direct SQL or **v2** X++ / Fabric Link)
- Live progress bar while counting tables
- Sortable / filterable results grid with colored Match / Drift / Anomaly / N/A / Error badges
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
