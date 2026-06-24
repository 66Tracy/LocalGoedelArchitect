import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
set_option linter.unusedVariables false

def MatrixProp (n : ℕ) (A : ℕ → ℕ → ℕ) : Prop :=
  (∀ i j : ℕ, 1 ≤ i → i ≤ n → 1 ≤ j → j ≤ n → i + j ≤ n → A i j = 0) ∧
  (∀ i j : ℕ, 1 ≤ i → i ≤ n - 1 → 1 ≤ j → j ≤ n →
    A (i + 1) j = A i j ∨ A (i + 1) j = A i j + 1) ∧
  (∀ i j : ℕ, 1 ≤ i → i ≤ n → 1 ≤ j → j ≤ n - 1 →
    A i (j + 1) = A i j ∨ A i (j + 1) = A i j + 1)

def colSum (n : ℕ) (A : ℕ → ℕ → ℕ) (j : ℕ) : ℕ :=
  Finset.sum (Finset.range n) (fun i => A (i + 1) j)

def colNonzeros (n : ℕ) (A : ℕ → ℕ → ℕ) (j : ℕ) : ℕ :=
  ((Finset.range n).filter (fun i => A (i + 1) j ≠ 0)).card

def totalSum (n : ℕ) (A : ℕ → ℕ → ℕ) : ℕ :=
  Finset.sum (Finset.range n) (fun j => colSum n A (j + 1))

def nonzeroCount (n : ℕ) (A : ℕ → ℕ → ℕ) : ℕ :=
  Finset.sum (Finset.range n) (fun j => colNonzeros n A (j + 1))

theorem putnam_2025_b4 (n : ℕ)
  (hn : 2 ≤ n)
  (A : ℕ → ℕ → ℕ)
  (h : MatrixProp n A):
  3 * totalSum n A ≤ (n + 2) * nonzeroCount n A := by
  sorry
