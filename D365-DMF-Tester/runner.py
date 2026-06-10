"""Test runner — executes a test plan N times in parallel or serial."""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from auth import get_access_token, AuthError
from config_manager import get_environment, get_plan, get_client_secret, save_result
from dmf_client import DMFClient, DMFError, unique_blob_name

_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()

MAX_PARALLEL_WORKERS = 20


def get_run_status(run_id: str) -> Optional[dict]:
    with _runs_lock:
        return dict(_runs[run_id]) if run_id in _runs else None


def list_active_runs() -> list[dict]:
    with _runs_lock:
        return [
            {k: v for k, v in r.items() if k != "executions"}
            for r in _runs.values()
            if r["status"] in ("initializing", "running")
        ]


def start_run(plan_id: str, env_id: str, iterations: int, mode: str) -> str:
    """Kick off a background test run and return its run_id."""
    run_id = str(uuid.uuid4())
    state: dict = {
        "id": run_id,
        "plan_id": plan_id,
        "env_id": env_id,
        "iterations": iterations,
        "mode": mode,
        "status": "initializing",
        "started_at": _now(),
        "completed_at": None,
        "executions": [],
        "summary": {},
        "error": None,
    }
    with _runs_lock:
        _runs[run_id] = state

    t = threading.Thread(target=_execute_run, args=(run_id,), daemon=True)
    t.start()
    return run_id


def _set(run_id: str, **kwargs) -> None:
    with _runs_lock:
        _runs[run_id].update(kwargs)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_execution(run_id: str, exec_record: dict) -> None:
    with _runs_lock:
        _runs[run_id]["executions"].append(exec_record)


def _execute_run(run_id: str) -> None:
    with _runs_lock:
        state = dict(_runs[run_id])

    plan = get_plan(state["plan_id"])
    env = get_environment(state["env_id"])

    if not plan:
        _set(run_id, status="failed", error="Test plan not found", completed_at=_now())
        return
    if not env:
        _set(run_id, status="failed", error="Environment not found", completed_at=_now())
        return

    try:
        secret = get_client_secret(env)
        token = get_access_token(env["tenant_id"], env["client_id"], secret, env["base_url"])
    except AuthError as exc:
        _set(run_id, status="failed", error=f"Auth failed: {exc}", completed_at=_now())
        return
    except Exception as exc:  # noqa: BLE001
        _set(run_id, status="failed", error=str(exc), completed_at=_now())
        return

    _set(run_id, status="running")
    iterations = state["iterations"]
    mode = state["mode"]

    try:
        if mode == "parallel":
            workers = min(iterations, MAX_PARALLEL_WORKERS)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_single_execution, run_id, idx, plan, env, token): idx
                    for idx in range(iterations)
                }
                for future in as_completed(futures):
                    rec = future.result()
                    _add_execution(run_id, rec)
        else:
            for idx in range(iterations):
                rec = _single_execution(run_id, idx, plan, env, token)
                _add_execution(run_id, rec)
    except Exception as exc:  # noqa: BLE001
        _set(run_id, status="failed", error=str(exc), completed_at=_now())
        return

    with _runs_lock:
        executions = list(_runs[run_id]["executions"])

    succeeded = [e for e in executions if e["status"] == "success"]
    failed = [e for e in executions if e["status"] == "failed"]
    durations = [e["duration_ms"] for e in executions if e.get("duration_ms") is not None]

    summary = {
        "total": iterations,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "avg_duration_ms": int(sum(durations) / len(durations)) if durations else 0,
        "min_duration_ms": min(durations, default=0),
        "max_duration_ms": max(durations, default=0),
    }

    completed_at = _now()
    _set(run_id, status="completed", summary=summary, completed_at=completed_at)

    save_result({
        "id": run_id,
        "plan_id": state["plan_id"],
        "plan_name": plan.get("name", ""),
        "environment_id": state["env_id"],
        "environment_name": env.get("name", ""),
        "operation": plan.get("operation", "import"),
        "mode": mode,
        "iterations": iterations,
        "started_at": state["started_at"],
        "completed_at": completed_at,
        "executions": executions,
        "summary": summary,
    })


def _single_execution(run_id: str, index: int, plan: dict, env: dict, token: str) -> dict:
    t0 = time.monotonic()
    started_at = _now()
    try:
        client = DMFClient(env["base_url"], token)
        op = plan.get("operation", "import")
        if op == "import":
            result = _do_import(client, plan, index)
        elif op == "export":
            result = _do_export(client, plan, index)
        else:
            raise ValueError(f"Unknown operation '{op}'")

        job_status = result.get("job_status", "")
        exec_failed = job_status in ("Failed", "PartiallySucceeded", "Error", "Canceled", "PollError", "Timeout")
        return {
            "index": index,
            "started_at": started_at,
            "completed_at": _now(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "status": "failed" if exec_failed else "success",
            "execution_id": result.get("execution_id", ""),
            "job_status": job_status,
            "error_message": f"DMF job ended with status: {job_status}" if exec_failed else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "index": index,
            "started_at": started_at,
            "completed_at": _now(),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "status": "failed",
            "execution_id": "",
            "job_status": "Error",
            "error_message": str(exc),
        }


def _do_import(client: DMFClient, plan: dict, index: int) -> dict:
    file_path = plan.get("file_path", "")
    if not file_path or not os.path.exists(file_path):
        raise DMFError(f"Import file not found: {file_path!r}")

    with open(file_path, "rb") as fh:
        file_bytes = fh.read()

    blob_name = unique_blob_name("import", index)
    write_url = client.get_azure_write_url(blob_name)
    client.upload_to_blob(write_url, file_bytes)

    execution_id = client.import_from_package(
        package_url=write_url,
        definition_group_id=plan["definition_group_id"],
        legal_entity=plan["legal_entity"],
        execute=bool(plan.get("execute_immediately", True)),
        overwrite=bool(plan.get("overwrite", True)),
    )

    poll = client.poll_until_complete(
        execution_id,
        timeout=int(plan.get("poll_timeout", 600)),
        interval=int(plan.get("poll_interval", 5)),
    )
    return {"execution_id": execution_id, "job_status": poll["status"]}


def _do_export(client: DMFClient, plan: dict, index: int) -> dict:
    pkg_name = plan.get("package_name") or f"export_run_{index + 1}"

    execution_id = client.export_to_package(
        definition_group_id=plan["definition_group_id"],
        package_name=pkg_name,
        legal_entity=plan["legal_entity"],
        re_execute=bool(plan.get("re_execute", True)),
    )

    poll = client.poll_until_complete(
        execution_id,
        timeout=int(plan.get("poll_timeout", 600)),
        interval=int(plan.get("poll_interval", 5)),
    )
    return {"execution_id": execution_id, "job_status": poll["status"]}
