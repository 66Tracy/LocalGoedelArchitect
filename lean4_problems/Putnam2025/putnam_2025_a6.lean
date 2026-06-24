import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open Classical

theorem putnam_2025_a6 (b : ℕ → ℤ)
  (hb0 : b 0 = 0)
  (hb : ∀ n, b (n + 1) = 2 * b n ^ 2 + b n + 1)
  (k : ℕ)
  (hk : k ≥ 1):
  padicValInt 2 (b (2 ^ (k + 1)) - 2 * b (2 ^ k)) = 2 * k + 2 := by
  sorry
