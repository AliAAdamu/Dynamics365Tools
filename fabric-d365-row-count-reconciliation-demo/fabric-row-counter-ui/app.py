"""
Fabric Row Counter — Streamlit UI.

Wraps the existing count_rows.py logic in a friendly web form. The original
CLI in ../fabric-row-counter is left untouched and is imported as a library.
"""

from __future__ import annotations

import io
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# Import the existing CLI as a library (no code duplication).
# Uses importlib to load directly from the .py file path, bypassing Python's
# module cache (.pyc) so the latest source is always used.
import importlib.util as _ilu

THIS_DIR = Path(__file__).parent.resolve()
_cr_path = None
for candidate in (THIS_DIR, THIS_DIR.parent / "fabric-row-counter"):
    if (candidate / "count_rows.py").exists():
        _cr_path = candidate / "count_rows.py"
        break

if _cr_path is None:
    raise FileNotFoundError("count_rows.py not found")

_spec = _ilu.spec_from_file_location("count_rows", str(_cr_path))
cr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(cr)  # type: ignore

STATUS_COLORS = {
    "Match":   ("#0f5132", "#d1e7dd"),
    "Drift":   ("#664d03", "#fff3cd"),
    "Anomaly": ("#842029", "#f8d7da"),
    "N/A":     ("#41464b", "#e2e3e5"),
    "Error":   ("#ffffff", "#dc3545"),
}

st.set_page_config(page_title="Fabric Row Counter", page_icon="📊", layout="wide")
st.title("📊 Fabric ↔ Dynamics 365 Row Counter")
_ver = getattr(cr, "APP_VERSION", "legacy")
st.caption(f"Compare Fabric Warehouse row counts to the source D365 F&O environment. &nbsp; `v{_ver}`")


# ---------------------------------------------------------------------------
# Sidebar — connection settings
# ---------------------------------------------------------------------------
st.sidebar.header("⚙️ Settings")
st.sidebar.caption(f"App version **{_ver}** | loaded from `{_cr_path.name}`")

with st.sidebar.expander("Fabric", expanded=True):
    fabric_endpoint = st.text_input(
        "SQL endpoint",
        value=os.getenv("FABRIC_SQL_ENDPOINT", ""),
        help="e.g. xxxxxx.datawarehouse.fabric.microsoft.com",
    )
    fabric_database = st.text_input("Database / Warehouse name", value=os.getenv("FABRIC_DATABASE", ""))
    fabric_auth = st.selectbox("Auth mode", ["interactive", "cli", "serviceprincipal"], index=0)
    count_mode = st.selectbox(
        "Count mode",
        ["exact", "fast"],
        index=0,
        help="exact = COUNT_BIG(*) (Fabric Warehouse). fast = sys.partitions metadata.",
    )

with st.sidebar.expander("Dynamics 365 F&O", expanded=True):
    skip_d365 = st.checkbox("Skip D365 (Fabric only)", value=False)
    d365_uri = st.text_input("D365 base URI", value=os.getenv("D365_URI", ""), disabled=skip_d365)
    d365_auth = st.selectbox("Auth mode", ["interactive", "serviceprincipal"], index=0, disabled=skip_d365)
    service_version = st.radio(
        "Service version",
        ["v1", "v2"],
        index=0,
        horizontal=True,
        format_func=lambda v: {"v1": "v1 — Direct SQL (faster, default)",
                                "v2": "v2 — X++ (edge-case fallback)"}[v],
        disabled=skip_d365,
        help="v1 calls getTableRecordCount (default). v2 calls getTableRecordCountV2 — use only when v1 and Fabric counts disagree and you need to rule out orphan-row noise.",
    )

with st.sidebar.expander("Service principal (optional)", expanded=False):
    sp_tenant = st.text_input("Tenant ID", value=os.getenv("AZURE_TENANT_ID", ""))
    sp_client = st.text_input("Client ID", value=os.getenv("AZURE_CLIENT_ID", ""))
    sp_secret = st.text_input("Client secret", value=os.getenv("AZURE_CLIENT_SECRET", ""), type="password")

