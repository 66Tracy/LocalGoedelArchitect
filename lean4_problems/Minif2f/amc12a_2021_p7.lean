import Mathlib

set_option maxRecDepth 10000 in
set_option maxHeartbeats 1000000 in
/- Let $N = 34 \cdot 34 \cdot 63 \cdot 270$. What is the ratio of the sum of the odd divisors of
$N$ to the sum of the even divisors of $N$? Show that it is 1:14. -/
theorem amc12a_2021_p7 :
    (∑ k ∈ (Nat.divisors (34 * 34 * 63 * 270)).filter (fun x => Odd x), k) * 14 =
    (∑ k ∈ (Nat.divisors (34 * 34 * 63 * 270)).filter (fun x => Even x), k) := by
  decide
