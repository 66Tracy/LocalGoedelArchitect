"""Integration tests for the Lean client."""
import pytest

from local_goedel.clients.lean_client import LeanClient
from local_goedel.config import Settings


@pytest.fixture
def lean_client():
    settings = Settings(lean_server_url="http://localhost:8000", lean_timeout_s=30)
    return LeanClient(settings)


@pytest.fixture(autouse=True)
def skip_if_no_lean(lean_client):
    if not lean_client.health():
        pytest.skip("Lean server not running at http://localhost:8000")


def test_health(lean_client):
    assert lean_client.health() is True


def test_nat_check(lean_client):
    """#check Nat should return info."""
    result = lean_client.check("#check Nat")
    # No errors expected
    assert not result.has_errors
    # Should have at least an info message
    infos = [m for m in result.messages if m.severity == "info"]
    assert len(infos) >= 1


def test_bad_theorem_has_errors(lean_client):
    """A provably false theorem should yield errors."""
    code = "import Mathlib\ntheorem bad: (1:Nat) = 2 := by rfl"
    result = lean_client.check(code)
    assert result.has_errors
