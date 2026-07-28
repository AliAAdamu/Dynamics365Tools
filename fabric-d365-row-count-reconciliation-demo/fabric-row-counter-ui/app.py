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
sys.modules["count_rows"] = cr  # must be registered before exec so Python 3.14 dataclasses can resolve the module
_spec.loader.exec_module(cr)  # type: ignore

STATUS_COLORS = {
    "Match":   ("#0f5132", "#d1e7dd"),
    "Drift":   ("#664d03", "#fff3cd"),
    "Anomaly": ("#842029", "#f8d7da"),
    "N/A":     ("#41464b", "#e2e3e5"),
    "Error":   ("#ffffff", "#dc3545"),
}

st.set_page_config(page_title="Fabric Row Counter", page_icon="📊", layout="wide")
st.title("📊 D365 Data Replication Reconciliation Tool")
_ver = getattr(cr, "APP_VERSION", "legacy")
st.caption(f"Compare **Fabric Link** or **Synapse Link** row counts to the source D365 F&O environment. &nbsp; `v{_ver}`")


# ---------------------------------------------------------------------------
# Sidebar — connection settings
# ---------------------------------------------------------------------------
st.sidebar.header("⚙️ Settings")
st.sidebar.caption(f"App version **{_ver}** | loaded from `{_cr_path.name}`")

source_type = st.sidebar.radio(
    "Data source",
    ["Fabric Link", "Synapse Link", "Both"],
    index=0,
    horizontal=True,
    help=(
        "**Fabric Link** — Fabric Warehouse / Lakehouse SQL endpoint.\n\n"
        "**Synapse Link** — Azure Synapse Analytics SQL Serverless endpoint.\n\n"
        "**Both** — run against both and compare results side by side."
    ),
)
_src = (
    "fabric_link" if source_type == "Fabric Link"
    else ("synapse_link" if source_type == "Synapse Link" else "both")
)

with st.sidebar.expander("Fabric Link", expanded=_src in ("fabric_link", "both")):
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

with st.sidebar.expander("Synapse Link (SQL Serverless)", expanded=_src in ("synapse_link", "both")):
    synapse_endpoint = st.text_input(
        "SQL Serverless endpoint",
        value=os.getenv("SYNAPSE_SQL_ENDPOINT", ""),
        help="e.g. myworkspace-ondemand.sql.azuresynapse.net",
    )
    synapse_database = st.text_input(
        "Lake database name",
        value=os.getenv("SYNAPSE_DATABASE", ""),
        help="Database created by Synapse Link for Dataverse / D365 Finance.",
    )
    synapse_auth = st.selectbox(
        "Auth mode",
        ["interactive", "cli", "serviceprincipal"],
        index=0,
        key="synapse_auth_mode",
    )
    st.caption(
        "ℹ️ Only **exact** count mode (`COUNT_BIG`) is supported on "
        "Synapse SQL Serverless. The *fast* mode (`sys.partitions`) is not available."
    )

with st.sidebar.expander("Dynamics 365 F&O", expanded=True):
    skip_d365 = st.checkbox("Skip D365 (Fabric only)", value=False)
    d365_uri = st.text_input("D365 base URI", value=os.getenv("D365_URI", ""), disabled=skip_d365)
    d365_auth = st.selectbox("Auth mode", ["interactive", "serviceprincipal"], index=0, disabled=skip_d365)
    same_credentials = st.checkbox(
        "Use same sign-in for Fabric/Synapse and D365",
        value=os.getenv("SAME_CREDENTIALS", "true").lower() in ("1", "true", "yes"),
        disabled=skip_d365,
        help=(
            "Checked (default): one interactive sign-in is reused for both the Fabric/Synapse "
            "SQL endpoint and D365 — only one browser popup. Uncheck if the Fabric/Synapse "
            "endpoint and the D365 environment need different user accounts — a separate "
            "sign-in prompt will appear for D365."
        ),
    )
    service_version = st.radio(
        "Service version",
        ["v3", "v1", "v2"],
        index=0,
        horizontal=True,
        format_func=lambda v: {"v1": "v1 — Direct SQL (per-table)",
                                "v2": "v2 — X++ (edge-case fallback)",
                                "v3": "v3 — Bulk catalog counts (fastest, default)"}[v],
        disabled=skip_d365,
        help="v3 fetches counts for ALL tables in one bulk call (getTableRowCounts) and only queries per-table metadata — fastest, especially when reconciling many tables (default). v1 calls getTableRecordCount (direct SQL, per-table). v2 calls getTableRecordCountV2 — use only when counts disagree with Fabric and you need to rule out orphan-row noise.",
    )

