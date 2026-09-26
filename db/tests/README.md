# Database-level tests

Built in **M1** (implementation plan): the tenant-isolation suite must exist before any
business module, per database spec §8.2 step 8 — "the safety net every later change relies
on, and the only thing that catches an autogenerate mistake" (§8.3).

- `rls_isolation.sql` — two-tenant fixtures; asserts tenant A's session can never see,
  modify or infer tenant B's rows, by list, by direct id, by filter, or by FK traversal
  (ADR-001 §"Verification").
- `constraints.sql` — CHECK constraints and exclusion constraints behave as specified.
- `explain_regression.sql` — `EXPLAIN` on the nine hot queries in database spec §3.7,
  guarding against an index regression.
