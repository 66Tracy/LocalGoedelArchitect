"""Tests for canonical problem parsing and anti-cheat finalization.

Fixtures use verbatim strings copied from the real benchmark examples
in lean4_problems/benchmark/benchmark_examples.jsonl so that parsing
is tested against realistic inputs.
"""
from __future__ import annotations

import pytest

from local_goedel.assembly.canonical import (
    CanonicalProblem,
    ALLOWED_AXIOMS,
    axiom_whitelist_ok,
    build_canonical_submission,
    scan_proof_body,
    split_canonical,
)
from local_goedel.domain.lean_check import CheckResult, LeanMessage


# ---------------------------------------------------------------------------
# Benchmark fixture data (verbatim from benchmark_examples.jsonl)
# ---------------------------------------------------------------------------

# Record 0: arithmetic_4185 (Heidi/Lola) — pre_decls has two defs
HEIDI_HEADER = "import Mathlib"
HEIDI_FORMAL = (
    "def Heidi : ℝ := 2.1\n"
    "def Lola : ℝ := 1.4\n"
    "theorem arithmetic_4185 : (Heidi + Lola) / 2 = 1.75 := by sorry"
)
HEIDI_LEAN4_CODE = (
    "import Mathlib\n"
    "def Heidi : ℝ := 2.1\n"
    "def Lola : ℝ := 1.4\n"
    "theorem arithmetic_4185 : (Heidi + Lola) / 2 = 1.75 := by sorry"
)

# Record 1: imo1972_p3 — multi-line signature, rich header
IMO1972_HEADER = (
    "import Mathlib\n"
    "import Aesop\n"
    "\n"
    "set_option maxHeartbeats 0\n"
    "\n"
    "open BigOperators Real Nat Topology Rat"
)
IMO1972_FORMAL = (
    "theorem imo1972_p3 (m n : ℕ) :\n"
    "    m ! * n ! * (m + n)! ∣ (2 * m)! * (2 * n)! := by sorry"
)
IMO1972_LEAN4_CODE = (
    "import Mathlib\n"
    "import Aesop\n"
    "\n"
    "set_option maxHeartbeats 0\n"
    "\n"
    "open BigOperators Real Nat Topology Rat\n"
    "\n"
    "theorem imo1972_p3 (m n : ℕ) :\n"
    "    m ! * n ! * (m + n)! ∣ (2 * m)! * (2 * n)! := by sorry"
)

# Record 3: imo_sl_2015_N3 — extra 'open Finset' INSIDE formal_statement
IMO_SL_HEADER = (
    "import Mathlib\n"
    "import Aesop\n"
    "\n"
    "set_option maxHeartbeats 0\n"
    "\n"
    "open BigOperators Real Nat Topology Rat"
)
IMO_SL_FORMAL = (
    "/- special open -/ open Finset\n"
    "theorem imo_sl_2015_N3 (hm : 0 < m) (hn : 1 < n) (h : ∀ k ∈ Ico n (2 * n), k ∣ m) :\n"
    "    ∀ N, ∏ k ∈ Ico n (2 * n), (m / k + 1) ≠ 2 ^ N + 1 := by sorry"
)