with st.sidebar.expander("Service principal (optional)", expanded=False):
    sp_tenant = st.text_input("Tenant ID", value=os.getenv("AZURE_TENANT_ID", ""))
    sp_client = st.text_input("Client ID", value=os.getenv("AZURE_CLIENT_ID", ""))
    sp_secret = st.text_input("Client secret", value=os.getenv("AZURE_CLIENT_SECRET", ""), type="password")
    if _src in ("synapse_link", "both"):
        st.caption("Synapse Link SP — leave blank to reuse the credentials above.")
        syn_sp_tenant = st.text_input("Synapse Tenant ID", value=os.getenv("SYNAPSE_TENANT_ID", ""))
        syn_sp_client = st.text_input("Synapse Client ID", value=os.getenv("SYNAPSE_CLIENT_ID", ""))
        syn_sp_secret = st.text_input("Synapse Client secret", value=os.getenv("SYNAPSE_CLIENT_SECRET", ""), type="password")
    else:
        syn_sp_tenant = syn_sp_client = syn_sp_secret = ""

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
def build_config(src: str = "fabric") -> cr.Config:
    """Build a Config for one source: 'fabric' or 'synapse'."""
    raw_tables = tables_text.replace("\n", ",")
    tables = [t.strip() for t in raw_tables.split(",") if t.strip()]
    # Synapse SP fields fall back to the shared SP credentials when left blank.
    _syn_tenant = (syn_sp_tenant.strip() or sp_tenant.strip()) or None
    _syn_client = (syn_sp_client.strip() or sp_client.strip()) or None
    _syn_secret = (syn_sp_secret.strip() or sp_secret.strip()) or None
    return cr.Config(
        source_type=src,
        endpoint=fabric_endpoint.strip(),
        database=fabric_database.strip(),
        tables=tables,
        auth_mode=fabric_auth,
        tenant_id=sp_tenant.strip() or None,
        client_id=sp_client.strip() or None,
        client_secret=sp_secret.strip() or None,
        count_mode=count_mode,
        synapse_endpoint=synapse_endpoint.strip() or None,
        synapse_database=synapse_database.strip() or None,
        synapse_auth_mode=synapse_auth,
        synapse_tenant_id=_syn_tenant,
        synapse_client_id=_syn_client,
        synapse_client_secret=_syn_secret,
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
        same_credentials=same_credentials,
    )


