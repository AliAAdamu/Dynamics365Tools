"""
Fabric vs Dynamics 365 row-count comparator.

1. Reads row counts from a Fabric Warehouse / Lakehouse SQL endpoint.
2. Calls a Dynamics 365 F&O custom service (FabricHelperService) to get row
   counts for the same tables in the source D365 environment.
3. Computes a delta and assigns a status:
     - Match    : Fabric == D365
     - Drift    : D365 > Fabric  (Fabric is missing rows / lagging)
     - Anomaly  : Fabric > D365  (Fabric has more than the source)
     - Error    : a row count could not be retrieved

Auth:
  - Fabric: interactive browser popup, Azure CLI, or service principal.
  - D365  : interactive browser popup or service principal.
"""

from __future__ import annotations

import csv
import html
import os
import struct
import sys
import time
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone

import pyodbc
import requests
from azure.identity import (
    AzureCliCredential,
    ClientSecretCredential,
    InteractiveBrowserCredential,
)
from dotenv import load_dotenv

SQL_COPT_SS_ACCESS_TOKEN = 1256
FABRIC_SCOPE = "https://database.windows.net/.default"


@dataclass
class Config:
    # Fabric
    endpoint: str
    database: str
    tables: list[str]
    auth_mode: str
    tenant_id: str | None
    client_id: str | None
    client_secret: str | None
    count_mode: str
    # D365
    d365_uri: str | None
    d365_auth_mode: str
    d365_tenant_id: str | None
    d365_client_id: str | None
    d365_client_secret: str | None
    table_name_map: dict[str, str]
    skip_d365: bool
    html_report: str | None
    html_open: bool
    csv_report: str | None
    d365_service_version: str


def _parse_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def load_config() -> Config:
    load_dotenv()
    raw_tables = os.getenv("FABRIC_TABLES", "").strip()
    tables = [t.strip() for t in raw_tables.split(",") if t.strip()]
    return Config(
        endpoint=os.environ["FABRIC_SQL_ENDPOINT"],
        database=os.environ["FABRIC_DATABASE"],
        tables=tables,
        auth_mode=os.getenv("AUTH_MODE", "interactive").lower(),
        tenant_id=os.getenv("AZURE_TENANT_ID"),
        client_id=os.getenv("AZURE_CLIENT_ID"),
        client_secret=os.getenv("AZURE_CLIENT_SECRET"),
        count_mode=os.getenv("COUNT_MODE", "exact").lower(),
        d365_uri=(os.getenv("D365_URI") or "").rstrip("/") or None,
        d365_auth_mode=os.getenv("D365_AUTH_MODE", "interactive").lower(),
        d365_tenant_id=os.getenv("D365_TENANT_ID"),
        d365_client_id=os.getenv("D365_CLIENT_ID"),
        d365_client_secret=os.getenv("D365_CLIENT_SECRET"),
        table_name_map=_parse_map(os.getenv("TABLE_NAME_MAP", "")),
        skip_d365=os.getenv("SKIP_D365", "false").lower() in ("1", "true", "yes"),
        html_report=(os.getenv("HTML_REPORT") or "").strip() or None,
        html_open=os.getenv("HTML_OPEN", "true").lower() in ("1", "true", "yes"),
        csv_report=(os.getenv("CSV_REPORT") or "").strip() or None,
        d365_service_version=os.getenv("D365_SERVICE_VERSION", "v2").strip().lower(),
    )


# ---------------------------------------------------------------------------
# Fabric
# ---------------------------------------------------------------------------

def get_fabric_token(cfg: Config) -> str:
    if cfg.auth_mode == "serviceprincipal":
        if not (cfg.tenant_id and cfg.client_id and cfg.client_secret):
            raise SystemExit("Fabric service principal auth requires AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET.")
        cred = ClientSecretCredential(cfg.tenant_id, cfg.client_id, cfg.client_secret)
    elif cfg.auth_mode == "cli":
        cred = AzureCliCredential()
    else:
        cred = InteractiveBrowserCredential()
    return cred.get_token(FABRIC_SCOPE).token


def pick_driver() -> str:
    installed = [d.upper() for d in pyodbc.drivers()]
    for preferred in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if preferred.upper() in installed:
            return preferred
    raise SystemExit(
        "No supported ODBC driver found. Install ODBC Driver 18 (recommended) or 17 for SQL Server: "
        "https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
    )