# Record 4: putnam_2025_a1 — multi-line signature, set_option in header
PUTNAM_HEADER = (
    "import Mathlib\n"
    "set_option pp.numericTypes true\n"
    "set_option pp.funBinderTypes true\n"
    "set_option maxHeartbeats 0\n"
    "open Classical"
)
PUTNAM_FORMAL = (
    "theorem putnam_2025_a1 (m n : ℕ → ℕ)\n"
    "  (h0 : m 0 > 0 ∧ n 0 > 0 ∧ m 0 ≠ n 0)\n"
    "  (hm : ∀ k : ℕ, m (k + 1) = ((2 * m k + 1) / (2 * n k + 1) : ℚ).num)\n"
    "  (hn : ∀ k : ℕ, n (k + 1) = ((2 * m k + 1) / (2 * n k + 1) : ℚ).den):\n"
    "  {k | ¬ (2 * m k + 1).Coprime (2 * n k + 1)}.Finite := by sorry"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_check(messages: list[LeanMessage], status: str = "") -> CheckResult:
    return CheckResult(snippet_id="test", messages=messages, status=status)


def _axiom_info(thm_name: str, axioms: list[str]) -> LeanMessage:
    axioms_str = ", ".join(axioms)
    return LeanMessage(
        severity="info",
        data=f"'{thm_name}' depends on axioms: [{axioms_str}]",
    )


# ===========================================================================
# split_canonical — structured path (header + formal_statement provided)
# ===========================================================================

class TestSplitCanonicalStructured:
    """Tests for the preferred (structured benchmark record) parsing path."""

    def test_heidi_lola_pre_decls(self):
        """arithmetic_4185: pre_decls has two def lines; thm extracted correctly."""
        cp = split_canonical(HEIDI_LEAN4_CODE, HEIDI_FORMAL, HEIDI_HEADER)

        assert cp.header == "import Mathlib"
        assert cp.keyword == "theorem"
        assert cp.thm_name == "arithmetic_4185"

        # Both def lines are in pre_decls.
        assert "def Heidi" in cp.pre_decls
        assert "def Lola" in cp.pre_decls

        # Signature should be the text between name and ':= by sorry'.
        assert ": (Heidi + Lola) / 2 = 1.75" in cp.thm_signature

        # ':= by sorry' must NOT be in the signature.
        assert ":= by sorry" not in cp.thm_signature
        assert "sorry" not in cp.thm_signature

    def test_heidi_header_verbatim(self):
        """Header must be taken verbatim from the 'header' field."""
        cp = split_canonical(HEIDI_LEAN4_CODE, HEIDI_FORMAL, HEIDI_HEADER)
        assert cp.header == HEIDI_HEADER

    def test_imo1972_multiline_signature(self):
        """imo1972_p3: multi-line signature must be preserved; no pre_decls."""
        cp = split_canonical(IMO1972_LEAN4_CODE, IMO1972_FORMAL, IMO1972_HEADER)

        assert cp.header == IMO1972_HEADER
        assert cp.keyword == "theorem"
        assert cp.thm_name == "imo1972_p3"
        assert cp.pre_decls == ""

        # Signature spans two lines in the source; it should contain the binders.
        assert "(m n : ℕ)" in cp.thm_signature
        assert "∣" in cp.thm_signature  # divisibility symbol

        # Must not contain the sorry body.
        assert ":= by sorry" not in cp.thm_signature

    def test_imo_sl_2015_N3_open_in_formal(self):
        """imo_sl_2015_N3: '/- special open -/ open Finset' in formal_statement
        must land in pre_decls, not in the theorem signature."""
        cp = split_canonical(
            "lean4_code_not_used",
            IMO_SL_FORMAL,
            IMO_SL_HEADER,
        )

        assert cp.header == IMO_SL_HEADER
        assert cp.keyword == "theorem"
        assert cp.thm_name == "imo_sl_2015_N3"

        # The 'open Finset' line must be in pre_decls.
        assert "open Finset" in cp.pre_decls

        # The main theorem signature must NOT include the open line.
        assert "open Finset" not in cp.thm_signature

        # Signature must contain hm, hn, h parameters.
        assert "hm" in cp.thm_signature
        assert ":= by sorry" not in cp.thm_signature

    def test_simple_minif2f_no_pre_decls(self):
        """A simple single-theorem with no pre_decls (MiniF2F-style)."""
        header = "import Mathlib"
        formal = "theorem algebra_sq (a b : ℝ) : (a - b) ^ 2 ≥ 0 := by sorry"
        lean4_code = "import Mathlib\ntheorem algebra_sq (a b : ℝ) : (a - b) ^ 2 ≥ 0 := by sorry"

        cp = split_canonical(lean4_code, formal, header)

        assert cp.pre_decls == ""
        assert cp.keyword == "theorem"
        assert cp.thm_name == "algebra_sq"
        assert "(a b : ℝ)" in cp.thm_signature
        assert "(a - b) ^ 2" in cp.thm_signature

    def test_putnam_multiline_signature(self):
        """putnam_2025_a1: 4-line signature with complex binders."""
        cp = split_canonical("lean4_code", PUTNAM_FORMAL, PUTNAM_HEADER)

        assert cp.thm_name == "putnam_2025_a1"
        assert cp.pre_decls == ""

        # Signature must contain the multi-line binders.
        assert "(m n : ℕ → ℕ)" in cp.thm_signature
        assert ".Finite" in cp.thm_signature
        assert ":= by sorry" not in cp.thm_signature

    def test_lemma_keyword_preserved(self):
        """When the canonical uses 'lemma', keyword must be 'lemma'."""
        header = "import Mathlib"
        formal = "lemma my_lemma (n : ℕ) : n + 0 = n := by sorry"
        lean4_code = "import Mathlib\nlemma my_lemma (n : ℕ) : n + 0 = n := by sorry"

        cp = split_canonical(lean4_code, formal, header)

        assert cp.keyword == "lemma"
        assert cp.thm_name == "my_lemma"


# ===========================================================================
# split_canonical — fallback path (lean4_code only)
# ===========================================================================

class TestSplitCanonicalFallback:
    """Tests for the lean4_code-only fallback path."""

    def test_fallback_heidi_same_as_structured(self):
        """Fallback path for Heidi/Lola should give the same result as structured."""
        structured = split_canonical(HEIDI_LEAN4_CODE, HEIDI_FORMAL, HEIDI_HEADER)
        fallback = split_canonical(HEIDI_LEAN4_CODE)

        assert fallback.thm_name == structured.thm_name
        assert fallback.keyword == structured.keyword
        # pre_decls should contain both defs.
        assert "def Heidi" in fallback.pre_decls
        assert "def Lola" in fallback.pre_decls
        # Signature should match.
        assert fallback.thm_signature == structured.thm_signature

    def test_fallback_imo1972_extracts_header(self):
        """Fallback for imo1972_p3 must extract the rich header correctly."""
        cp = split_canonical(IMO1972_LEAN4_CODE)

        assert "import Mathlib" in cp.header
        assert "set_option maxHeartbeats 0" in cp.header
        assert "open BigOperators" in cp.header
        assert cp.thm_name == "imo1972_p3"
        assert cp.pre_decls == ""

    def test_fallback_simple(self):
        """Fallback path for a trivial single-theorem file."""
        code = "import Mathlib\ntheorem trivial_eq : 1 + 1 = 2 := by sorry"
        cp = split_canonical(code)

        assert "import Mathlib" in cp.header
        assert cp.thm_name == "trivial_eq"
        assert cp.pre_decls == ""
        assert ": 1 + 1 = 2" in cp.thm_signature


# ===========================================================================
# build_canonical_submission
# ===========================================================================

class TestBuildCanonicalSubmission:
    """Tests for the file builder."""

    def _heidi_canon(self) -> CanonicalProblem:
        return split_canonical(HEIDI_LEAN4_CODE, HEIDI_FORMAL, HEIDI_HEADER)

    def test_round_trip_contains_header(self):
        """Output must contain the canonical header verbatim."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by nlinarith [sq_nonneg (a-b)]")
        assert "import Mathlib" in out

    def test_round_trip_contains_theorem_line(self):
        """Output must contain 'theorem arithmetic_4185 ... := by ...'."""
        cp = self._heidi_canon()
        body = "by nlinarith [sq_nonneg (a-b)]"
        out = build_canonical_submission(cp, body)
        assert "theorem arithmetic_4185" in out
        assert ":= by nlinarith" in out

    def test_round_trip_contains_pre_decls(self):
        """Output must include pre_decls (the two def lines)."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by norm_num")
        assert "def Heidi" in out
        assert "def Lola" in out

    def test_round_trip_axiom_print(self):
        """#print axioms line must appear when add_axiom_print=True (default)."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by norm_num")
        assert "#print axioms arithmetic_4185" in out

    def test_round_trip_no_axiom_print(self):
        """#print axioms line must NOT appear when add_axiom_print=False."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by norm_num", add_axiom_print=False)
        assert "#print axioms" not in out

    def test_leading_assign_stripped(self):
        """A proof body starting with ':=' should have it stripped."""
        cp = self._heidi_canon()
        # Model submitted ":= by norm_num" with a leading ':='
        out = build_canonical_submission(cp, ":= by norm_num")
        # The file should contain ':= by norm_num' ONCE (as the := proof),
        # not ':= := by norm_num'.
        assert ":= by norm_num" in out
        assert ":= := by" not in out

    def test_trailing_newline(self):
        """Output must end with exactly one trailing newline."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by norm_num")
        assert out.endswith("\n")

    def test_unix_line_endings(self):
        """Output must use Unix line endings only."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by norm_num")
        assert "\r" not in out

    def test_no_sorry_in_output(self):
        """The output file must not contain ':= by sorry' from the canonical."""
        cp = self._heidi_canon()
        out = build_canonical_submission(cp, "by norm_num")
        # ':= by sorry' was in the canonical; the output should use the supplied body.
        assert "sorry" not in out

    def test_imo1972_multiline(self):
        """imo1972_p3 with multi-line signature builds correctly."""
        cp = split_canonical(IMO1972_LEAN4_CODE, IMO1972_FORMAL, IMO1972_HEADER)
        body = "by exact Nat.factorial_dvd_factorial_mul_factorial m n"
        out = build_canonical_submission(cp, body)

        assert "theorem imo1972_p3" in out
        # Header lines present.
        assert "import Mathlib" in out
        assert "set_option maxHeartbeats 0" in out
        assert "#print axioms imo1972_p3" in out

    def test_body_verbatim_otherwise(self):
        """A body that does not start with ':=' is used verbatim."""
        cp = self._heidi_canon()
        body = "by\n  unfold Heidi Lola\n  norm_num"
        out = build_canonical_submission(cp, body)
        assert "by\n  unfold Heidi Lola\n  norm_num" in out


