# D365 Data Replication Reconciliation Tool — Streamlit UI

A small web app that wraps the original `fabric-row-counter` CLI in a
user-friendly form. No code from the CLI is duplicated — `count_rows.py`
in `..\ fabric-row-counter` is imported as a library, so any improvement
to the CLI is automatically picked up here.

Supports both **Fabric Link** (Fabric Warehouse / Lakehouse SQL endpoint)
and **Synapse Link** (Azure Synapse Analytics SQL Serverless endpoint) as
the analytical data source — customers who have one or both can choose at
run time.
## Features
- **Data source selector** — choose **Fabric Link**, **Synapse Link**, or **Both** from the sidebar radio button. When *Both* is selected results appear in two tabs, one per source.
- Form-based connection settings for each source (SQL endpoint, database, auth, etc.)
- D365 connection settings (URI, auth, service version)
- Choose D365 service version (**v1** Direct SQL or **v2** X++ / Fabric Link)
- Live progress bar while counting tables
- Sortable / filterable results grid showing all comparison columns:
  - **Source Rows** / **D365 Rows** — raw counts from each side (column header shows *Fabric* or *Synapse* based on the selected source)
  - **Source RowVersion** — `MAX(SYSROWVERSION)` from the source table (Fabric or Synapse)
  - **Source Last Modified** — `MAX(MODIFIEDDATETIME)` from the source table when the column is present; otherwise an estimate is provided and marked `(est.)`
  - **D365 RowVersion** — `MAX(SYSROWVERSION)` from D365 (0 = empty / unavailable)
  - **D365 Last Modified** — last modification timestamp from D365; marked `(est.)` when not directly available
  - **Running Latency** — `D365 Last Modified − Source Last Modified`; how far behind the source mirror is (e.g. `3d 4h`, `45m 12s`, `0s`, `N/A`). When `SinkModifiedOn` is more recent than the source `MODIFIEDDATETIME` but still behind D365, it is used as the reference instead. **Interpret this column only when Status is Drift or Anomaly** — for Match rows the mirror is fully in sync and any non-zero value is normal replication delay that has not yet produced a row-count gap.
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
- The first run authenticates twice via browser popup (Fabric or Synapse, then D365).
  Subsequent runs in the same session reuse the cached token.
- For unattended/CI use, switch the auth mode to **serviceprincipal** and
  fill in the **Service principal** expander. Synapse Link can share the
  same SP credentials or use its own (separate fields appear when Synapse
  is selected).
- Pre-fill defaults via environment variables (same names as the CLI's
  `.env`): `FABRIC_SQL_ENDPOINT`, `FABRIC_DATABASE`, `SYNAPSE_SQL_ENDPOINT`,
  `SYNAPSE_DATABASE`, `D365_URI`, etc.
- **Synapse SQL Serverless** only supports `exact` count mode (`COUNT_BIG(*)`)
  — `sys.partitions` metadata is not available on serverless endpoints.
  The UI enforces this automatically.
