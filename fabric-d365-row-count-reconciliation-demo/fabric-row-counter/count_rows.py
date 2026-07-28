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
import json
import os
import re
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
APP_VERSION = "1.1.1.2"

# Cache a single InteractiveBrowserCredential per process so MSAL's token cache
# can serve subsequent get_token() calls silently. Creating a brand-new
# InteractiveBrowserCredential() on every call spawns a new local redirect
# listener + browser tab each time; if a stale tab from a previous attempt is
# still open (or the OS reuses a just-freed port), its callback can land on the
# new listener and fail OAuth 'state' (CSRF) validation with a confusing
# "state mismatch" error instead of ever reaching the SQL/D365 connection.
_interactive_credential_cache: dict[str, InteractiveBrowserCredential] = {}


def _get_interactive_credential(key: str = "default") -> InteractiveBrowserCredential:
    cred = _interactive_credential_cache.get(key)
    if cred is None:
        cred = InteractiveBrowserCredential()
        _interactive_credential_cache[key] = cred
    return cred


@dataclass
class Config:
    # Source selection
    source_type: str  # "fabric" | "synapse"
    # Fabric Link
    endpoint: str
    database: str
    tables: list[str]
    auth_mode: str
    tenant_id: str | None
    client_id: str | None
    client_secret: str | None
    count_mode: str
    # Synapse Link (SQL Serverless)
    synapse_endpoint: str | None
    synapse_database: str | None
    synapse_auth_mode: str
    synapse_tenant_id: str | None
    synapse_client_id: str | None
    synapse_client_secret: str | None
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
    # When True (default), interactive auth reuses the same cached credential
    # (and therefore the same signed-in account) for Fabric/Synapse and D365.
    # When False, D365 gets its own InteractiveBrowserCredential instance so a
    # different user can sign in for D365 than for Fabric/Synapse.
    same_credentials: bool = True


def _parse_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


_D365_MINVALUE_MS = -2208988800000  # DateTimeUtil::minValue() = 1900-01-01 00:00:00 UTC