with st.sidebar.expander("Tables & mapping", expanded=False):
    tables_text = st.text_area(
        "Tables (comma or newline separated, blank = all user tables)",
        value=os.getenv("FABRIC_TABLES", ""),
        height=120,
    )
    map_text = st.text_input(
        "Name overrides (Fabric=D365, comma-separated)",
        value=os.getenv("TABLE_NAME_MAP", ""),
        help="e.g. dbo.systemuser=USERINFO,dbo.bot=BOTTABLE",
    )


# ---------------------------------------------------------------------------
# Build a cr.Config from the form
# ---------------------------------------------------------------------------
def build_config() -> cr.Config:
    raw_tables = tables_text.replace("\n", ",")
    tables = [t.strip() for t in raw_tables.split(",") if t.strip()]
    return cr.Config(
        endpoint=fabric_endpoint.strip(),
        database=fabric_database.strip(),
        tables=tables,
        auth_mode=fabric_auth,
        tenant_id=sp_tenant.strip() or None,
        client_id=sp_client.strip() or None,
        client_secret=sp_secret.strip() or None,
        count_mode=count_mode,
        d365_uri=(d365_uri.strip().rstrip("/") or None) if not skip_d365 else None,
        d365_auth_mode=d365_auth,
        d365_tenant_id=sp_tenant.strip() or None,
        d365_client_id=sp_client.strip() or None,
        d365_client_secret=sp_secret.strip() or None,
        table_name_map=cr._parse_map(map_text),
        skip_d365=skip_d365,
        html_report=None,
        html_open=False,
        csv_report=None,
        d365_service_version=service_version,
    )


# ---------------------------------------------------------------------------
# Run logic with progress bar
# ---------------------------------------------------------------------------
def run_comparison(cfg: cr.Config):
    progress = st.progress(0.0, text="Connecting to Fabric…")
    status_box = st.empty()

    with cr.connect_fabric(cfg) as conn:
        progress.progress(0.1, text="Counting rows in Fabric…")
        if cfg.count_mode == "exact":
            fabric_rows = cr.count_fabric_exact(conn, cfg.tables)
        else:
            fabric_rows = cr.count_fabric_fast(conn, cfg.tables)

    if cfg.skip_d365 or not cfg.d365_uri:
        progress.progress(1.0, text="Done (Fabric only)")
        return [
            (name, "-", n, None, "-", "N/A", sink, None, None, False, "N/A",
             fab_srv, fab_mod, False)
            for name, n, sink, fab_srv, fab_mod in fabric_rows
        ]

    progress.progress(0.4, text="Authenticating to D365…")
    token = cr.get_d365_token(cfg)
    session = requests.Session()

    rows = []
    total = len(fabric_rows)
    for i, (fabric_name, fabric_count, sink, fabric_srv, fabric_mod) in enumerate(fabric_rows, 1):
        d365_name = cr.fabric_to_d365_name(fabric_name, cfg.table_name_map)
        status_box.write(f"Querying D365 for **{d365_name}** ({i}/{total})…")
        d365_count_val, err, not_found, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est = cr.d365_count(
            session, cfg.d365_uri, token, d365_name, cfg.d365_service_version,
            fabric_srv=fabric_srv, fabric_mod=fabric_mod,
        )
        status, delta = cr.classify(fabric_count, d365_count_val, not_found, err)
        fabric_last_mod = fabric_mod or fabric_synced_mod
        is_fabric_mod_est = fabric_mod is None and fabric_synced_mod is not None
        latency = cr.compute_latency(last_mod, fabric_mod, fabric_synced_mod)
        rows.append((fabric_name, d365_name, fabric_count, d365_count_val, delta, status, sink,
                     last_srv, last_mod, is_est, latency, fabric_srv, fabric_last_mod, is_fabric_mod_est))
        progress.progress(0.4 + 0.6 * i / total, text=f"D365 lookup {i}/{total}")

    status_box.empty()
    progress.progress(1.0, text="Done")
    return rows


# ---------------------------------------------------------------------------
# Render results
# ---------------------------------------------------------------------------
def style_status(val: str) -> str:
    fg, bg = STATUS_COLORS.get(val, ("#000", "#eee"))
    return f"background-color: {bg}; color: {fg}; font-weight: 600; text-align: center;"


