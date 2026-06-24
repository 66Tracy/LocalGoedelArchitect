import Mathlib
set_option pp.numericTypes true
set_option pp.funBinderTypes true
set_option maxHeartbeats 0
open Classical Finset

abbrev State (n : ℕ) : Type := (Fin n → Fin 3) × Finset (Fin n → Fin 3)

abbrev State.validNext {n : ℕ} (s : State n) : Finset (State n) :=
  {s' |
    s'.1 ∈ s.2 ∧
    (∃ (i : Fin n) (δ : ({1, -1} : Finset ℤ)),
      s'.1 i = s.1 i + δ.val ∧ ∀ j ≠ i, s'.1 j = s.1 j) ∧
    s'.2 = s.2.erase s'.1}

abbrev State.init (n : ℕ) : State n :=
  ⟨fun _ ↦ 0, Finset.univ.erase (fun _ ↦ 0)⟩

abbrev Strategy (n : ℕ) : Type := State n → State n

abbrev IsRollout {n : ℕ} (a b : Strategy n) (s : ℕ → State n) : Prop :=
  s 0 = State.init n ∧
  (∀ i, Even i → s (i + 1) = a (s i)) ∧
  (∀ i, Odd i → s (i + 1) = b (s i))

theorem putnam_2025_a3 (n : ℕ) (_hn : 1 ≤ n) :
  ∃ b : Strategy n, ∀ a : Strategy n,
    ∀ s : ℕ → State n, IsRollout a b s →
      ∃ k : ℕ, Even k ∧
        (∀ i < k, s (i + 1) ∈ (s i).validNext) ∧
        s (k + 1) ∉ (s k).validNext := by
  sorry