def _parse_d365_datetime(raw) -> str | None:
    """Convert a D365 datetime value to 'YYYY-MM-DD HH:MM:SS'.

    Handles both WCF /Date(ms)/ and ISO 8601 formats.
    Returns None for null and 1900-01-01 (DateTimeUtil::minValue) sentinels.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # WCF /Date(ms)/ format
    m = re.match(r"^/Date\((-?\d+)\)/", s)
    if m:
        ms = int(m.group(1))
        if ms <= _D365_MINVALUE_MS:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # ISO 8601 / SQL datetime string (e.g. "2026-04-29T08:08:42Z" or "2026-04-29 08:08:42")
    s_clean = s.rstrip("Z").replace("T", " ")[:19]  # "YYYY-MM-DD HH:MM:SS"
    if s_clean.startswith("1900-01-01") or s_clean.startswith("0001-01-01"):
        return None
    try:
        datetime.strptime(s_clean, "%Y-%m-%d %H:%M:%S")  # validate
        return s_clean
    except ValueError:
        return None


def compute_latency(d365_mod: str | None, fabric_mod: str | None, fabric_synced_mod: str | None = None, sink_mod: str | None = None, status: str | None = None) -> str:
    """Return a human-readable lag between D365’s latest change and Fabric’s last known state.

    Reference priority for the Fabric side:
    1. fabric_mod  (MAX(MODIFIEDDATETIME) from Fabric)
    2. If sink_mod > fabric_mod AND sink_mod < d365_mod, use sink_mod instead —
       SinkModifiedOn is a more recent ingestion marker in that case.
    3. Falls back to fabric_synced_mod (estimated from D365 SYSROWVERSION).
    ‘0s’ = Fabric current or ahead.  ‘N/A’ = timestamps unavailable.

    When ``status`` is ``"Match"`` the row counts are already aligned between the
    source and D365, so a latency figure wouldn't be meaningful — skip the
    calculation entirely and return "N/A".
    """
    if status == "Match":
        return "N/A"
    ref = fabric_mod or fabric_synced_mod
    # Override with SinkModifiedOn when it is a better “how current is Fabric” proxy:
    # i.e. more recent than MODIFIEDDATETIME but still behind D365’s latest change.
    if sink_mod and fabric_mod and d365_mod:
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            t_sink = datetime.strptime(sink_mod, fmt).replace(tzinfo=timezone.utc)
            t_fab  = datetime.strptime(fabric_mod, fmt).replace(tzinfo=timezone.utc)
            t_d365 = datetime.strptime(d365_mod, fmt).replace(tzinfo=timezone.utc)
            if t_sink > t_fab and t_sink < t_d365:
                ref = sink_mod
        except (ValueError, TypeError):
            pass
    if not d365_mod or not ref:
        return "N/A"
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        t_d365 = datetime.strptime(d365_mod, fmt).replace(tzinfo=timezone.utc)
        t_ref  = datetime.strptime(ref, fmt).replace(tzinfo=timezone.utc)
        total  = int((t_d365 - t_ref).total_seconds())
        if total <= 0:
            return "0s"
        days, rem    = divmod(total, 86400)
        hours, rem   = divmod(rem, 3600)
        minutes, sec = divmod(rem, 60)
        if days:    return f"{days}d {hours}h"
        if hours:   return f"{hours}h {minutes}m"
        if minutes: return f"{minutes}m {sec}s"
        return f"{sec}s"
    except (ValueError, TypeError):
        return "N/A"


def load_config() -> Config:
    load_dotenv()
    raw_tables = os.getenv("FABRIC_TABLES", "").strip()
    tables = [t.strip() for t in raw_tables.split(",") if t.strip()]
    source = os.getenv("SOURCE_TYPE", "fabric").lower()
    if source == "fabric" and not os.getenv("FABRIC_SQL_ENDPOINT"):
        raise SystemExit("FABRIC_SQL_ENDPOINT is required when SOURCE_TYPE=fabric (or is unset).")
    if source == "synapse" and not os.getenv("SYNAPSE_SQL_ENDPOINT"):
        raise SystemExit("SYNAPSE_SQL_ENDPOINT is required when SOURCE_TYPE=synapse.")
    return Config(
        source_type=source,
        endpoint=(os.getenv("FABRIC_SQL_ENDPOINT") or "").strip(),
        database=(os.getenv("FABRIC_DATABASE") or "").strip(),
        tables=tables,
        auth_mode=os.getenv("AUTH_MODE", "interactive").lower(),
        tenant_id=os.getenv("AZURE_TENANT_ID"),
        client_id=os.getenv("AZURE_CLIENT_ID"),
        client_secret=os.getenv("AZURE_CLIENT_SECRET"),
        count_mode=os.getenv("COUNT_MODE", "exact").lower(),
        synapse_endpoint=(os.getenv("SYNAPSE_SQL_ENDPOINT") or "").strip() or None,
        synapse_database=(os.getenv("SYNAPSE_DATABASE") or "").strip() or None,
        synapse_auth_mode=os.getenv("SYNAPSE_AUTH_MODE", "interactive").lower(),
        synapse_tenant_id=os.getenv("SYNAPSE_TENANT_ID"),
        synapse_client_id=os.getenv("SYNAPSE_CLIENT_ID"),
        synapse_client_secret=os.getenv("SYNAPSE_CLIENT_SECRET"),
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
        d365_service_version=os.getenv("D365_SERVICE_VERSION", "v3").strip().lower(),
        same_credentials=os.getenv("SAME_CREDENTIALS", "true").lower() in ("1", "true", "yes"),
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
        cred = _get_interactive_credential("default" if cfg.same_credentials else "source")
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


def get_synapse_token(cfg: Config) -> str:
    """Obtain an Azure AD bearer token for Synapse Link SQL Serverless (same scope as Fabric)."""
    if cfg.synapse_auth_mode == "serviceprincipal":
        if not (cfg.synapse_tenant_id and cfg.synapse_client_id and cfg.synapse_client_secret):
            raise SystemExit(
                "Synapse service principal auth requires SYNAPSE_TENANT_ID, "
                "SYNAPSE_CLIENT_ID, SYNAPSE_CLIENT_SECRET."
            )
        cred = ClientSecretCredential(cfg.synapse_tenant_id, cfg.synapse_client_id, cfg.synapse_client_secret)
    elif cfg.synapse_auth_mode == "cli":
        cred = AzureCliCredential()
    else:
        cred = _get_interactive_credential("default" if cfg.same_credentials else "source")
    return cred.get_token(FABRIC_SCOPE).token  # https://database.windows.net/.default


def connect_synapse(cfg: Config) -> pyodbc.Connection:
    """Connect to a Synapse Link SQL Serverless endpoint using Azure AD token auth.

    The serverless endpoint format is:
        <workspace>-ondemand.sql.azuresynapse.net

    Note: sys.partitions metadata is not available on SQL Serverless — only
    COUNT_BIG(*) (exact mode) is supported.
    """
    if not cfg.synapse_endpoint or not cfg.synapse_database:
        raise SystemExit(
            "SYNAPSE_SQL_ENDPOINT and SYNAPSE_DATABASE are required when SOURCE_TYPE=synapse.\n"
            "The endpoint looks like: <workspace>-ondemand.sql.azuresynapse.net"
        )
    token = get_synapse_token(cfg)
    token_bytes = token.encode("utf-16-le")
    token_struct = struct.pack(f"=i{len(token_bytes)}s", len(token_bytes), token_bytes)
    driver = pick_driver()
    conn_str = (
        f"Driver={{{driver}}};"
        f"Server={cfg.synapse_endpoint},1433;"
        f"Database={cfg.synapse_database};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct})


def split_table(name: str) -> tuple[str, str]:
    if "." in name:
        schema, tbl = name.split(".", 1)
        return schema.strip("[] "), tbl.strip("[] ")
    return "dbo", name.strip("[] ")


def _resolve_table_case(conn: pyodbc.Connection, tables: list[str]) -> tuple[list[str], set[str]]:
    """Resolve user-supplied schema.table names to their actual stored case.

    Fabric Warehouse (and some Synapse SQL Serverless databases) default to a
    case-sensitive (BIN2) collation, so ``FROM [dbo].[BatchHistory]`` raises
    "Invalid object name" if the real object is ``dbo.batchhistory``. Look up
    the real casing once via INFORMATION_SCHEMA and substitute it so manually
    typed table names (any case) still resolve.

    Returns (resolved_names, not_found_set). Names with no catalog match are
    passed through unchanged AND included in ``not_found_set`` — callers should
    treat those as "table doesn't exist on this side" (e.g. a system table like
    BatchHistory that was never part of the Fabric/Synapse Link sync scope)
    rather than attempting a doomed query and reporting a generic Error.
    """
    if not tables:
        return tables, set()
    cur = conn.cursor()
    cur.execute("SELECT table_schema, table_name FROM INFORMATION_SCHEMA.TABLES")
    catalog = {f"{s.upper()}.{t.upper()}": f"{s}.{t}" for s, t in cur.fetchall()}
    resolved = []
    not_found: set[str] = set()
    for name in tables:
        schema, tbl = split_table(name)
        key = f"{schema.upper()}.{tbl.upper()}"
        full = f"{schema}.{tbl}"
        resolved_name = catalog.get(key, full)
        resolved.append(resolved_name)
        if key not in catalog:
            not_found.add(resolved_name)
    return resolved, not_found


def count_fabric_fast(conn: pyodbc.Connection, tables: list[str]) -> list[tuple[str, int | None, str | None, int | None, str | None, str | None]]:
    cur = conn.cursor()
    if tables:
        tables, not_found = _resolve_table_case(conn, tables)
        results: list[tuple[str, int | None, str | None, int | None, str | None, str | None]] = []
        for t in tables:
            schema, tbl = split_table(t)
            full = f"{schema}.{tbl}"
            if full in not_found:
                results.append((full, None, None, None, None, "NOT_FOUND"))
                continue
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
            results.append((full, int(row[0]) if row else 0, None, None, None, None))
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
    return [(r[0], int(r[1]) if r[1] is not None else 0, None, None, None, None) for r in cur.fetchall()]


def count_fabric_exact(conn: pyodbc.Connection, tables: list[str]) -> list[tuple[str, int | None, str | None, int | None, str | None, str | None]]:
    cur      = conn.cursor()  # main cursor for SELECT COUNT / aggregate queries
    cur_meta = conn.cursor()  # separate cursor for INFORMATION_SCHEMA detection
    not_found: set[str] = set()
    if not tables:
        cur_meta.execute(
            "SELECT table_schema + '.' + table_name FROM INFORMATION_SCHEMA.TABLES "
            "WHERE table_type = 'BASE TABLE' ORDER BY 1"
        )
        tables = [r[0] for r in cur_meta.fetchall()]
    else:
        tables, not_found = _resolve_table_case(conn, tables)

    def _dt(v) -> str | None:
        if v is None:
            return None
        return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else (str(v) or None)

    results: list[tuple[str, int | None, str | None, int | None, str | None, str | None]] = []
    for t in tables:
        schema, tbl = split_table(t)
        full = f"{schema}.{tbl}"
        if full in not_found:
            # Table doesn't exist in this Fabric/Synapse database at all (e.g. a
            # system table like BatchHistory that was never in the Link sync scope).
            # Report it as missing rather than running a doomed query and showing
            # a generic, undiagnosable "Error".
            results.append((full, None, None, None, None, "NOT_FOUND"))
            continue
        # Detect available columns via a targeted INFORMATION_SCHEMA.COLUMNS query
        # using a DEDICATED cursor so it doesn't interfere with the main SELECT cursor.
        t_col_map: dict[str, str] = {}
        try:
            cur_meta.execute(
                "SELECT UPPER(column_name), column_name "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE table_schema = '{schema}' AND table_name = '{tbl}' "
                "AND UPPER(column_name) IN "
                "('ISDELETE','SINKMODIFIEDON','SYSROWVERSION','MODIFIEDDATETIME')"
            )
            t_col_map = {row[0]: row[1] for row in cur_meta.fetchall()}
        except Exception:
            pass

        col_sink     = f"MAX([{t_col_map['SINKMODIFIEDON']}])"   if "SINKMODIFIEDON"   in t_col_map else "NULL"
        col_sysrv    = f"MAX([{t_col_map['SYSROWVERSION']}])"    if "SYSROWVERSION"    in t_col_map else "NULL"
        col_moddt    = f"MAX([{t_col_map['MODIFIEDDATETIME']}])" if "MODIFIEDDATETIME" in t_col_map else "NULL"
        isdelete_col = t_col_map.get("ISDELETE")
        where        = f" WHERE ISNULL([{isdelete_col}], 0) = 0" if isdelete_col else ""
        try:
            cur.execute(
                f"SELECT COUNT_BIG(*), {col_sink}, {col_sysrv}, {col_moddt} "
                f"FROM [{schema}].[{tbl}]{where}"
            )
            row = cur.fetchone()
            # sysrowversion may come back as bytes (SQL rowversion type) — convert to int
            raw_srv = row[2]
            if isinstance(raw_srv, (bytes, bytearray)):
                fabric_srv: int | None = int.from_bytes(raw_srv, "big")
            elif raw_srv is not None:
                try:
                    fabric_srv = int(raw_srv)
                except (TypeError, ValueError):
                    fabric_srv = None
            else:
                fabric_srv = None
            results.append((
                full,
                int(row[0]),
                _dt(row[1]),
                fabric_srv,
                _dt(row[3]),
                None,
            ))
        except Exception as _count_err:
            # Table metadata exists but underlying storage is unavailable
            # (e.g. OneLake Parquet file missing, Fabric Link sync in progress).
            # Record the real error message so this shows up as an actionable
            # Error (not a blank one) instead of silently aborting the run.
            import sys as _sys
            print(f"[WARN] Count query failed for {full}: {_count_err}", file=_sys.stderr, flush=True)
            results.append((full, None, None, None, None, str(_count_err)))
    return results


# ---------------------------------------------------------------------------
# Dynamics 365 F&O
# ---------------------------------------------------------------------------

def get_d365_token(cfg: Config) -> str:
    if not cfg.d365_uri:
        raise SystemExit("D365_URI is required to call the FabricHelperService.")
    if not cfg.d365_uri.startswith(("http://", "https://")):
        raise SystemExit(
            f"D365 base URI '{cfg.d365_uri}' is not a valid URL — it must start with https:// "
            "(e.g. https://yourorg.operations.dynamics.com). This looks like it might be a "
            "Fabric/Dataverse mirrored-database name, not the D365 F&O environment URL."
        )
    scope = f"{cfg.d365_uri}/.default"
    if cfg.d365_auth_mode == "serviceprincipal":
        if not (cfg.d365_tenant_id and cfg.d365_client_id and cfg.d365_client_secret):
            raise SystemExit("D365 service principal auth requires D365_TENANT_ID, D365_CLIENT_ID, D365_CLIENT_SECRET.")
        cred = ClientSecretCredential(cfg.d365_tenant_id, cfg.d365_client_id, cfg.d365_client_secret)
    else:
        cred = _get_interactive_credential("default" if cfg.same_credentials else "d365")
    return cred.get_token(scope).token


def fabric_to_d365_name(fabric_name: str, mapping: dict[str, str]) -> str:
    """Resolve the corresponding D365 (F&O) table name for a Fabric table."""
    key = fabric_name.lower()
    if key in mapping:
        return mapping[key]
    # Strip schema and uppercase (F&O convention).
    _, tbl = split_table(fabric_name)
    return tbl.upper()


def d365_count(session: requests.Session, base_uri: str, token: str, table_name: str, service_version: str = "v2", *, fabric_srv: int | None = None, fabric_mod: str | None = None) -> tuple[int | None, str | None, bool, int | None, str | None, bool, str | None, bool]:
    """Returns (count, error, not_found, last_sysrowversion, last_modified_dt, is_estimated, fabric_synced_mod, is_fabric_sync_est).

    service_version:
      - "v1": getTableRecordCount   (direct SQL query - faster)
      - "v2": getTableRecordCountV2 (X++ query - identical to Fabric Link)
      - "v3": use d365_count_batch() + d365_metadata() instead (bulk catalog-based
              counts in one round trip); this function is not used in that mode.
    fabric_mod: MAX(MODIFIEDDATETIME) from Fabric — when provided, D365 is not asked for a sync point.
    fabric_srv: MAX(SYSROWVERSION) from Fabric — sent to D365 only when fabric_mod is None.
    """
    op = "getTableRecordCount" if service_version == "v1" else "getTableRecordCountV2"
    url = f"{base_uri}/api/services/FabricHelperServiceGroup/FabricHelperService/{op}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"_request": {"TableName": table_name, "RequestId": f"FRC-{uuid.uuid4().hex[:8]}"}}
    # Pass Fabric rowversion to D365 only when Fabric lacks MODIFIEDDATETIME;
    # D365 uses it to resolve the modification time at that sync point.
    if fabric_mod is None and fabric_srv:
        body["_request"]["FabricLastSysRowVersion"] = fabric_srv

    # Retry with exponential backoff on throttling (429) / transient 5xx.
    for attempt in range(5):
        try:
            r = session.post(url, json=body, headers=headers, timeout=60)
        except requests.RequestException as e:
            return None, f"network: {e}", False, None, None, False, None, False
        if r.status_code in (429, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", "0")) or (2 ** attempt)
            time.sleep(min(wait, 30))
            continue
        break

    if r.status_code == 404:
        return None, None, True, None, None, False, None, False
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}", False, None, None, False, None, False
    try:
        data = r.json()
    except ValueError:
        return None, f"non-JSON response: {r.text[:200]}", False, None, None, False, None, False

    if data.get("Success", False):
        rc = data.get("RecordCount")
        if rc is None:
            return None, "no RecordCount in response", False, None, None, False, None, False
        last_srv = int(data.get("LastSysRowVersion") or 0)
        last_mod = _parse_d365_datetime(data.get("LastModifiedDateTime"))
        is_est = bool(data.get("IsEstimated", False))
        fabric_synced_mod = _parse_d365_datetime(data.get("FabricSyncedModifiedDateTime"))
        is_fabric_sync_est = bool(data.get("IsFabricSyncEstimated", False))
        return int(rc), None, False, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est

    # Success=false. The service returns a generic "Error: " for both unknown tables
    # and other failures, and echoes back empty TableName/RequestId. Treat that
    # signature as "table not found" (N/A). Anything else is a real error.
    msg = (data.get("Message") or "").strip()
    echoed_table = data.get("TableName") or ""
    not_found_markers = ("does not exist", "not found", "unknown table", "no such table", "invalid table")
    looks_generic = msg.lower().rstrip(":").strip() in ("error", "") and not echoed_table
    if looks_generic or any(m in msg.lower() for m in not_found_markers):
        return None, None, True, None, None, False, None, False
    return None, msg or "service returned Success=false", False, None, None, False, None, False


def _post_d365(session: requests.Session, url: str, token: str, body: dict) -> tuple[requests.Response | None, str | None]:
    """POST to a FabricHelperService operation with 429/5xx retry. Returns (response, error)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    r = None
    for attempt in range(5):
        try:
            r = session.post(url, json=body, headers=headers, timeout=120)
        except requests.RequestException as e:
            return None, f"network: {e}"
        if r.status_code in (429, 502, 503, 504):
            wait = int(r.headers.get("Retry-After", "0")) or (2 ** attempt)
            time.sleep(min(wait, 30))
            continue
        break
    return r, None


