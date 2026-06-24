import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open Classical

theorem putnam_2025_a1 (m n : ℕ → ℕ)
  (h0 : m 0 > 0 ∧ n 0 > 0 ∧ m 0 ≠ n 0)
  (hm : ∀ k : ℕ, m (k + 1) = ((2 * m k + 1) / (2 * n k + 1) : ℚ).num)
  (hn : ∀ k : ℕ, n (k + 1) = ((2 * m k + 1) / (2 * n k + 1) : ℚ).den):
  {k | ¬ (2 * m k + 1).Coprime (2 * n k + 1)}.Finite := by
  sorry
