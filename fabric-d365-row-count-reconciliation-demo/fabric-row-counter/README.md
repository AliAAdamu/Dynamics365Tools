# Fabric / Synapse Link Row Counter

Small Python CLI that connects to a Microsoft Fabric **Warehouse**, **Lakehouse SQL endpoint**, or an **Azure Synapse Analytics SQL Serverless endpoint** and reports row counts for one or more tables.

## How it works
- Connects to **Fabric Link** (Fabric Warehouse / Lakehouse SQL endpoint) or **Synapse Link** (Synapse SQL Serverless) via the SQL (TDS) endpoint using `pyodbc` + ODBC Driver 18.
- Authenticates with Azure AD (interactive browser popup or service principal).
- Counts rows per table in **Fabric or Synapse** and per table in **Dynamics 365 F&O** (via the `FabricHelperService` custom service).
- Computes the delta and assigns a status:
  - **Match** — Fabric == D365
  - **Drift** — D365 has more rows (Fabric is missing / lagging)
  - **Anomaly** — Fabric has more rows than the source
  - **N/A** — table doesn't exist in D365 (typically Dataverse-only tables)
  - **Error** — count could not be retrieved
- Excludes soft-deleted rows from Fabric (`IsDelete = 1`) when the column is present.
- Reports the latest `SinkModifiedOn` per table (when the column is present) so you can see how fresh the mirrored data is.
- Retrieves per-table metadata from both sides:
  - **Fabric RowVersion** — `MAX(SYSROWVERSION)` from the Fabric table.
  - **Fabric Last Modified** — `MAX(MODIFIEDDATETIME)` from the Fabric table when the column is present. If not, an estimate is provided and flagged with `(est.)`.
  - **D365 RowVersion** — `MAX(SYSROWVERSION)` from D365, the database rowversion stamp of the last changed record. `0` means the table is empty or the column is unavailable.
  - **D365 Last Modified** — `MAX(MODIFIEDDATETIME)` from D365 when the column is enabled. If not, an estimate is provided and flagged with `(est.)`.
  - **Running Latency** — `D365 Last Modified − Source Last Modified` (or `D365 Last Modified − SinkModifiedOn` when `SinkModifiedOn` is more recent than `MODIFIEDDATETIME` but still behind D365). Rendered as `3d 4h`, `45m 12s`, `0s` (current or ahead), or `N/A` when timestamps are unavailable. **This value is most meaningful when Status is Drift or Anomaly** — it shows how far the mirror lags behind the source. For Match rows the mirror is fully in sync so a non-zero value simply reflects normal replication delay that hasn’t yet produced a row-count gap.
- Generates a timestamped **HTML report** (sortable / filterable grid) and a matching **CSV** on every run.
- Two counting modes (controlled by `COUNT_MODE` in `.env`):
  - **exact** (default) — runs `SELECT COUNT_BIG(*)` per table. Required for Fabric Warehouse and **mandatory** for Synapse SQL Serverless (`sys.partitions` is not available on serverless endpoints).
  - **fast** — reads `sys.partitions` metadata. Instant on classic SQL Server / Azure SQL DB, but returns 0 on Fabric Warehouse and **must not be used** with Synapse SQL Serverless.

## D365 endpoint — V1 vs V2
The script POSTs to one of two operations on the same `FabricHelperService`:

| Version | Operation | Backend | When to use |
| ------- | --------- | ------- | ----------- |
| **v1** ⭐ **default** | `getTableRecordCount`   | Direct SQL `COUNT(*)` against the AxDB | **Preferred** — fastest, no X++ overhead. Sufficient for normal reconciliation. |
| **v2** (edge-case fallback) | `getTableRecordCountV2` | X++ `select count(RecId)` — same code path Fabric Link uses | Slower. Use only when V1 shows a persistent unexplained **Anomaly** and you need to rule out orphan-row noise (rows belonging to a removed `DataAreaId`). |

Select the version with `D365_SERVICE_VERSION=v1` or `v2` in `.env` (default `v1`).

URL format:
```
<D365_URI>/api/services/FabricHelperServiceGroup/FabricHelperService/getTableRecordCount
<D365_URI>/api/services/FabricHelperServiceGroup/FabricHelperService/getTableRecordCountV2
```
Body:
```json
{ "_request": { "TableName": "<NAME>", "RequestId": "<id>" } }
```
> `FabricLastSysRowVersion` (optional `int64`) can be added to the request body when the Fabric table has no `MODIFIEDDATETIME` column. D365 uses it to resolve the modification time of the record last synced to Fabric.

## Table name mapping
The D365 service expects F&O table names (e.g. `CUSTTABLE`). By default the script
strips the schema and uppercases the Fabric table name (`dbo.systemuser` -> `SYSTEMUSER`).
Override per-table with `TABLE_NAME_MAP` in `.env`:

```
TABLE_NAME_MAP=dbo.systemuser=USERINFO,dbo.bot=BOTTABLE
```