def d365_count_batch(session: requests.Session, base_uri: str, token: str, table_names: list[str]) -> tuple[dict[str, int], str | None]:
    """Calls getTableRowCounts ONCE for every table in table_names.

    Returns (counts_by_lower_table_name, error). A table missing from the
    returned dict means D365 has no such table (or it wasn't counted) —
    callers should treat that the same way as a per-table 404 (not_found).

    Uses catalog-based statistics (sys.dm_db_partition_stats) server-side, so
    it avoids a COUNT_BIG(*) scan per table and collapses N HTTP round trips
    into one.
    """
    if not table_names:
        return {}, None
    url = f"{base_uri}/api/services/FabricHelperServiceGroup/FabricHelperService/getTableRowCounts"
    body = {"_request": {"TableNames": ",".join(table_names), "RequestId": f"FRC-{uuid.uuid4().hex[:8]}"}}
    r, err = _post_d365(session, url, token, body)
    if err:
        return {}, err
    if r.status_code != 200:
        return {}, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        data = r.json()
    except ValueError:
        return {}, f"non-JSON response: {r.text[:200]}"
    if not data.get("Success", False):
        return {}, data.get("Message") or "service returned Success=false"
    try:
        counts = json.loads(data.get("RowCountsJson") or "{}")
    except ValueError:
        return {}, "invalid RowCountsJson in response"
    return {str(k).lower(): int(v) for k, v in counts.items()}, None