def render_results(rows):
    df = pd.DataFrame(rows, columns=[
        "Fabric Table", "D365 Table", "Fabric Rows", "D365 Rows",
        "Delta", "Status", "Last SinkModifiedOn",
        "D365 RowVersion", "D365 Last Modified", "D365 Estimated", "Latency",
        "Fabric RowVersion", "Fabric Last Modified", "Fabric Mod Estimated",
    ])
    # Merge estimated flags into datetime strings for display
    df["D365 Last Modified"] = df.apply(
        lambda r: (
            f'{r["D365 Last Modified"]} (est.)' if r["D365 Estimated"] and r["D365 Last Modified"]
            else (r["D365 Last Modified"] or "—")
        ),
        axis=1,
    )
    df["Fabric Last Modified"] = df.apply(
        lambda r: (
            f'{r["Fabric Last Modified"]} (est.)' if r["Fabric Mod Estimated"] and r["Fabric Last Modified"]
            else (r["Fabric Last Modified"] or "—")
        ),
        axis=1,
    )

    # Summary chips
    counts = df["Status"].value_counts().to_dict()
    cols = st.columns(5)
    for i, key in enumerate(["Match", "Drift", "Anomaly", "N/A", "Error"]):
        fg, bg = STATUS_COLORS[key]
        with cols[i]:
            st.markdown(
                f"<div style='padding:10px;border-radius:8px;background:{bg};color:{fg};"
                f"text-align:center;font-weight:600;'>{key}<br/>"
                f"<span style='font-size:1.6em'>{counts.get(key, 0)}</span></div>",
                unsafe_allow_html=True,
            )

    st.divider()

    # Filter
    flt = st.multiselect(
        "Filter by status",
        options=["Match", "Drift", "Anomaly", "N/A", "Error"],
        default=[],
    )
    base = df if not flt else df[df["Status"].isin(flt)]
    display_cols = [
        "Fabric Table", "D365 Table",
        "Fabric Rows", "Fabric RowVersion", "Fabric Last Modified",
        "D365 Rows", "D365 RowVersion", "D365 Last Modified",
        "Latency", "Delta", "Status", "Last SinkModifiedOn",
    ]
    view = base[display_cols].copy()
    # Pre-format numeric columns as strings to avoid "None" display in newer Streamlit/pandas.
    def _fi(v, comma=False) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "—"
        return f"{int(v):,}" if comma else str(int(v))
    view["Fabric Rows"]       = view["Fabric Rows"].apply(lambda v: _fi(v, comma=True))
    view["Fabric RowVersion"] = view["Fabric RowVersion"].apply(_fi)
    view["D365 Rows"]         = view["D365 Rows"].apply(lambda v: _fi(v, comma=True))
    view["D365 RowVersion"]   = view["D365 RowVersion"].apply(_fi)

    styled = view.style.map(style_status, subset=["Status"])
    st.dataframe(styled, use_container_width=True, height=560)

    # Downloads
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)
    html_path = cr.timestamped_path("report.html", ts)
    cfg = build_config()
    cfg.html_report = html_path
    summary_counts = {k: counts.get(k, 0) for k in ["Match", "Drift", "Anomaly", "N/A", "Error"]}
    html_doc_path = cr.render_html_report(cfg, rows, summary_counts, html_path)
    with open(html_doc_path, "r", encoding="utf-8") as f:
        html_bytes = f.read().encode("utf-8")

    c1, c2 = st.columns(2)
    c1.download_button(
        "⬇️ Download CSV",
        data=csv_buf.getvalue(),
        file_name=f"fabric_row_counts_{ts}.csv",
        mime="text/csv",
    )
    c2.download_button(
        "⬇️ Download HTML report",
        data=html_bytes,
        file_name=f"fabric_row_counts_{ts}.html",
        mime="text/html",
    )

    # Cleanup the temp html written to disk
    try:
        os.remove(html_doc_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
left, right = st.columns([3, 1])
with right:
    run = st.button("▶️ Run comparison", type="primary", use_container_width=True)

if run:
    if not fabric_endpoint or not fabric_database:
        st.error("Fabric SQL endpoint and database are required.")
    else:
        try:
            cfg = build_config()
            t0 = time.time()
            rows = run_comparison(cfg)
            st.success(f"Completed in {time.time() - t0:.1f}s — {len(rows)} table(s).")
            render_results(rows)
        except Exception as e:
            st.exception(e)
else:
    st.info("Fill in the connection settings in the sidebar and click **Run comparison**.")
