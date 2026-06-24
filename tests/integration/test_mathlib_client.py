"""Integration tests for the Mathlib search client."""
import pytest
import requests

from local_goedel.clients.mathlib_client import MathlibSearchClient
from local_goedel.config import Settings

LEANDEX_URL = "https://leandex.projectnumina.ai/api/v1/search"


def _is_leandex_reachable() -> bool:
    try:
        resp = requests.get(LEANDEX_URL, timeout=5, params={"q": "test", "limit": 1})
        return True
    except Exception:
        return False


@pytest.fixture
def mathlib_client():
    settings = Settings(
        leandex_url=LEANDEX_URL,
        mathlib_min_interval_s=0.5,
        mathlib_max_retries=2,
    )
    return MathlibSearchClient(settings)


def test_search_returns_results(mathlib_client):
    if not _is_leandex_reachable():
        pytest.skip("Leandex not reachable")

    hits = mathlib_client.search("square inequality real numbers", limit=3)
    assert len(hits) >= 1
    assert hits[0].lean_name  # non-empty name