def d365_metadata(session: requests.Session, base_uri: str, token: str, table_name: str, *, fabric_srv: int | None = None, fabric_mod: str | None = None) -> tuple[str | None, bool, int | None, str | None, bool, str | None, bool]:
    """Calls getTableMetadata for a single table — same metadata as d365_count()
    but WITHOUT computing a row count (no COUNT_BIG(*) scan). Pair with
    d365_count_batch() for the counts.

    Returns (error, not_found, last_sysrowversion, last_modified_dt, is_estimated, fabric_synced_mod, is_fabric_sync_est).
    """
    url = f"{base_uri}/api/services/FabricHelperServiceGroup/FabricHelperService/getTableMetadata"
    body = {"_request": {"TableName": table_name, "RequestId": f"FRC-{uuid.uuid4().hex[:8]}"}}
    if fabric_mod is None and fabric_srv:
        body["_request"]["FabricLastSysRowVersion"] = fabric_srv

    r, err = _post_d365(session, url, token, body)
    if err:
        return err, False, None, None, False, None, False
    if r.status_code == 404:
        return None, True, None, None, False, None, False
    if r.status_code != 200:
        return f"HTTP {r.status_code}: {r.text[:200]}", False, None, None, False, None, False
    try:
        data = r.json()
    except ValueError:
        return f"non-JSON response: {r.text[:200]}", False, None, None, False, None, False

    if data.get("Success", False):
        last_srv = int(data.get("LastSysRowVersion") or 0)
        last_mod = _parse_d365_datetime(data.get("LastModifiedDateTime"))
        is_est = bool(data.get("IsEstimated", False))
        fabric_synced_mod = _parse_d365_datetime(data.get("FabricSyncedModifiedDateTime"))
        is_fabric_sync_est = bool(data.get("IsFabricSyncEstimated", False))
        return None, False, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est

    msg = (data.get("Message") or "").strip()
    echoed_table = data.get("TableName") or ""
    not_found_markers = ("does not exist", "not found", "unknown table", "no such table", "invalid table")
    looks_generic = msg.lower().rstrip(":").strip() in ("error", "") and not echoed_table
    if looks_generic or any(m in msg.lower() for m in not_found_markers):
        return None, True, None, None, False, None, False
    return msg or "service returned Success=false", False, None, None, False, None, False


