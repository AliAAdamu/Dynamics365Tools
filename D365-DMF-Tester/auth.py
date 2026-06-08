"""OAuth2 client-credentials token management with in-memory caching."""
import threading
import time

import requests

_cache: dict[str, dict] = {}
_lock = threading.Lock()

AUTHORITY = "https://login.microsoftonline.com"
_TOKEN_EXPIRY_BUFFER_S = 90  # refresh this many seconds before expiry


def get_access_token(
    tenant_id: str, client_id: str, client_secret: str, base_url: str
) -> str:
    """Return a valid bearer token (from cache or freshly acquired)."""
    cache_key = f"{tenant_id}:{client_id}:{base_url}"

    with _lock:
        entry = _cache.get(cache_key)
        if entry and entry["expires_at"] > time.monotonic() + _TOKEN_EXPIRY_BUFFER_S:
            return entry["access_token"]

    resource = base_url.rstrip("/")
    token_url = f"{AUTHORITY}/{tenant_id}/oauth2/v2.0/token"

    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{resource}/.default",
        },
        timeout=30,
    )

    if not resp.ok:
        body = resp.json() if resp.content else {}
        raise AuthError(
            f"Token request failed ({resp.status_code}): "
            f"{body.get('error_description', resp.text[:300])}"
        )

    data = resp.json()
    expires_in = int(data.get("expires_in", 3600))

    with _lock:
        _cache[cache_key] = {
            "access_token": data["access_token"],
            "expires_at": time.monotonic() + expires_in,
        }

    return data["access_token"]


def clear_token_cache(tenant_id: str | None = None, client_id: str | None = None) -> None:
    """Evict one entry or the entire cache."""
    with _lock:
        if tenant_id and client_id:
            keys = [k for k in _cache if k.startswith(f"{tenant_id}:{client_id}")]
            for k in keys:
                _cache.pop(k, None)
        else:
            _cache.clear()


class AuthError(Exception):
    """Raised when authentication fails."""
