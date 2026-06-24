import Mathlib

/-- (a) Find all positive integers $n$ for which $2^n-1$ is divisible by $7$.
(b) Prove that there is no positive integer $n$ for which $2^n+1$ is divisible by $7$. -/
theorem imo_1964_p1_a (n : ℕ) (hn : 0 < n) : 7 ∣ 2 ^ n - 1 ↔ 3 ∣ n := by
  sorry

theorem imo_1964_p1_b (n : ℕ) (hn : 0 < n) : ¬7 ∣ 2 ^ n + 1 := by
  sorry
