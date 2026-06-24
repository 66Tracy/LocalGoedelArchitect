import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open EuclideanGeometry Affine Simplex

theorem putnam_2025_b1 (c : EuclideanSpace ℝ (Fin 2) → Bool)
  (hc : ∀ t : Triangle ℝ (EuclideanSpace ℝ (Fin 2)), c (t.points 0) = c (t.points 1) → c (t.points 0) = c (t.points 2) →
    c (t.points 0) = c (circumcenter t)):
  ∃ c0 : Bool, ∀ A, c A = c0 := by
  sorry
