"""Tests for axiom checking."""
import pytest

from local_goedel.assembly.axiom_check import uses_sorry
from local_goedel.domain.lean_check import CheckResult, LeanMessage


def _make_check(messages):
    return CheckResult(snippet_id="test", messages=messages)


def test_uses_sorry_with_sorryAx():
    """Info message with sorryAx -> uses_sorry=True."""
    msg = LeanMessage(
        severity="info",
        data="'myThm' depends on axioms: [sorryAx, propext]",
    )
    check = _make_check([msg])
    assert uses_sorry(check, "myThm") is True


def test_uses_sorry_without_sorryAx():
    """Info message without sorryAx -> uses_sorry=False."""
    msg = LeanMessage(
        severity="info",
        data="'myThm' depends on axioms: [propext, Classical.choice, Quot.sound]",
    )
    check = _make_check([msg])
    assert uses_sorry(check, "myThm") is False


def test_uses_sorry_fallback_to_warning():
    """No axiom info message -> falls back to mentions_sorry."""
    msg = LeanMessage(
        severity="warning",
        data="declaration uses 'sorry'",
    )
    check = _make_check([msg])
    # fallback: mentions_sorry() returns True
    assert uses_sorry(check, "myThm") is True


def test_uses_sorry_clean():
    """Clean check with correct axiom info."""
    msg = LeanMessage(
        severity="info",
        data="'cleanThm' depends on axioms: [propext]",
    )
    check = _make_check([msg])
    assert uses_sorry(check, "cleanThm") is False


def test_uses_sorry_wrong_theorem_name_fallback():
    """Wrong theorem name -> no match -> fallback."""
    msg = LeanMessage(
        severity="info",
        data="'otherThm' depends on axioms: [sorryAx]",
    )
    check = _make_check([msg])
    # No match for 'myThm', fallback to mentions_sorry() = False
    assert uses_sorry(check, "myThm") is False