## Prerequisites
1. **ODBC Driver 18 for SQL Server** — https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server
2. Python 3.10+
3. **Fabric Link** — your Fabric Warehouse / Lakehouse **SQL connection string** (PPAC → Warehouse → Settings → SQL connection string), e.g. `xxxxxx.datawarehouse.fabric.microsoft.com`.
   **— or —**
   **Synapse Link** — your Synapse Analytics workspace **SQL Serverless endpoint**, e.g. `myworkspace-ondemand.sql.azuresynapse.net`, and the lake database name.
4. The signed-in account (or service principal) must have at least **read** access on the workspace / endpoint.

## Setup
```powershell
cd fabric-row-counter
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# edit .env with your endpoint, database, and tables
```

Key environment variables (set in `.env`):

| Variable | Required for | Description |
| -------- | ------------ | ----------- |
| `SOURCE_TYPE` | all | `fabric` (default) or `synapse` |
| `FABRIC_SQL_ENDPOINT` | Fabric Link | e.g. `xxxxxx.datawarehouse.fabric.microsoft.com` |
| `FABRIC_DATABASE` | Fabric Link | Warehouse / database name |
| `SYNAPSE_SQL_ENDPOINT` | Synapse Link | e.g. `myworkspace-ondemand.sql.azuresynapse.net` |
| `SYNAPSE_DATABASE` | Synapse Link | Lake database name |
| `AUTH_MODE` / `SYNAPSE_AUTH_MODE` | all | `interactive` (default), `cli`, or `serviceprincipal` |
| `D365_URI` | D365 comparison | Base URL of the D365 F&O environment |
| `COUNT_MODE` | Fabric only | `exact` (default) or `fast` — Synapse always uses `exact` |

## Run
```powershell
python count_rows.py
```

Sample output:
```
Fabric Table     D365 Table  Fabric Rows  Fabric RowVersion  Fabric Last Modified  D365 Rows  D365 RowVersion  D365 Last Modified    Latency    Delta  Status  Last SinkModifiedOn
---------------  ----------  -----------  -----------------  --------------------  ---------  ---------------  --------------------  ---------  -----  ------  -------------------
dbo.bot          BOT                  40         9876540001  2026-04-29 07:58:00          40       9876540000  2026-04-29 08:00:00   2m 0s          0  Match   2026-04-29 07:57:45
dbo.systemuser   SYSTEMUSER          972         9876498000  2026-04-27 18:00:00       1,015       9876501234  2026-04-28 16:30:00   22h 30m      +43  Drift   2026-04-28 08:00:00
dbo.incident     INCIDENT              0                  0  —                             0                0  —                     N/A            0  Match   —

Summary: 2 Match, 1 Drift, 0 Anomaly, 0 Error (total 3 table(s))
```

Exit code is always `0` — Drift / Anomaly / N/A are reported as data, not failures. The only non-zero exit is for unrecoverable errors (bad config, no Fabric connection, etc.).

## Outputs
On every run two timestamped files are written next to the script:
- `report_YYYYMMDD_HHMMSS.html` — sortable / filterable grid with status badges and summary chips. Columns: **Fabric Table**, **D365 Table**, **Fabric Rows**, **Fabric RowVersion**, **Fabric Last Modified**, **D365 Rows**, **D365 RowVersion**, **D365 Last Modified** (each `(est.)` flagged when approximated), **Running Latency**, **Delta**, **Status**, **Last SinkModifiedOn**. Auto-opens in the default browser unless `HTML_OPEN=false`.
- `report_YYYYMMDD_HHMMSS.csv` — same data, one row per table, with columns: `Fabric Table`, `D365 Table`, `Fabric Rows`, `Fabric RowVersion`, `Fabric Last Modified`, `Fabric Mod Estimated`, `D365 Rows`, `D365 RowVersion`, `D365 Last Modified`, `D365 Estimated`, `Latency`, `Delta`, `Status`, `Last SinkModifiedOn`. Suitable for Excel / further processing.

Override the base names with `HTML_REPORT` and `CSV_REPORT` in `.env`. Leave either empty to skip that format.

## Notes
- Leave `FABRIC_TABLES` empty to count **every** user table in the database.
- For unattended/CI use, switch `AUTH_MODE=serviceprincipal` and grant the SP access to the workspace.
- For Lakehouse data, point at the Lakehouse's **SQL analytics endpoint** (read-only) — same code path.- For **Synapse Link**, set `SOURCE_TYPE=synapse` and provide `SYNAPSE_SQL_ENDPOINT` / `SYNAPSE_DATABASE`. The serverless endpoint format is `<workspace>-ondemand.sql.azuresynapse.net`. Only `COUNT_BIG(*)` (exact mode) is supported — `sys.partitions` is not available on SQL Serverless.- For KQL databases or Power BI semantic models, this code won't apply — let me know and I'll add a variant.

