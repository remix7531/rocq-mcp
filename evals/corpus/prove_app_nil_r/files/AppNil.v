From Coq Require Import List.

Theorem app_nil_right : forall (A : Type) (l : list A), app l nil = l.
Admitted.