# ===========================================================================
# scan_proof_body
# ===========================================================================

class TestScanProofBody:
    """Tests for the proof body violation scanner."""

    def test_clean_body_no_violations(self):
        """A legitimate tactic proof produces no violations."""
        body = (
            "by\n"
            "  intro h\n"
            "  have hx := sq_nonneg x\n"
            "  let y := 2 * x\n"
            "  calc a + b = b + a := by ring\n"
            "  linarith [hx]\n"
        )
        assert scan_proof_body(body) == []

    def test_injected_theorem_at_col0_flagged(self):
        """A 'theorem' keyword at column 0 inside the body is a violation."""
        body = (
            "by\n"
            "  sorry\n"
            "theorem evil_bypass (n : Nat) : n = n := by rfl\n"
        )
        violations = scan_proof_body(body)
        assert any("theorem" in v for v in violations)

    def test_injected_lemma_at_col0_flagged(self):
        """'lemma' at column 0 is flagged."""
        body = "by\n  rfl\nlemma helper : True := trivial\n"
        violations = scan_proof_body(body)
        assert any("lemma" in v for v in violations)

    def test_injected_def_at_col0_flagged(self):
        """'def' at column 0 is flagged."""
        body = "by\n  norm_num\ndef myVal : Nat := 42\n"
        violations = scan_proof_body(body)
        assert any("def" in v for v in violations)

    def test_injected_import_at_col0_flagged(self):
        """'import' at column 0 inside body is flagged."""
        body = "by\nimport Std\n  rfl\n"
        violations = scan_proof_body(body)
        assert any("import" in v for v in violations)

    def test_bare_open_at_col0_flagged(self):
        """'open Foo' at column 0 (without ' in') is flagged."""
        body = "by\n  rfl\nopen Finset\n  apply card_le_card\n"
        violations = scan_proof_body(body)
        assert any("open" in v for v in violations)

    def test_open_foo_in_not_flagged(self):
        """'open Foo in' (expression-level open) must NOT be flagged."""
        body = "by\n  exact open Finset in card_pos.mpr (by simp)\n"
        # The 'open Foo in' is NOT at column 0 here (it's after 'exact').
        assert scan_proof_body(body) == []

    def test_col0_open_with_in_not_flagged(self):
        """'open Finset in' at column 0 is NOT flagged (it has ' in')."""
        body = "open Finset in\nby\n  apply card_le_card\n"
        violations = scan_proof_body(body)
        # 'open … in' at col 0 is a legal term-mode prefix — not flagged.
        assert not any("'open' at column 0" in v for v in violations)

    def test_native_decide_flagged(self):
        """native_decide usage is flagged."""
        body = "by\n  native_decide\n"
        violations = scan_proof_body(body)
        assert any("native_decide" in v for v in violations)

    def test_native_decide_inline_flagged(self):
        """native_decide appearing inline is also flagged."""
        body = "by exact native_decide"
        violations = scan_proof_body(body)
        assert any("native_decide" in v for v in violations)

    def test_sorry_flagged(self):
        """sorry anywhere in the proof body is flagged."""
        body = "by\n  intro h\n  sorry\n"
        violations = scan_proof_body(body)
        assert any("sorry" in v for v in violations)

    def test_axiom_at_col0_flagged(self):
        """'axiom' at column 0 is flagged by the column-0 rule."""
        body = "by\n  apply h\naxiom foo : Nat\n"
        violations = scan_proof_body(body)
        assert any("axiom" in v for v in violations)

    def test_indented_def_not_flagged(self):
        """An indented 'def' (inside let/where) is NOT flagged."""
        body = (
            "by\n"
            "  let rec f : Nat → Nat\n"
            "    | 0 => 0\n"
            "    | n + 1 => f n + 1\n"
            "  exact f 5 |>.succ_pos\n"
        )
        # 'let rec' is indented → no col-0 match; the line starting with spaces
        # containing 'def' patterns won't match the col-0 rule.
        violations = scan_proof_body(body)
        assert violations == []

    def test_multiple_violations_all_reported(self):
        """Multiple violations should all be present in the list."""
        body = (
            "by\n"
            "  sorry\n"
            "theorem injected : True := trivial\n"
            "axiom bad_axiom : False\n"
        )
        violations = scan_proof_body(body)
        # At minimum: sorry, theorem at col 0, axiom at col 0.
        assert len(violations) >= 2

    def test_clean_multiline_calc(self):
        """Multi-line calc proof with no violations."""
        body = (
            "by\n"
            "  calc a ^ 2 + b ^ 2\n"
            "      _ = a ^ 2 - 2 * a * b + b ^ 2 + 2 * a * b := by ring\n"
            "      _ ≥ 2 * a * b := by nlinarith [sq_nonneg (a - b)]\n"
        )
        assert scan_proof_body(body) == []

    def test_namespace_at_col0_flagged(self):
        """'namespace' at column 0 is flagged."""
        body = "by\n  rfl\nnamespace Injected\n"
        violations = scan_proof_body(body)
        assert any("namespace" in v for v in violations)