# ---------------------------------------------------------------------------
# Run logic with progress bar
# ---------------------------------------------------------------------------
def run_comparison(cfg: cr.Config):
    is_synapse = cfg.source_type == "synapse"
    source_label = "Synapse" if is_synapse else "Fabric"
    progress = st.progress(0.0, text=f"Connecting to {source_label}…")
    status_box = st.empty()

    connect_fn = cr.connect_synapse if is_synapse else cr.connect_fabric
    with connect_fn(cfg) as conn:
        progress.progress(0.1, text=f"Retrieving info from {source_label}…")
        # Synapse SQL Serverless does not expose sys.partitions; always use exact mode.
        if cfg.count_mode == "exact" or is_synapse:
            fabric_rows = cr.count_fabric_exact(conn, cfg.tables)
        else:
            fabric_rows = cr.count_fabric_fast(conn, cfg.tables)

    if cfg.skip_d365 or not cfg.d365_uri:
        progress.progress(1.0, text="Done (source only)")
        return [
            (name, "-", n, None, "-", "N/A", sink, None, None, False, "N/A",
             fab_srv, fab_mod, False)
            for name, n, sink, fab_srv, fab_mod, f_err in fabric_rows
        ]

    progress.progress(0.4, text="Authenticating to D365…")
    token = cr.get_d365_token(cfg)
    session = requests.Session()

    use_batch = cfg.d365_service_version == "v3"
    batch_counts: dict[str, int] = {}
    if use_batch:
        status_box.write("Fetching row counts for all tables in one bulk call…")
        d365_names = [cr.fabric_to_d365_name(name, cfg.table_name_map) for name, *_ in fabric_rows]
        batch_counts, batch_err = cr.d365_count_batch(session, cfg.d365_uri, token, d365_names)
        if batch_err:
            st.warning(f"Bulk row count retrieval failed, falling back to per-table counts: {batch_err}")
            use_batch = False

    rows = []
    total = len(fabric_rows)
    for i, (fabric_name, fabric_count, sink, fabric_srv, fabric_mod, fabric_err) in enumerate(fabric_rows, 1):
        d365_name = cr.fabric_to_d365_name(fabric_name, cfg.table_name_map)
        status_box.write(f"Querying D365 for **{d365_name}** ({i}/{total})…")
        if use_batch:
            err, not_found, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est = cr.d365_metadata(
                session, cfg.d365_uri, token, d365_name,
                fabric_srv=fabric_srv, fabric_mod=fabric_mod,
            )
            key = d365_name.lower()
            if not_found or key not in batch_counts:
                d365_count_val, not_found = None, True
            else:
                d365_count_val = batch_counts[key]
        else:
            d365_count_val, err, not_found, last_srv, last_mod, is_est, fabric_synced_mod, is_fabric_sync_est = cr.d365_count(
                session, cfg.d365_uri, token, d365_name, cfg.d365_service_version,
                fabric_srv=fabric_srv, fabric_mod=fabric_mod,
            )
        status, delta = cr.classify(fabric_count, d365_count_val, not_found, err, fabric_err)
        fabric_last_mod = fabric_mod or fabric_synced_mod
        is_fabric_mod_est = fabric_mod is None and fabric_synced_mod is not None
        latency = cr.compute_latency(last_mod, fabric_mod, fabric_synced_mod, sink, status)
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


