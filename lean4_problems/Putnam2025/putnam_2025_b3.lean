import Mathlib
set_option maxHeartbeats 0
open Classical

theorem putnam_2025_b3 (S : Set ℕ)
  (hS : S.Nonempty)
  (_h0 : ∀ n ∈ S, 0 < n)
  (h : ∀ n ∈ S, ∀ d : ℕ, 0 < d → d ∣ 2025 ^ n - 15 ^ n → d ∈ S)
  (n : ℕ)
  (hn : 0 < n):
  n ∈ S := by
  sorry
