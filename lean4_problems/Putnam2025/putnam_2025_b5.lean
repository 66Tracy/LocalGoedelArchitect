import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open Finset ZMod Classical

theorem putnam_2025_b5 (p : ℕ)
  [hp : Fact p.Prime]
  (hp3 : 3 < p):
  (#{k : ZMod p | k ≠ 0 ∧ k ≠ -1 ∧ (k + 1)⁻¹.val < k⁻¹.val} : ℝ) > p / 4 - 1 := by
  sorry
