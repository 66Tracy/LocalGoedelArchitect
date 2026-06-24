"""Client for Leandex Mathlib search."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import requests

from local_goedel.logging_utils import get_logger

if TYPE_CHECKING:
    from local_goedel.config import Settings

_global_lock = threading.Lock()
_last_request_time: float = 0.0


@dataclass
class SearchHit:
    lean_name: str
    signature_or_source: str
    docstring: str
    module: str


class MathlibSearchClient:
    def __init__(self, settings: "Settings") -> None:
        self._url = settings.leandex_url
        self._api_key = settings.leandex_api_key
        self._min_interval = settings.mathlib_min_interval_s
        self._max_retries = settings.mathlib_max_retries
        self._logger = get_logger(__name__)

    def _headers(self) -> dict[str, str]:
        h = {
            "accept": "text/event-stream",
            "user-agent": "local-goedel/0.1",
        }
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _throttle(self) -> None:
        global _last_request_time
        with _global_lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            _last_request_time = time.monotonic()

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        limit = min(limit, 10)
        self._logger.info("Mathlib search: %r limit=%d", query, limit)

        for attempt in range(self._max_retries):
            self._throttle()
            try:
                data_str: Optional[str] = None
                with requests.get(
                    self._url,
                    headers=self._headers(),
                    params={
                        "q": query,
                        "limit": limit,
                        "generate_query": False,
                        "analyze_result": False,
                    },
                    stream=True,
                    timeout=30,
                ) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        if line.startswith("data:"):
                            data_str = line.removeprefix("data:").strip()

                if data_str is None:
                    self._logger.warning("No data from Leandex (attempt %d)", attempt)
                    continue

                parsed = json.loads(data_str)
                raw_results = parsed["data"]["search_results"]
                hits: list[SearchHit] = []
                for r in raw_results:
                    pd = r.get("primary_declaration") or {}
                    if isinstance(pd, str):
                        lean_name = pd
                    elif isinstance(pd, dict) and pd.get("lean_name"):
                        lean_name = pd.get("lean_name", "")
                    else:
                        # Flat structure: use "name" or "lean_name"
                        lean_name = r.get("lean_name") or r.get("name", "")

                    if isinstance(pd, dict):
                        sig = pd.get("signature_or_source", "") or r.get("signature_or_source", "") or r.get("source_text", "")
                        doc = pd.get("docstring", "") or r.get("docstring", "")
                        mod = pd.get("module", "") or r.get("module", "")
                    else:
                        sig = r.get("signature_or_source", "") or r.get("source_text", "")
                        doc = r.get("docstring", "")
                        mod = r.get("module", "")

                    hits.append(SearchHit(
                        lean_name=lean_name,
                        signature_or_source=sig,
                        docstring=doc or "",
                        module=mod,
                    ))
                self._logger.info("Mathlib search returned %d hits", len(hits))
                return hits

            except Exception as e:
                wait = 2 ** attempt
                self._logger.warning(
                    "Mathlib search error (attempt %d/%d): %s; retrying in %ds",
                    attempt + 1, self._max_retries, e, wait,
                )
                time.sleep(wait)

        self._logger.error("Mathlib search failed after %d attempts", self._max_retries)
        return []
