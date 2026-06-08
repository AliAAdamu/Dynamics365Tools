"""Persistent storage for environments, test plans and run results.

Environments: stored in data/environments.json
  - client_secret is Fernet-encrypted at rest
Test Plans:  stored in data/plans.json
Results:     stored in data/results/<id>.json  (one file per run)
"""
import json
import os
import uuid
from datetime import datetime, timezone

from cryptography.fernet import Fernet

_BASE = os.path.dirname(__file__)
DATA_DIR = os.path.join(_BASE, "data")
KEY_FILE = os.path.join(DATA_DIR, ".encryption.key")
ENVS_FILE = os.path.join(DATA_DIR, "environments.json")
PLANS_FILE = os.path.join(DATA_DIR, "plans.json")
RESULTS_DIR = os.path.join(DATA_DIR, "results")
UPLOADS_DIR = os.path.join(_BASE, "uploads")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    for d in (DATA_DIR, RESULTS_DIR, UPLOADS_DIR):
        os.makedirs(d, exist_ok=True)


def _fernet() -> Fernet:
    _ensure_dirs()
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "wb") as fh:
            fh.write(Fernet.generate_key())
    with open(KEY_FILE, "rb") as fh:
        return Fernet(fh.read())


def _load(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Environments ─────────────────────────────────────────────────────────────

def list_environments() -> list[dict]:
    return _load(ENVS_FILE)


def get_environment(env_id: str) -> dict | None:
    return next((e for e in list_environments() if e["id"] == env_id), None)


def save_environment(data: dict) -> dict:
    """Create or update an environment.  Pass client_secret in plaintext; it
    will be encrypted before writing to disk.  Returns the stored record."""
    fernet = _fernet()
    envs = list_environments()

    if not data.get("id"):
        data["id"] = str(uuid.uuid4())
        data["created_at"] = _now()

    # Encrypt the secret if a new one was supplied
    if data.get("client_secret"):
        data["client_secret_encrypted"] = fernet.encrypt(
            data["client_secret"].encode()
        ).decode()
    # Never persist the plaintext field
    data.pop("client_secret", None)

    data["updated_at"] = _now()

    idx = next((i for i, e in enumerate(envs) if e["id"] == data["id"]), None)
    if idx is not None:
        # Keep encrypted secret if none supplied in update
        if not data.get("client_secret_encrypted"):
            data["client_secret_encrypted"] = envs[idx].get("client_secret_encrypted", "")
        envs[idx] = data
    else:
        envs.append(data)

    _save(ENVS_FILE, envs)
    return data


def delete_environment(env_id: str) -> bool:
    envs = list_environments()
    filtered = [e for e in envs if e["id"] != env_id]
    if len(filtered) == len(envs):
        return False
    _save(ENVS_FILE, filtered)
    return True


def get_client_secret(env: dict) -> str:
    """Decrypt and return the client secret for an environment."""
    encrypted = env.get("client_secret_encrypted", "")
    if not encrypted:
        return ""
    return _fernet().decrypt(encrypted.encode()).decode()


# ─── Test Plans ───────────────────────────────────────────────────────────────

def list_plans() -> list[dict]:
    return _load(PLANS_FILE)


def get_plan(plan_id: str) -> dict | None:
    return next((p for p in list_plans() if p["id"] == plan_id), None)


def save_plan(data: dict) -> dict:
    plans = list_plans()

    if not data.get("id"):
        data["id"] = str(uuid.uuid4())
        data["created_at"] = _now()

    data["updated_at"] = _now()

    idx = next((i for i, p in enumerate(plans) if p["id"] == data["id"]), None)
    if idx is not None:
        plans[idx] = data
    else:
        plans.append(data)

    _save(PLANS_FILE, plans)
    return data


def delete_plan(plan_id: str) -> bool:
    plans = list_plans()
    filtered = [p for p in plans if p["id"] != plan_id]
    if len(filtered) == len(plans):
        return False
    _save(PLANS_FILE, filtered)
    return True


# ─── Results ──────────────────────────────────────────────────────────────────

def save_result(result: dict) -> str:
    _ensure_dirs()
    if not result.get("id"):
        result["id"] = str(uuid.uuid4())
    path = os.path.join(RESULTS_DIR, f"{result['id']}.json")
    _save(path, result)
    return result["id"]


def get_result(result_id: str) -> dict | None:
    path = os.path.join(RESULTS_DIR, f"{result_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def list_results(limit: int = 50) -> list[dict]:
    """Return result summaries (newest first)."""
    _ensure_dirs()
    summaries = []
    files = sorted(os.listdir(RESULTS_DIR), reverse=True)
    for fname in files[:limit]:
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(RESULTS_DIR, fname), encoding="utf-8") as fh:
                r = json.load(fh)
            summaries.append({
                "id": r.get("id"),
                "plan_name": r.get("plan_name"),
                "environment_name": r.get("environment_name"),
                "mode": r.get("mode"),
                "iterations": r.get("iterations"),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
                "summary": r.get("summary", {}),
            })
        except Exception:
            continue
    return summaries
