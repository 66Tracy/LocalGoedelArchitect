import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open Classical Set Real

theorem putnam_2025_a2 :
  IsGreatest {a : ℝ | ∀ x ∈ Icc 0 π, a * x * (π - x) ≤ sin x} (1 / π) ∧
  IsLeast {b : ℝ | ∀ x ∈ Icc 0 π, b * x * (π - x) ≥ sin x} (4 / π ^ 2) := by
  sorry
