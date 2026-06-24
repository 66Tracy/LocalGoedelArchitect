import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open Finset

def f (n : ℕ) (s : Fin n → ℤˣ) : ℕ :=
  Finset.card {σ : Equiv.Perm (Fin (n + 1)) |
    ∀ i : Fin n, 0 < (s i : ℤ) * ((σ i.succ : ℤ) - (σ i.castSucc : ℤ))}

theorem putnam_2025_a5
    (n : ℕ)
    (hn : 1 ≤ n)
    (s : Fin n → ℤˣ) :
    (∀ t : Fin n → ℤˣ, f n t ≤ f n s) ↔
    (∀ i : Fin n, s i = (-1) ^ (i.val + 1)) ∨ (∀ i : Fin n, s i = (-1) ^ i.val) := by
    sorry