def connect_fabric(cfg: Config) -> pyodbc.Connection:
    token = get_fabric_token(cfg)
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"=i{len(token_bytes)}s", len(token_bytes), token_bytes)
    driver = pick_driver()
    conn_str = (
        f"Driver={{{driver}}};"
        f"Server={cfg.endpoint},1433;"
        f"Database={cfg.database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def split_table(name: str) -> tuple[str, str]:
    if "." in name:
        schema, tbl = name.split(".", 1)
        return schema.strip("[] "), tbl.strip("[] ")
    return "dbo", name.strip("[] ")


def count_fabric_fast(conn: pyodbc.Connection, tables: list[str]) -> list[tuple[str, int, str | None]]:
    cur = conn.cursor()
    if tables:
        results: list[tuple[str, int, str | None]] = []
        for t in tables:
            schema, tbl = split_table(t)
            cur.execute(
                """
                SELECT ISNULL(SUM(p.rows), 0)
                FROM sys.tables t
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                JOIN sys.partitions p ON t.object_id = p.object_id
                WHERE p.index_id IN (0, 1) AND s.name = ? AND t.name = ?
                """,
                schema, tbl,
            )
            row = cur.fetchone()
            results.append((f"{schema}.{tbl}", int(row[0]) if row else 0, None))
        return results

    cur.execute(
        """
        SELECT s.name + '.' + t.name, ISNULL(SUM(p.rows), 0)
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        LEFT JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
        GROUP BY s.name, t.name
        ORDER BY s.name, t.name
        """
    )
    return [(r[0], int(r[1]) if r[1] is not None else 0, None) for r in cur.fetchall()]


def count_fabric_exact(conn: pyodbc.Connection, tables: list[str]) -> list[tuple[str, int, str | None]]:
    cur = conn.cursor()
    if not tables:
        cur.execute(
            "SELECT s.name + '.' + t.name FROM sys.tables t "
            "JOIN sys.schemas s ON t.schema_id = s.schema_id ORDER BY 1"
        )
        tables = [r[0] for r in cur.fetchall()]

    # Discover which tables expose IsDelete / SinkModifiedOn columns
    # (Dataverse / Fabric mirroring metadata).
    cur.execute(
        """
        SELECT LOWER(s.name + '.' + t.name), c.name
        FROM sys.columns c
        JOIN sys.tables  t ON c.object_id = t.object_id
        JOIN sys.schemas s ON t.schema_id  = s.schema_id
        WHERE c.name IN ('IsDelete', 'SinkModifiedOn')
        """
    )
    has_isdelete: set[str] = set()
    has_sink: set[str] = set()
    for full, col in cur.fetchall():
        if col == "IsDelete":
            has_isdelete.add(full)
        elif col == "SinkModifiedOn":
            has_sink.add(full)

    results: list[tuple[str, int, str | None]] = []
    for t in tables:
        schema, tbl = split_table(t)
        full = f"{schema}.{tbl}"
        key = full.lower()
        select = ["COUNT_BIG(*)"]
        if key in has_sink:
            select.append("MAX(SinkModifiedOn)")
        where = " WHERE ISNULL(IsDelete, 0) = 0" if key in has_isdelete else ""
        cur.execute(f"SELECT {', '.join(select)} FROM [{schema}].[{tbl}]{where}")
        row = cur.fetchone()
        count = int(row[0])
        sink = None
        if key in has_sink and row[1] is not None:
            sink = row[1].strftime("%Y-%m-%d %H:%M:%S") if hasattr(row[1], "strftime") else str(row[1])
        results.append((full, count, sink))
    return results


# ---------------------------------------------------------------------------
# Dynamics 365 F&O
# ---------------------------------------------------------------------------

def get_d365_token(cfg: Config) -> str:
    if not cfg.d365_uri:
        raise SystemExit("D365_URI is required to call the FabricHelperService.")
    scope = f"{cfg.d365_uri}/.default"
    if cfg.d365_auth_mode == "serviceprincipal":
        if not (cfg.d365_tenant_id and cfg.d365_client_id and cfg.d365_client_secret):
            raise SystemExit("D365 service principal auth requires D365_TENANT_ID, D365_CLIENT_ID, D365_CLIENT_SECRET.")
        cred = ClientSecretCredential(cfg.d365_tenant_id, cfg.d365_client_id, cfg.d365_client_secret)
    else:
        cred = InteractiveBrowserCredential()
    return cred.get_token(scope).token


def fabric_to_d365_name(fabric_name: str, mapping: dict[str, str]) -> str:
    """Resolve the corresponding D365 (F&O) table name for a Fabric table."""
    key = fabric_name.lower()
    if key in mapping:
        return mapping[key]
    # Strip schema and uppercase (F&O convention).
    _, tbl = split_table(fabric_name)
    return tbl.upper()


def d365_count(session: requests.Session, base_uri: str, token: str, table_name: str, service_version: str = "v2") -> tuple[int | None, str | None, bool]:
    """Returns (count, error_message, not_found).

    service_version:
      - "v1": getTableRecordCount   (direct SQL query - faster)
      - "v2": getTableRecordCountV2 (X++ query - identical to Fabric Link)
    """
    op = "getTableRecordCount" if service_version == "v1" else "getTableRecordCountV2"
    url = f"{base_uri}/api/services/FabricHelperServiceGroup/FabricHelperService/{op}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"_request": {"TableName": table_name, "RequestId": f"FRC-{uuid.uuid4().hex[:8]}"}}

    # Retry with exponential backoff on throttling (429) / transient 5xx.
    for attempt in range(5):
        try:
            r = session.post(url, json=body, headers=headers, timeout=60)
        except requests.RequestException as e:
            return None, f"network: {e}", False
        if r.status_code in (429, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", "0")) or (2 ** attempt)
            time.sleep(min(wait, 30))
            continue
        break

    if r.status_code == 404:
        return None, None, True
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}", False
    try:
        data = r.json()
    except ValueError:
        return None, f"non-JSON response: {r.text[:200]}", False

    if data.get("Success", False):
        rc = data.get("RecordCount")
        if rc is None:
            return None, "no RecordCount in response", False
        return int(rc), None, False

    # Success=false. The service returns a generic "Error: " for both unknown tables
    # and other failures, and echoes back empty TableName/RequestId. Treat that
    # signature as "table not found" (N/A). Anything else is a real error.
    msg = (data.get("Message") or "").strip()
    echoed_table = data.get("TableName") or ""
    not_found_markers = ("does not exist", "not found", "unknown table", "no such table", "invalid table")
    looks_generic = msg.lower().rstrip(":").strip() in ("error", "") and not echoed_table
    if looks_generic or any(m in msg.lower() for m in not_found_markers):
        return None, None, True
    return None, msg or "service returned Success=false", False


# ---------------------------------------------------------------------------
# Comparison & reporting
# ---------------------------------------------------------------------------

def classify(fabric: int | None, d365: int | None, not_found: bool, err: str | None) -> tuple[str, str]:
    """Return (status, delta_str)."""
    if not_found:
        return "N/A", "-"
    if err:
        return "Error", err[:60]
    if fabric is None or d365 is None:
        return "Error", "-"
    delta = d365 - fabric
    if delta == 0:
        return "Match", "0"
    if delta > 0:
        return "Drift", f"+{delta:,}"
    return "Anomaly", f"{delta:,}"


STATUS_COLORS = {
    "Match":   ("#1b873f", "#e6f7ec"),
    "Drift":   ("#a86200", "#fff4e0"),
    "Anomaly": ("#a4262c", "#fde7e9"),
    "N/A":     ("#5c5c5c", "#eeeeee"),
    "Error":   ("#a4262c", "#fde7e9"),
}


def render_html_report(cfg: Config, rows, counts, out_path: str) -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total = len(rows)

    def cell(value, align="left"):
        return f'<td style="text-align:{align}">{html.escape(str(value))}</td>'

    body_rows = []
    for fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink in rows:
        fg, bg = STATUS_COLORS.get(status, ("#000", "#fff"))
        d365_disp = f"{d365_count_val:,}" if d365_count_val is not None else "—"
        body_rows.append(
            "<tr>"
            + cell(fabric_name)
            + cell(d365_name)
            + cell(f"{fabric_count:,}", "right")
            + cell(d365_disp, "right")
            + cell(delta, "right")
            + f'<td style="text-align:center"><span class="badge" '
              f'style="background:{bg};color:{fg};border:1px solid {fg}33">'
              f'{html.escape(status)}</span></td>'
            + cell(sink or "—", "center")
            + "</tr>"
        )

    summary_chips = "".join(
        f'<span class="chip" style="background:{STATUS_COLORS[k][1]};color:{STATUS_COLORS[k][0]};'
        f'border:1px solid {STATUS_COLORS[k][0]}33">{k}: <b>{counts.get(k, 0)}</b></span>'
        for k in ("Match", "Drift", "Anomaly", "N/A", "Error")
    )

    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fabric vs D365 Row Count Report</title>
  <style>
    :root {{ font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color:#222; }}
    body {{ margin: 24px; background: #fafafa; }}
    h1 {{ margin: 0 0 4px; font-size: 22px; }}
    .meta {{ color:#666; font-size: 13px; margin-bottom: 16px; }}
    .meta code {{ background:#eee; padding:1px 5px; border-radius:3px; }}
    .summary {{ margin: 12px 0 18px; display:flex; gap:8px; flex-wrap:wrap; }}
    .chip, .badge {{ display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:500; }}
    table {{ border-collapse: collapse; width:100%; background:#fff; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
    thead th {{ background:#f3f3f3; padding:8px 10px; text-align:left; font-size:13px; border-bottom:1px solid #ddd; cursor:pointer; user-select:none; }}
    thead th.right {{ text-align:right; }}
    thead th.center {{ text-align:center; }}
    tbody td {{ padding:7px 10px; font-size:13px; border-bottom:1px solid #eee; }}
    tbody tr:hover {{ background:#fafcff; }}
    .footer {{ margin-top: 14px; color:#888; font-size:11px; }}
    input[type="search"] {{ padding:6px 10px; width:280px; border:1px solid #ccc; border-radius:4px; font-size:13px; margin-bottom:8px; }}
  </style>
</head>
<body>
  <h1>Fabric vs Dynamics 365 Row Count Comparison</h1>
  <div class="meta">
    Generated <b>{generated}</b><br>
    Fabric: <code>{html.escape(cfg.endpoint)}</code> / <code>{html.escape(cfg.database)}</code><br>
    D365: <code>{html.escape(cfg.d365_uri or '')}</code> &nbsp; Tables: <b>{total}</b>
  </div>
  <div class="summary">{summary_chips}</div>
  <input id="filter" type="search" placeholder="Filter by table name or status..." oninput="filterRows(this.value)">
  <table id="grid">
    <thead>
      <tr>
        <th onclick="sortTable(0)">Fabric Table</th>
        <th onclick="sortTable(1)">D365 Table</th>
        <th class="right" onclick="sortTable(2, true)">Fabric Rows</th>
        <th class="right" onclick="sortTable(3, true)">D365 Rows</th>
        <th class="right" onclick="sortTable(4, true)">Delta</th>
        <th class="center" onclick="sortTable(5)">Status</th>
        <th class="center" onclick="sortTable(6)">Last SinkModifiedOn</th>
      </tr>
    </thead>
    <tbody>
      {''.join(body_rows)}
    </tbody>
  </table>
  <div class="footer">Generated by fabric-row-counter</div>

  <script>
    function filterRows(q) {{
      q = q.toLowerCase();
      document.querySelectorAll('#grid tbody tr').forEach(tr => {{
        tr.style.display = tr.innerText.toLowerCase().includes(q) ? '' : 'none';
      }});
    }}
    let sortDir = {{}};
    function sortTable(col, numeric=false) {{
      const tbody = document.querySelector('#grid tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const dir = sortDir[col] = !sortDir[col];
      rows.sort((a, b) => {{
        let av = a.children[col].innerText.trim();
        let bv = b.children[col].innerText.trim();
        if (numeric) {{
          const an = parseFloat(av.replace(/[+,\u2014-]/g, '')) || 0;
          const bn = parseFloat(bv.replace(/[+,\u2014-]/g, '')) || 0;
          return dir ? an - bn : bn - an;
        }}
        return dir ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      rows.forEach(r => tbody.appendChild(r));
    }}
  </script>
</body>
</html>"""

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


def main() -> int:
    cfg = load_config()
    print(f"Connecting to Fabric {cfg.endpoint} / {cfg.database} "
          f"(auth={cfg.auth_mode}, mode={cfg.count_mode})")

    with connect_fabric(cfg) as conn:
        if cfg.count_mode == "exact":
            fabric_rows = count_fabric_exact(conn, cfg.tables)
        else:
            fabric_rows = count_fabric_fast(conn, cfg.tables)

    if cfg.skip_d365 or not cfg.d365_uri:
        if not cfg.skip_d365:
            print("\n[D365_URI not set - skipping D365 comparison]")
        width = max((len(name) for name, *_ in fabric_rows), default=10)
        print(f"\n{'Table'.ljust(width)}  {'Fabric Rows':>15}  {'Last SinkModifiedOn':>20}")
        print(f"{'-' * width}  {'-' * 15}  {'-' * 20}")
        for name, n, sink in fabric_rows:
            print(f"{name.ljust(width)}  {n:>15,}  {(sink or '-'):>20}")
        print(f"\n{len(fabric_rows)} table(s) reported.")
        return 0

    print(f"\nAuthenticating to D365 at {cfg.d365_uri} (auth={cfg.d365_auth_mode}, service={cfg.d365_service_version})")
    d365_token = get_d365_token(cfg)
    session = requests.Session()

    rows: list[tuple[str, str, int, int | None, str, str, str | None]] = []
    for fabric_name, fabric_count, sink in fabric_rows:
        d365_name = fabric_to_d365_name(fabric_name, cfg.table_name_map)
        d365_count_val, err, not_found = d365_count(session, cfg.d365_uri, d365_token, d365_name, cfg.d365_service_version)
        status, delta = classify(fabric_count, d365_count_val, not_found, err)
        rows.append((fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink))

    # Render table
    headers = ("Fabric Table", "D365 Table", "Fabric Rows", "D365 Rows", "Delta", "Status", "Last SinkModifiedOn")
    cols = list(zip(*([headers] + [
        (n, d, f"{f:,}", f"{c:,}" if c is not None else "-", str(delta), status, sink or "-")
        for n, d, f, c, delta, status, sink in rows
    ])))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print()
    print(fmt.format(*headers).rstrip())
    print(fmt.format(*["-" * w for w in widths]).rstrip())
    for n, d, f, c, delta, status, sink in rows:
        print(fmt.format(n, d, f"{f:,}", f"{c:,}" if c is not None else "-", str(delta), status, sink or "-").rstrip())

    # Summary
    counts = {"Match": 0, "Drift": 0, "Anomaly": 0, "N/A": 0, "Error": 0}
    for row in rows:
        status = row[5]
        counts[status] = counts.get(status, 0) + 1
    print(f"\nSummary: {counts['Match']} Match, {counts['Drift']} Drift, "
          f"{counts['Anomaly']} Anomaly, {counts['N/A']} N/A, {counts['Error']} Error "
          f"(total {len(rows)} table(s))")

    # Timestamped output files
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # HTML report
    if cfg.html_report:
        html_path = timestamped_path(cfg.html_report, ts)
        path = render_html_report(cfg, rows, counts, html_path)
        print(f"\nHTML report written: {path}")
        if cfg.html_open:
            try:
                webbrowser.open(f"file:///{path.replace(os.sep, '/')}")
            except Exception:
                pass

    # CSV report
    if cfg.csv_report:
        csv_path = timestamped_path(cfg.csv_report, ts)
        write_csv_report(rows, csv_path)
        print(f"CSV  report written: {csv_path}")
    return 0


def timestamped_path(template: str, ts: str) -> str:
    """Insert a timestamp before the extension: report.html -> report_20260429_174400.html"""
    base, ext = os.path.splitext(template)
    return os.path.abspath(f"{base}_{ts}{ext}")


def write_csv_report(rows, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Fabric Table", "D365 Table", "Fabric Rows", "D365 Rows", "Delta", "Status", "Last SinkModifiedOn"])
        for fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink in rows:
            w.writerow([
                fabric_name, d365_name, fabric_count,
                d365_count_val if d365_count_val is not None else "",
                delta, status, sink or "",
            ])


if __name__ == "__main__":
    sys.exit(main())
