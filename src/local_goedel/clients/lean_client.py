"""Client for the Kimina Lean server."""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import requests

from local_goedel.domain.lean_check import CheckResult
from local_goedel.logging_utils import get_logger

if TYPE_CHECKING:
    from local_goedel.config import Settings


class LeanServerError(Exception):
    """Raised on transport failure talking to the Lean server."""



class LeanClient:
    def __init__(self, settings: "Settings") -> None:
        self._url = settings.lean_server_url.rstrip("/")
        self._timeout = settings.lean_timeout_s
        self._logger = get_logger(__name__)
        self._use_kimina: bool | None = None  # lazy probe

    def _try_import_kimina(self) -> bool:
        if self._use_kimina is not None:
            return self._use_kimina
        try:
            import kimina_client  # noqa: F401
            self._use_kimina = True
        except ImportError:
            self._use_kimina = False
        return self._use_kimina

    def health(self) -> bool:
        """Return True if the server responds healthy."""
        try:
            resp = requests.get(
                f"{self._url}/health", timeout=10
            )
            return resp.status_code == 200
        except Exception:
            return False

    def check(self, code: str, snippet_id: str | None = None) -> CheckResult:
        """Check a single Lean snippet, return CheckResult."""
        if snippet_id is None:
            snippet_id = str(uuid.uuid4())
        results = self._check_batch_impl([(snippet_id, code)])
        return results[0]

    def check_batch(
        self, items: list[tuple[str, str] | dict[str, str]]
    ) -> list[CheckResult]:
        """Check multiple snippets. items: list of (id, code) tuples or dicts."""
        results = []
        for item in items:
            if isinstance(item, dict):
                sid, code = item["id"], item["code"]
            else:
                sid, code = item[0], item[1]
            results.append(self.check(code, sid))
        return results

    # ------------------------------------------------------------------
    # Private implementation
    # ------------------------------------------------------------------

    def _check_batch_impl(
        self, snippets: list[tuple[str, str]]
    ) -> list[CheckResult]:
        """Send snippets to /api/check over raw HTTP and return CheckResults."""
        payload_snippets = [{"id": sid, "code": code} for sid, code in snippets]
        try:
            resp = requests.post(
                f"{self._url}/api/check",
                json={"snippets": payload_snippets},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            raise LeanServerError(f"Transport error: {e}") from e

        results = []
        for sid, _code in snippets:
            result = CheckResult.from_payload(payload, sid)
            results.append(result)
        return results
