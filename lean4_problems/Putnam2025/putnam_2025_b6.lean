import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open BigOperators Real Nat Topology Rat

theorem putnam_2025_b6 :
  IsGreatest {r : ℝ | ∃ g : ℕ+ → ℕ+, ∀ n : ℕ+, (g (n + 1) : ℝ) - (g n : ℝ) ≥ (g (g n) : ℝ) ^ r} (1/4 : ℝ) := by
  sorry
