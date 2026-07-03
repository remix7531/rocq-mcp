From Coq Require Import Arith.

Theorem add_0_r_broken : forall n : nat, n + 0 = n.
Proof.
  intros n. reflexivity.
Qed.
