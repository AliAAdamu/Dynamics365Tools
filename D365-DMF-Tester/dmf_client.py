"""Dynamics 365 Data Management Framework REST API client."""
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

import requests

_TERMINAL_STATUSES = {"Succeeded", "PartiallySucceeded", "Failed", "Canceled", "Error"}
_POLL_MAX_RETRIES = 3


class DMFError(Exception):
    """Raised for DMF API errors."""


class DMFClient:
    """Thin wrapper around the D365 DMF OData endpoints."""

    def __init__(self, base_url: str, access_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}/data/{path}"

    def _post(self, path: str, body: dict) -> dict:
        url = self._url(path)
        resp = self._session.post(url, json=body, timeout=60)
        if not resp.ok:
            raise DMFError(f"POST {path} returned HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self._url(path)
        resp = self._session.get(url, params=params, timeout=30)
        if not resp.ok:
            raise DMFError(f"GET {path} returned HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def get_azure_write_url(self, unique_filename: str) -> str:
        result = self._post(
            "DataManagementDefinitionGroups"
            "/Microsoft.Dynamics.DataEntities.GetAzureWriteUrl",
            {"uniqueFileName": unique_filename},
        )
        # The API returns a JSON-encoded string as `value`, e.g.:
        # {"BlobId":"{...}","BlobUrl":"https://..."}
        import json as _json
        raw = result["value"]
        if isinstance(raw, str):
            try:
                parsed = _json.loads(raw)
                return parsed["BlobUrl"]
            except (ValueError, KeyError):
                pass
        # Fallback: value is already a dict
        if isinstance(raw, dict):
            return raw["BlobUrl"]
        return raw

    def upload_to_blob(self, write_url: str, file_bytes: bytes) -> None:
        resp = requests.put(
            write_url,
            data=file_bytes,
            headers={
                "x-ms-blob-type": "BlockBlob",
                "Content-Type": "application/octet-stream",
            },
            timeout=120,
        )
        if not resp.ok:
            raise DMFError(f"Blob upload failed (HTTP {resp.status_code}): {resp.text[:300]}")

    def import_from_package(
        self,
        package_url: str,
        definition_group_id: str,
        legal_entity: str,
        *,
        execute: bool = True,
        overwrite: bool = True,
    ) -> str:
        result = self._post(
            "DataManagementDefinitionGroups"
            "/Microsoft.Dynamics.DataEntities.ImportFromPackage",
            {
                "packageUrl": package_url,
                "definitionGroupId": definition_group_id,
                "execute": execute,
                "overwrite": overwrite,
                "legalEntityId": legal_entity,
                "executionId": "",
            },
        )
        return result["value"]

    def export_to_package(
        self,
        definition_group_id: str,
        package_name: str,
        legal_entity: str,
        *,
        re_execute: bool = True,
    ) -> str:
        result = self._post(
            "DataManagementDefinitionGroups"
            "/Microsoft.Dynamics.DataEntities.ExportToPackage",
            {
                "definitionGroupId": definition_group_id,
                "packageName": package_name,
                "executionId": "",
                "reExecute": re_execute,
                "legalEntityId": legal_entity,
            },
        )
        return result["value"]

    def get_execution_status(self, execution_id: str) -> str:
        result = self._post(
            "DataManagementDefinitionGroups"
            "/Microsoft.Dynamics.DataEntities.GetExecutionSummaryStatus",
            {"executionId": execution_id},
        )
        return result["value"]

    def get_exported_file_url(self, execution_id: str) -> str:
        result = self._post(
            "DataManagementDefinitionGroups"
            "/Microsoft.Dynamics.DataEntities.GetExportedPackageUrl",
            {"executionId": execution_id},
        )
        return result["value"]

    def poll_until_complete(
        self,
        execution_id: str,
        *,
        timeout: int = 600,
        interval: int = 5,
        on_status: Callable[[str, float], None] | None = None,
    ) -> dict:
        deadline = time.monotonic() + timeout
        retries = 0

        while True:
            elapsed = timeout - (deadline - time.monotonic())
            try:
                status = self.get_execution_status(execution_id)
                retries = 0
            except DMFError:
                retries += 1
                if retries > _POLL_MAX_RETRIES:
                    return {"status": "PollError", "elapsed_seconds": elapsed}
                time.sleep(interval)
                continue

            if on_status:
                on_status(status, elapsed)

            if status in _TERMINAL_STATUSES:
                return {"status": status, "elapsed_seconds": elapsed}

            if time.monotonic() >= deadline:
                return {"status": "Timeout", "elapsed_seconds": timeout}

            time.sleep(interval)

    def list_definition_groups(self) -> list[dict]:
        try:
            data = self._get(
                "DataManagementDefinitionGroups",
                params={
                    "$select": "DefinitionGroupId,Description",
                    "$top": "500",
                    "$orderby": "DefinitionGroupId",
                },
            )
            return data.get("value", [])
        except DMFError:
            return []


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unique_blob_name(prefix: str = "dmf_pkg", index: int = 0) -> str:
    ts = int(time.time())
    short = uuid.uuid4().hex[:8]
    return f"{prefix}_{ts}_{index}_{short}.zip"
