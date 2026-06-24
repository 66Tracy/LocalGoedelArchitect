"""Tests for CheckResult parsing."""
import pytest

from local_goedel.domain.lean_check import CheckResult, LeanMessage


def _make_payload(snippet_id, messages, sorries=None):
    return {
        "results": [
            {
                "id": snippet_id,
                "time": 0.5,
                "response": {
                    "env": 1,
                    "messages": messages,
                    "sorries": sorries or [],
                },
            }
        ]
    }


def test_clean_result():
    """No errors, no sorry."""
    payload = _make_payload(
        "s1",
        [{"severity": "info", "data": "theorem proved", "pos": {}, "endPos": {}}],
    )
    result = CheckResult.from_payload(payload, "s1")
    assert not result.has_errors
    assert not result.mentions_sorry()
    assert result.env == 1


def test_error_result():
    """Has errors."""
    payload = _make_payload(
        "s2",
        [
            {
                "severity": "error",
                "data": "unknown identifier 'bad'",
                "pos": {"line": 3, "column": 5},
                "endPos": {"line": 3, "column": 8},
            }
        ],
    )
    result = CheckResult.from_payload(payload, "s2")
    assert result.has_errors
    assert len(result.errors) == 1
    assert "unknown identifier" in result.errors[0].data


def test_sorry_warning():
    """Warning about sorry usage."""
    payload = _make_payload(
        "s3",
        [
            {
                "severity": "warning",
                "data": "declaration uses 'sorry'",
                "pos": {},
                "endPos": {},
            }
        ],
    )
    result = CheckResult.from_payload(payload, "s3")
    assert not result.has_errors
    assert result.mentions_sorry()


def test_sorry_via_sorries_field():
    """Non-empty sorries field triggers mentions_sorry."""
    payload = _make_payload("s4", [], sorries=[{"some": "sorry"}])
    result = CheckResult.from_payload(payload, "s4")
    assert result.mentions_sorry()


def test_missing_snippet_id_fallback():
    """Falls back to first result if ID not found."""
    payload = _make_payload("real_id", [])
    result = CheckResult.from_payload(payload, "different_id")
    assert result.snippet_id == "different_id"