# ===========================================================================
# axiom_whitelist_ok
# ===========================================================================

class TestAxiomWhitelistOk:
    """Tests for the axiom whitelist checker."""

    def test_allowed_axioms_only(self):
        """All three standard axioms → ok=True, offending=[]."""
        check = _make_check([
            _axiom_info("myThm", ["propext", "Classical.choice", "Quot.sound"])
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is True
        assert offending == []

    def test_subset_of_allowed_axioms(self):
        """Only propext → ok=True."""
        check = _make_check([_axiom_info("myThm", ["propext"])])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is True
        assert offending == []

    def test_empty_axiom_list(self):
        """Zero axiom dependencies → ok=True."""
        check = _make_check([_axiom_info("myThm", [])])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is True
        assert offending == []

    def test_sorry_ax_rejected(self):
        """sorryAx in axiom list → ok=False, sorryAx in offending."""
        check = _make_check([
            _axiom_info("myThm", ["sorryAx", "propext"])
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert "sorryAx" in offending
        assert "propext" not in offending

    def test_native_decide_axiom_rejected(self):
        """Lean.ofReduceBool (from native_decide) → ok=False."""
        check = _make_check([
            _axiom_info("myThm", ["propext", "Classical.choice", "Lean.ofReduceBool"])
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert "Lean.ofReduceBool" in offending

    def test_lean_of_reduce_nat_rejected(self):
        """Lean.ofReduceNat (from native_decide on Nat) → ok=False."""
        check = _make_check([
            _axiom_info("myThm", ["Lean.ofReduceNat", "Quot.sound"])
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert "Lean.ofReduceNat" in offending

    def test_user_axiom_rejected(self):
        """A custom user-declared axiom → ok=False."""
        check = _make_check([
            _axiom_info("myThm", ["propext", "myPackage.myAxiom"])
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert "myPackage.myAxiom" in offending

    def test_fallback_no_info_message_clean(self):
        """No 'depends on axioms' message and no sorry → ok=True."""
        # A check with only a benign info message.
        check = _make_check([
            LeanMessage(severity="info", data="something unrelated")
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is True
        assert offending == []

    def test_fallback_no_info_message_sorry(self):
        """No 'depends on axioms' message but sorry warning → ok=False."""
        check = _make_check([
            LeanMessage(severity="warning", data="declaration uses 'sorry'")
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert offending == []

    def test_fallback_status_sorry(self):
        """No axiom message, status='sorry' → ok=False (via mentions_sorry)."""
        check = _make_check([], status="sorry")
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert offending == []

    def test_wrong_theorem_name_uses_fallback(self):
        """Info message for a DIFFERENT theorem name triggers fallback."""
        check = _make_check([
            _axiom_info("otherThm", ["sorryAx"])
        ])
        # No message matches 'myThm', so fallback applies.
        # The check has no sorry warning, so fallback → ok=True.
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is True
        assert offending == []

    def test_allowed_axioms_constant_content(self):
        """ALLOWED_AXIOMS must contain exactly the three standard axioms."""
        assert ALLOWED_AXIOMS == {"propext", "Classical.choice", "Quot.sound"}

    def test_all_three_plus_sorry_ax(self):
        """All three allowed + sorryAx → ok=False, offending=[sorryAx]."""
        check = _make_check([
            _axiom_info("myThm", ["propext", "Classical.choice", "Quot.sound", "sorryAx"])
        ])
        ok, offending = axiom_whitelist_ok(check, "myThm")
        assert ok is False
        assert offending == ["sorryAx"]