# ---------------------------------------------------------------------------
# Comparison & reporting
# ---------------------------------------------------------------------------

def classify(fabric: int | None, d365: int | None, not_found: bool, err: str | None, fabric_err: str | None = None) -> tuple[str, str]:
    """Return (status, delta_str)."""
    if not_found:
        return "N/A", "-"
    if err:
        return "Error", err[:60]
    if fabric_err == "NOT_FOUND":
        # Table simply doesn't exist on the Fabric/Synapse side (e.g. an
        # unsynced system table) — this is an expected gap, not an app error.
        return "N/A", "-"
    if fabric_err:
        return "Error", f"Fabric: {fabric_err[:50]}"
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


def render_html_report(cfg: Config, rows, counts, out_path: str, source_label: str = "Fabric") -> str:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    total = len(rows)
    src_endpoint = (cfg.synapse_endpoint or "") if cfg.source_type == "synapse" else cfg.endpoint
    src_database = (cfg.synapse_database or "") if cfg.source_type == "synapse" else cfg.database

    def cell(value, align="left"):
        return f'<td style="text-align:{align}">{html.escape(str(value))}</td>'

    body_rows = []
    for fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink, last_srv, last_mod, is_est, latency, fabric_srv, fabric_last_mod, is_fabric_mod_est in rows:
        fg, bg = STATUS_COLORS.get(status, ("#000", "#fff"))
        fab_srv_disp    = "—" if fabric_srv is None else str(fabric_srv)
        fab_mod_content = html.escape(fabric_last_mod) if fabric_last_mod else "—"
        if is_fabric_mod_est and fabric_last_mod:
            fab_mod_content += ' <em title="Estimated from correlated changes via SYSROWVERSION" style="color:#888;font-size:11px">(est.)</em>'
        d365_disp    = f"{d365_count_val:,}" if d365_count_val is not None else "—"
        fabric_disp  = f"{fabric_count:,}" if fabric_count is not None else "Error"
        srv_disp     = "—" if last_srv is None else str(last_srv)
        mod_content  = html.escape(last_mod) if last_mod else "—"
        if is_est and last_mod:
            mod_content += ' <em title="Estimated from correlated changes via SYSROWVERSION" style="color:#888;font-size:11px">(est.)</em>'
        lat_fg = "#a86200" if latency not in ("N/A", "0s", "—", "-") else "#5c5c5c"
        body_rows.append(
            "<tr>"
            + cell(fabric_name)
            + cell(d365_name)
            + cell(fabric_disp, "right")
            + cell(fab_srv_disp, "right")
            + f'<td style="text-align:center;white-space:nowrap">{fab_mod_content}</td>'
            + cell(d365_disp, "right")
            + cell(srv_disp, "right")
            + f'<td style="text-align:center;white-space:nowrap">{mod_content}</td>'
            + f'<td style="text-align:center;color:{lat_fg};font-weight:500">{html.escape(latency)}</td>'
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
  <title>{source_label} vs D365 Row Count Report</title>
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
  <h1>{source_label} vs Dynamics 365 Row Count Comparison</h1>
  <div class="meta">
    Generated <b>{generated}</b><br>
    {source_label}: <code>{html.escape(src_endpoint)}</code> / <code>{html.escape(src_database)}</code><br>
    D365: <code>{html.escape(cfg.d365_uri or '')}</code> &nbsp; Tables: <b>{total}</b>
  </div>
  <div class="summary">{summary_chips}</div>
  <input id="filter" type="search" placeholder="Filter by table name or status..." oninput="filterRows(this.value)">
  <table id="grid">
    <thead>
      <tr>
        <th onclick="sortTable(0)">{source_label} Table</th>
        <th onclick="sortTable(1)">D365 Table</th>
        <th class="right" onclick="sortTable(2, true)">{source_label} Rows</th>
        <th class="right" onclick="sortTable(3, true)">{source_label} RowVersion</th>
        <th class="center" onclick="sortTable(4)">{source_label} Last Modified</th>
        <th class="right" onclick="sortTable(5, true)">D365 Rows</th>
        <th class="right" onclick="sortTable(6, true)">D365 RowVersion</th>
        <th class="center" onclick="sortTable(7)">D365 Last Modified</th>
        <th class="center" onclick="sortTable(8)">Running Latency</th>
        <th class="right" onclick="sortTable(9, true)">Delta</th>
        <th class="center" onclick="sortTable(10)">Status</th>
        <th class="center" onclick="sortTable(11)">Last SinkModifiedOn</th>
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
    is_synapse = cfg.source_type == "synapse"
    source_label = "Synapse" if is_synapse else "Fabric"
    src_endpoint = cfg.synapse_endpoint if is_synapse else cfg.endpoint
    src_database = cfg.synapse_database if is_synapse else cfg.database
    src_auth = cfg.synapse_auth_mode if is_synapse else cfg.auth_mode
    effective_mode = "exact (forced — SQL Serverless)" if is_synapse else cfg.count_mode

    print(f"Connecting to {source_label} {src_endpoint} / {src_database} "
          f"(auth={src_auth}, mode={effective_mode})")

    connect_fn = connect_synapse if is_synapse else connect_fabric
    with connect_fn(cfg) as conn:
        # Synapse SQL Serverless does not expose sys.partitions; always use exact mode.
        if cfg.count_mode == "exact" or is_synapse:
            fabric_rows = count_fabric_exact(conn, cfg.tables)
        else:
            fabric_rows = count_fabric_fast(conn, cfg.tables)

    if cfg.skip_d365 or not cfg.d365_uri:
        if not cfg.skip_d365:
            print("\n[D365_URI not set - skipping D365 comparison]")
        width = max((len(name) for name, *_ in fabric_rows), default=10)
        print(f"\n{'Table'.ljust(width)}  {f'{source_label} Rows':>15}  {'Last SinkModifiedOn':>20}")
        print(f"{'-' * width}  {'-' * 15}  {'-' * 20}")
        for name, n, sink, _srv, _mod, f_err in fabric_rows:
            n_disp = "N/A (not found)" if f_err == "NOT_FOUND" else (f"{n:,}" if n is not None else "Error")
            print(f"{name.ljust(width)}  {n_disp:>15}  {(sink or '-'):>20}")
        print(f"\n{len(fabric_rows)} table(s) reported.")
        return 0

    print(f"\nAuthenticating to D365 at {cfg.d365_uri} (auth={cfg.d365_auth_mode}, service={cfg.d365_service_version})")
    d365_token = get_d365_token(cfg)
    session = requests.Session()

    use_batch = cfg.d365_service_version == "v3"
    batch_counts: dict[str, int] = {}
    if use_batch:
        d365_names = [fabric_to_d365_name(fabric_name, cfg.table_name_map) for fabric_name, *_ in fabric_rows]
        batch_counts, batch_err = d365_count_batch(session, cfg.d365_uri, d365_token, d365_names)
        if batch_err:
            print(f"[WARN] Bulk row count retrieval failed, falling back to per-table counts: {batch_err}", file=sys.stderr, flush=True)
            use_batch = False

    rows: list[tuple] = []
    for fabric_name, fabric_count, sink, fabric_srv, fabric_mod, fabric_err in fabric_rows:
        d365_name = fabric_to_d365_name(fabric_name, cfg.table_name_map)
        if use_batch:
            # Counts came from the single bulk getTableRowCounts call (catalog-based,
            # no per-table scan); only metadata (rowversion/modified) is fetched here.
            err, not_found, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est = d365_metadata(
                session, cfg.d365_uri, d365_token, d365_name,
                fabric_srv=fabric_srv, fabric_mod=fabric_mod,
            )
            key = d365_name.lower()
            if not_found or key not in batch_counts:
                d365_count_val, not_found = None, True
            else:
                d365_count_val = batch_counts[key]
        else:
            d365_count_val, err, not_found, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est = d365_count(
                session, cfg.d365_uri, d365_token, d365_name, cfg.d365_service_version,
                fabric_srv=fabric_srv, fabric_mod=fabric_mod,
            )
        status, delta = classify(fabric_count, d365_count_val, not_found, err, fabric_err)
        fabric_last_mod = fabric_mod or fabric_synced_mod
        is_fabric_mod_est = fabric_mod is None and fabric_synced_mod is not None
        latency = compute_latency(last_mod, fabric_mod, fabric_synced_mod, sink, status)
        rows.append((fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink,
                     last_srv, last_mod, is_est, latency, fabric_srv, fabric_last_mod, is_fabric_mod_est))

    # Render table
    headers = (f"{source_label} Table", "D365 Table", f"{source_label} Rows", f"{source_label} RowVersion", f"{source_label} Last Modified",
               "D365 Rows", "D365 RowVersion", "D365 Last Modified", "Running Latency", "Delta", "Status", "Last SinkModifiedOn")
    cols = list(zip(*([headers] + [
        (n, d, f"{f:,}" if f is not None else "Error",
         "-" if fab_srv is None else str(fab_srv),
         (f"{fab_mod} (est.)" if fab_est else fab_mod) if fab_mod else "-",
         f"{c:,}" if c is not None else "-",
         "-" if srv is None else str(srv),
         (f"{mod} (est.)" if is_est else mod) if mod else "-",
         lat, str(delta), status, sink or "-")
        for n, d, f, c, delta, status, sink, srv, mod, is_est, lat, fab_srv, fab_mod, fab_est in rows
    ])))
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print()
    print(fmt.format(*headers).rstrip())
    print(fmt.format(*["-" * w for w in widths]).rstrip())
    for n, d, f, c, delta, status, sink, srv, mod, is_est, lat, fab_srv, fab_mod, fab_est in rows:
        mod_disp     = (f"{mod} (est.)" if is_est else mod) if mod else "-"
        fab_mod_disp = (f"{fab_mod} (est.)" if fab_est else fab_mod) if fab_mod else "-"
        print(fmt.format(n, d, f"{f:,}" if f is not None else "Error",
                         "-" if fab_srv is None else str(fab_srv), fab_mod_disp,
                         f"{c:,}" if c is not None else "-",
                         "-" if srv is None else str(srv), mod_disp,
                         lat, str(delta), status, sink or "-").rstrip())

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
        path = render_html_report(cfg, rows, counts, html_path, source_label)
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
        w.writerow(["Fabric Table", "D365 Table",
                    "Fabric Rows", "Fabric RowVersion", "Fabric Last Modified", "Fabric Mod Estimated",
                    "D365 Rows", "D365 RowVersion", "D365 Last Modified", "D365 Estimated",
                    "Running Latency", "Delta", "Status", "Last SinkModifiedOn"])
        for fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink, last_srv, last_mod, is_est, latency, fabric_srv, fabric_last_mod, is_fabric_mod_est in rows:
            w.writerow([
                fabric_name, d365_name,
                fabric_count,
                fabric_srv if fabric_srv is not None else "",
                fabric_last_mod or "",
                "Yes" if is_fabric_mod_est else "No",
                d365_count_val if d365_count_val is not None else "",
                last_srv if last_srv is not None else "",
                last_mod or "",
                "Yes" if is_est else "No",
                latency, delta, status, sink or "",
            ])


if __name__ == "__main__":
    sys.exit(main())