def render_results(rows, source_label: str = "Fabric"):
    src = source_label
    df = pd.DataFrame(rows, columns=[
        f"{src} Table", "D365 Table", f"{src} Rows", "D365 Rows",
        "Delta", "Status", "Last SinkModifiedOn",
        "D365 RowVersion", "D365 Last Modified", "D365 Estimated", "Running Latency",
        f"{src} RowVersion", f"{src} Last Modified", f"{src} Mod Estimated",
    ])
    # Merge estimated flags into datetime strings for display
    df["D365 Last Modified"] = df.apply(
        lambda r: (
            f'{r["D365 Last Modified"]} (est.)' if r["D365 Estimated"] and r["D365 Last Modified"]
            else (r["D365 Last Modified"] or "—")
        ),
        axis=1,
    )
    df[f"{src} Last Modified"] = df.apply(
        lambda r, s=src: (
            f'{r[f"{s} Last Modified"]} (est.)' if r[f"{s} Mod Estimated"] and r[f"{s} Last Modified"]
            else (r[f"{s} Last Modified"] or "—")
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
        key=f"filter_status_{source_label.lower()}",
    )
    base = df if not flt else df[df["Status"].isin(flt)]
    display_cols = [
        f"{src} Table", "D365 Table",
        f"{src} Rows", f"{src} RowVersion", f"{src} Last Modified",
        "D365 Rows", "D365 RowVersion", "D365 Last Modified",
        "Running Latency", "Delta", "Status", "Last SinkModifiedOn",
    ]
    view = base[display_cols].copy()
    # Keep numeric columns as nullable ints (not strings) so clicking a column
    # header sorts numerically instead of lexicographically. Comma formatting
    # is applied via column_config (NumberColumn), not by pre-stringifying.
    def _to_int(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return pd.NA
        return int(v)
    for col in (f"{src} Rows", f"{src} RowVersion", "D365 Rows", "D365 RowVersion"):
        view[col] = view[col].apply(_to_int).astype("Int64")

    styled = view.style.map(style_status, subset=["Status"])
    st.dataframe(
        styled,
        width="stretch",
        height=560,
        column_config={
            f"{src} Rows": st.column_config.NumberColumn(format="%,d"),
            f"{src} RowVersion": st.column_config.NumberColumn(format="%d"),
            "D365 Rows": st.column_config.NumberColumn(format="%,d"),
            "D365 RowVersion": st.column_config.NumberColumn(format="%d"),
        },
    )

    # Downloads
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_buf = io.StringIO()
    df.to_csv(csv_buf, index=False)
    html_path = cr.timestamped_path("report.html", ts)
    cfg = build_config("synapse" if source_label == "Synapse" else "fabric")
    cfg.html_report = html_path
    summary_counts = {k: counts.get(k, 0) for k in ["Match", "Drift", "Anomaly", "N/A", "Error"]}
    html_doc_path = cr.render_html_report(cfg, rows, summary_counts, html_path, source_label)
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
if "results" not in st.session_state:
    st.session_state["results"] = None

left, right = st.columns([3, 1])
with right:
    run = st.button("▶️ Run comparison", type="primary", width="stretch")

if run:
    try:
        if _src == "fabric_link":
            if not fabric_endpoint or not fabric_database:
                st.error("Fabric SQL endpoint and database are required.")
                st.stop()
            cfg = build_config("fabric")
            t0 = time.time()
            rows = run_comparison(cfg)
            st.session_state["results"] = {
                "mode": "single", "label": "Fabric", "rows": rows,
                "elapsed": time.time() - t0,
            }

        elif _src == "synapse_link":
            if not synapse_endpoint or not synapse_database:
                st.error("Synapse SQL Serverless endpoint and database are required.")
                st.stop()
            cfg = build_config("synapse")
            t0 = time.time()
            rows = run_comparison(cfg)
            st.session_state["results"] = {
                "mode": "single", "label": "Synapse", "rows": rows,
                "elapsed": time.time() - t0,
            }

        else:  # both
            errors = []
            if not fabric_endpoint or not fabric_database:
                errors.append("Fabric SQL endpoint and database are required for the **Fabric Link** comparison.")
            if not synapse_endpoint or not synapse_database:
                errors.append("Synapse SQL Serverless endpoint and database are required for the **Synapse Link** comparison.")
            if errors:
                for msg in errors:
                    st.error(msg)
                st.stop()
            t0 = time.time()
            cfg_fab = build_config("fabric")
            rows_fab = run_comparison(cfg_fab)
            cfg_syn = build_config("synapse")
            rows_syn = run_comparison(cfg_syn)
            st.session_state["results"] = {
                "mode": "both", "rows_fab": rows_fab, "rows_syn": rows_syn,
                "elapsed": time.time() - t0,
            }

    except Exception as e:
        st.session_state["results"] = None
        st.exception(e)

# Render from session_state rather than only inside `if run:` — Streamlit
# reruns the whole script on every widget interaction (e.g. the results
# filter or a download button), and `run` is only True on the exact rerun
# where the button was clicked. Without this, any later widget interaction
# would skip the `if run:` block and wipe the just-computed results.
results = st.session_state.get("results")
if results is None:
    st.info("Fill in the connection settings in the sidebar and click **Run comparison**.")
elif results["mode"] == "single":
    st.success(f"Completed in {results['elapsed']:.1f}s — {len(results['rows'])} table(s).")
    render_results(results["rows"], results["label"])
else:
    tab_fab, tab_syn = st.tabs(["Fabric Link", "Synapse Link"])
    with tab_fab:
        st.subheader("Fabric Link ↔ Dynamics 365")
        st.success(f"{len(results['rows_fab'])} table(s) queried.")
        render_results(results["rows_fab"], "Fabric")
    with tab_syn:
        st.subheader("Synapse Link ↔ Dynamics 365")
        st.success(f"{len(results['rows_syn'])} table(s) queried.")
        render_results(results["rows_syn"], "Synapse")
    st.caption(f"Both comparisons completed in {results['elapsed']:.1f}s.")
