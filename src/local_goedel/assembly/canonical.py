"""Canonical problem parsing and anti-cheat finalization utilities.

This module implements the finalization rule from Goedel-Architect §C.2:
a submission is only accepted when it provides a self-contained proof of the
CANONICAL theorem.  The imports / set_option / open lines and the theorem
statement come verbatim from the canonical problem record; ONLY the ':= by …'
proof body is taken from the model's submission; all helper lemmas must be
'have' inside the proof (no extra top-level declarations); no 'axiom', no
'native_decide', and no import/open lines beyond the canonical ones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from local_goedel.domain.lean_check import CheckResult


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class CanonicalProblem:
    """Parsed representation of a canonical benchmark problem.

    Attributes:
        header:        imports + set_option + open lines, verbatim (may be
                       multi-line). Taken directly from the benchmark record's
                       'header' field, or extracted from lean4_code.
        pre_decls:     canonical declarations BEFORE the main theorem
                       (e.g. 'def Heidi : ℝ := 2.1'), verbatim; may be "".
        thm_name:      identifier of the main theorem, e.g. "arithmetic_4185".
        thm_signature: text AFTER the name and BEFORE ':=', verbatim/trimmed
                       (may be multi-line, e.g. multi-argument IMO theorems).
        keyword:       "theorem" or "lemma" as written in the canonical source.
    """
    header: str
    pre_decls: str
    thm_name: str
    thm_signature: str
    keyword: str = "theorem"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Preamble line types that belong in the header section.
_PREAMBLE_RE = re.compile(
    r'^\s*('
    r'import\s+'           # import Mathlib / import Aesop
    r'|set_option\s+'      # set_option maxHeartbeats 0
    r'|open\s+'            # open BigOperators …
    r'|--'                 # line comment
    r'|/\-'               # block comment start
    r'|-/'                # block comment end
    r'|$'                  # blank line
    r')'
)

# A top-level theorem/lemma declaration keyword at the start of a (stripped) line.
_DECL_RE = re.compile(r'^(theorem|lemma)\s+(\S+)(.*)', re.DOTALL)

# Recognise ':= by sorry' or ':= sorry' at the end of a declaration, including
# multi-line variants.  We look for the LAST ':=' followed by 'by sorry' or
# just 'sorry' (with optional whitespace).
_SORRY_BODY_RE = re.compile(r':=\s*by\s+sorry\s*$|:=\s*sorry\s*$', re.DOTALL)


def _find_main_theorem(body: str) -> Optional[re.Match]:
    """Return the re.Match for the ':= by sorry' / ':= sorry' theorem in body.

    Strategy: find ALL top-level theorem/lemma declarations that END with a
    sorry body, then return the last one (= the unproved target).  If none end
    with sorry, return the last theorem/lemma declaration overall.
    """
    # Split into candidate blocks: each block starts at a top-level decl line.
    # We iterate by finding all non-indented 'theorem'/'lemma' starts.
    decl_starts = [m.start() for m in re.finditer(r'^(?:theorem|lemma)\s+', body, re.MULTILINE)]

    if not decl_starts:
        return None

    blocks: list[str] = []
    for idx, start in enumerate(decl_starts):
        end = decl_starts[idx + 1] if idx + 1 < len(decl_starts) else len(body)
        blocks.append(body[start:end])

    # Prefer the last block that ends with ':= by sorry' / ':= sorry'.
    sorry_blocks = [b for b in blocks if _SORRY_BODY_RE.search(b)]
    candidate = sorry_blocks[-1] if sorry_blocks else blocks[-1]

    # Match the full declaration structure from the candidate block.
    return _DECL_RE.match(candidate)


def _parse_theorem_block(keyword: str, thm_name: str, rest: str) -> tuple[str, str]:
    """Extract (thm_signature, proof_trailer) from the text after the name.

    'rest' is everything on the same line and subsequent lines after the name.

    We split on the LAST ':=' that is followed by 'by sorry', 'sorry', 'by\n',
    or end-of-declaration.  The ':= by' / ':= sorry' terminator is stripped.

    Returns (thm_signature, proof_trailer) where proof_trailer includes the
    ':= …' part (may be empty if we stripped it).
    """
    # Normalise: strip leading whitespace from rest.
    rest = rest.strip()

    # Try to find ':= by sorry' or ':= sorry' as the terminator (the unproved
    # sorry placeholder).  We want the LAST such occurrence so that ':=' inside
    # binder defaults (rare) do not confuse us.
    # Design choice: search for ':= by sorry' first (preferred), then ':= sorry'.
    for pattern in (r':=\s*by\s+sorry', r':=\s*sorry'):
        m = None
        for mm in re.finditer(pattern, rest):
            m = mm  # take the last match
        if m is not None:
            sig = rest[:m.start()].strip()
            return sig, rest[m.start():]

    # Fallback: split on the first top-level ' := ' (with surrounding spaces),
    # or ':= by' if present.
    for pattern in (r'\s*:=\s*by\b', r'\s*:=\s'):
        m = re.search(pattern, rest)
        if m is not None:
            sig = rest[:m.start()].strip()
            return sig, rest[m.start():]

    # Last resort: the entire rest is the signature (no ':=' found).
    return rest, ""


def _split_formal_statement(formal_statement: str) -> tuple[str, str, str, str]:
    """Parse formal_statement into (pre_decls, keyword, thm_name, thm_signature).

    The formal_statement may contain:
    - Extra 'open' / comment lines BEFORE the main theorem declaration.
    - One or more 'def' / type declarations before the main theorem.
    - Inline comments on the same line as 'open' (e.g. '/- special open -/ open Finset').
    - A main theorem ending in ':= by sorry' or ':= sorry'.

    Returns:
        pre_decls    -- verbatim text before the main theorem (may be "")
        keyword      -- "theorem" or "lemma"
        thm_name     -- identifier
        thm_signature -- text between name and ':=' (trimmed)
    """
    lines = formal_statement.splitlines(keepends=True)

    # Find where the MAIN theorem/lemma declaration starts.
    # The main theorem is identified as per _find_main_theorem: the last
    # 'theorem'/'lemma' with ':= by sorry'/':= sorry'.  We collect
    # everything before it as pre_decls.

    # Collect non-indented 'theorem'/'lemma' line indices.
    decl_line_indices: list[int] = []
    for i, line in enumerate(lines):
        if re.match(r'^(theorem|lemma)\s+', line):
            decl_line_indices.append(i)

    if not decl_line_indices:
        # No theorem found – return empty pre_decls and let caller handle it.
        return formal_statement, "theorem", "", ""

    # Determine which declaration is the sorry-terminated target.
    # Build blocks (line idx → text from that line to the next decl).
    def _block_text(start_idx: int) -> str:
        if start_idx >= len(decl_line_indices) - 1:
            return "".join(lines[decl_line_indices[start_idx]:])
        next_start = decl_line_indices[decl_line_indices.index(start_idx) + 1] if False else (
            decl_line_indices[decl_line_indices.index(start_idx) + 1]
        )
        return "".join(lines[decl_line_indices[start_idx]:next_start])

    # Reconstruct ordered blocks properly
    main_decl_line_idx = decl_line_indices[-1]  # default: last theorem

    sorry_candidates = []
    for pos, line_idx in enumerate(decl_line_indices):
        next_line_idx = decl_line_indices[pos + 1] if pos + 1 < len(decl_line_indices) else len(lines)
        block = "".join(lines[line_idx:next_line_idx])
        if _SORRY_BODY_RE.search(block):
            sorry_candidates.append(line_idx)

    if sorry_candidates:
        main_decl_line_idx = sorry_candidates[-1]

    # pre_decls = everything before main theorem line
    pre_decls = "".join(lines[:main_decl_line_idx]).rstrip()

    # The main theorem block = from main_decl_line_idx to end of formal_statement
    thm_block = "".join(lines[main_decl_line_idx:])

    m = _DECL_RE.match(thm_block)
    if m is None:
        return pre_decls, "theorem", "", ""

    keyword = m.group(1)
    thm_name = m.group(2)
    rest = m.group(3)

    thm_signature, _ = _parse_theorem_block(keyword, thm_name, rest)

    return pre_decls, keyword, thm_name, thm_signature


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def split_canonical(
    lean4_code: str,
    formal_statement: Optional[str] = None,
    header: Optional[str] = None,
) -> CanonicalProblem:
    """Parse a benchmark record into a CanonicalProblem.

    Preferred path (structured benchmark record):
        If both `header` and `formal_statement` are given, use `header`
        verbatim and parse `formal_statement` to extract pre_decls, keyword,
        thm_name, and thm_signature.

    Fallback path (lean4_code only):
        Split the leading preamble (consecutive import / set_option / open /
        blank / comment lines) into `header`, and treat the remainder as the
        `formal_statement` body to parse.

    Locating the MAIN theorem:
        The main theorem is the LAST top-level 'theorem'/'lemma' declaration
        that ends in ':= by sorry' or ':= sorry'.  If none match, the last
        such declaration overall is used.  Everything in the body BEFORE that
        declaration is `pre_decls`.

    Args:
        lean4_code:       Full lean4_code field from benchmark record.
        formal_statement: Optional formal_statement field (structured path).
        header:           Optional header field (structured path).

    Returns:
        CanonicalProblem with all fields populated.
    """
    if header is not None and formal_statement is not None:
        # --- Structured path ---
        parsed_header = header
        pre_decls, keyword, thm_name, thm_signature = _split_formal_statement(formal_statement)
        return CanonicalProblem(
            header=parsed_header,
            pre_decls=pre_decls,
            thm_name=thm_name,
            thm_signature=thm_signature,
            keyword=keyword,
        )

    # --- Fallback: lean4_code only ---
    lines = lean4_code.splitlines(keepends=True)

    # Consume leading preamble lines: import / set_option / open / blank /
    # comment.  A line that is a 'theorem'/'lemma'/'def'/'abbrev' etc. at
    # column 0 terminates the preamble.
    _NON_PREAMBLE_RE = re.compile(r'^(theorem|lemma|def|abbrev|inductive|structure|class)\s+')
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _PREAMBLE_RE.match(line):
            header_lines.append(line)
            body_start = i + 1
        elif _NON_PREAMBLE_RE.match(line):
            body_start = i
            break
        else:
            # Unknown line at col 0 — stop preamble collection.
            body_start = i
            break

    parsed_header = "".join(header_lines).rstrip()
    body = "".join(lines[body_start:])

    pre_decls, keyword, thm_name, thm_signature = _split_formal_statement(body)

    return CanonicalProblem(
        header=parsed_header,
        pre_decls=pre_decls,
        thm_name=thm_name,
        thm_signature=thm_signature,
        keyword=keyword,
    )


def build_canonical_submission(
    canon: CanonicalProblem,
    proof_body: str,
    add_axiom_print: bool = True,
) -> str:
    """Build a complete, self-contained Lean file for submission checking.

    The file is constructed as:
        <header>
        (blank line)
        [<pre_decls>]
        (blank line if pre_decls non-empty)
        <keyword> <thm_name> <thm_signature> := <proof_body>
        (blank line + #print axioms <thm_name>  if add_axiom_print)

    NOTHING from the model's submission except the proof_body appears in the
    output.  A leading ':=' on proof_body is stripped if present (some models
    include it).  The file always ends with a trailing newline and uses Unix
    line endings ('\n').

    Args:
        canon:            The CanonicalProblem to build against.
        proof_body:       The proof body from the model (e.g. "by nlinarith").
        add_axiom_print:  Whether to append '#print axioms <name>'.

    Returns:
        Complete Lean file as a string with '\n' line endings.
    """
    # Normalise proof_body: strip a leading ':=' that the model may have included.
    body = proof_body.strip()
    if body.startswith(":="):
        body = body[2:].lstrip()

    parts: list[str] = []

    # Header (verbatim).
    parts.append(canon.header.replace("\r\n", "\n").replace("\r", "\n"))
    parts.append("")

    # Pre-declarations (if any).
    if canon.pre_decls:
        parts.append(canon.pre_decls.replace("\r\n", "\n").replace("\r", "\n"))
        parts.append("")

    # Main theorem line.
    thm_line = f"{canon.keyword} {canon.thm_name} {canon.thm_signature} := {body}"
    parts.append(thm_line)

    # Axiom print directive.
    if add_axiom_print:
        parts.append("")
        parts.append(f"#print axioms {canon.thm_name}")

    return "\n".join(parts) + "\n"


def scan_proof_body(proof_body: str) -> list[str]:
    """Scan a model's proof body for anti-cheat violations.

    Returns a (possibly empty) list of human-readable violation strings.
    An empty list means the proof body is clean.

    Rules enforced:
    1. Column-0 top-level declarations: any line whose FIRST token at column 0
       (no leading whitespace) is one of:
           theorem | lemma | def | abbrev | instance | inductive | structure
           | class | axiom | import | open | namespace | macro | notation
       These indicate injected top-level Lean declarations.  Exception: an
       'open X in' expression-level prefix is NOT flagged because it has
       ' in' on the same line (only bare 'open X' is flagged).
       Rationale: a legitimate proof body is entirely indented under 'by';
       column-0 keywords indicate the model tried to inject declarations
       outside the proof.

    2. native_decide (word boundary): triggers native code evaluation at
       elaboration time, which circumvents the axiom whitelist and can
       introduce 'Lean.ofReduceBool' / 'Lean.ofReduceNat'.

    3. sorry (word boundary): the proof is incomplete or uses the sorry axiom.

    4. axiom (word boundary, catching inline uses): the proof declares or
       references a new axiom.  This is already partially caught by rule 1
       (column-0 'axiom'), but this rule catches e.g. 'have h := @axiom_foo'.

    Conservative design: we only flag things we are confident about.
    False negatives (missing exotic abuse) are less harmful than false
    positives that reject valid proofs.
    """
    violations: list[str] = []

    # --- Rule 1: column-0 top-level declaration keywords ---
    # Match a line that starts with no whitespace and whose first token is a
    # reserved declaration keyword.  For 'open', we additionally require that
    # the line does NOT contain ' in' (which would make it an 'open X in'
    # expression prefix, which is legal inside a tactic proof).
    _TOP_LEVEL_KEYWORDS = (
        "theorem", "lemma", "def", "abbrev", "instance", "inductive",
        "structure", "class", "axiom", "import", "open", "namespace",
        "macro", "notation",
    )
    _kw_pattern = r'^(' + '|'.join(_TOP_LEVEL_KEYWORDS) + r')\b'
    _kw_re = re.compile(_kw_pattern, re.MULTILINE)

    for m in _kw_re.finditer(proof_body):
        kw = m.group(1)
        # Find the full line containing this match.
        line_start = proof_body.rfind('\n', 0, m.start()) + 1
        line_end = proof_body.find('\n', m.end())
        if line_end == -1:
            line_end = len(proof_body)
        full_line = proof_body[line_start:line_end]

        # Verify: match must be at column 0 (no leading whitespace on this line).
        col = m.start() - line_start
        if col != 0:
            continue  # indented → not a top-level injection

        # Special exception for 'open X in' (expression-level open).
        if kw == "open" and re.search(r'\bin\b', full_line):
            continue

        violations.append(f"injected top-level declaration: '{kw}' at column 0")

    # --- Rule 2: native_decide ---
    if re.search(r'\bnative_decide\b', proof_body):
        violations.append("uses native_decide")

    # --- Rule 3: sorry ---
    if re.search(r'\bsorry\b', proof_body):
        violations.append("contains sorry")

    # --- Rule 4: axiom (inline, not already caught by rule 1) ---
    # Rule 1 catches 'axiom' at column 0 as a declaration keyword.
    # Here we catch inline usages like 'have h := @myAxiom' — but we only
    # flag the bare word 'axiom' (which appears in e.g. 'noncomputable axiom').
    # The column-0 case is already reported above; we avoid double-reporting.
    if re.search(r'\baxiom\b', proof_body):
        # Check if already reported via rule 1 (column 0 case).
        already_flagged = any("'axiom' at column 0" in v for v in violations)
        if not already_flagged:
            violations.append("declares axiom")

    return violations


# ---------------------------------------------------------------------------
# Axiom whitelist
# ---------------------------------------------------------------------------

#: The three axioms that every Lean 4 / Mathlib proof legitimately depends on.
ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})


def axiom_whitelist_ok(check: CheckResult, thm_name: str) -> tuple[bool, list[str]]:
    """Check whether a theorem's axiom dependencies are all whitelisted.

    Looks for an info message of the form:
        '<thm_name>' depends on axioms: [axiom1, axiom2, ...]

    The ONLY allowed axioms are: propext, Classical.choice, Quot.sound.
    Anything else (sorryAx, Lean.ofReduceBool, Lean.ofReduceNat, user axioms)
    is a violation.

    If NO 'depends on axioms' message is present, we fall back to:
        ok = not check.mentions_sorry()
        offending = []
    A theorem with zero axiom dependencies is fine; a sorry-tainted result
    is not.  This is consistent with axiom_check.uses_sorry's fallback.

    Args:
        check:    A CheckResult from the Lean REPL for the submitted file.
        thm_name: The canonical theorem name (must match the info message).

    Returns:
        (ok, offending) where ok is True iff all axioms are whitelisted and
        offending is the list of disallowed axiom names.
    """
    prefix = f"'{thm_name}' depends on axioms"

    for msg in check.messages:
        if msg.severity == "info" and msg.data.startswith(prefix):
            # Parse the bracketed list: '...axioms: [a, b, c]'
            bracket_m = re.search(r'\[([^\]]*)\]', msg.data)
            if bracket_m is None:
                # Malformed message — treat as suspicious.
                return False, []
            raw = bracket_m.group(1)
            axiom_names = [a.strip() for a in raw.split(",") if a.strip()]
            offending = [a for a in axiom_names if a not in ALLOWED_AXIOMS]
            return (len(offending) == 0), offending

    # Fallback: no axiom info message present.
    ok = not check.mentions_sorry()
    return ok, []
