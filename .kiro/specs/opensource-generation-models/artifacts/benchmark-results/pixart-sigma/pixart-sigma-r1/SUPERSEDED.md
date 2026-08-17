# SUPERSEDED — not the canonical metrics set for PixArt-Sigma

Canonical evidence for this model: `../run-001/`.

This directory holds the results of a **parallel execution of task 4.1** that
ran against the shared small-class instance behind ledger row `pixart-r1`
(`i-01b4e3e5e2fe0eb99`), whereas the canonical sigma run
(`../run-001/`) ran on `i-01361c1f436a6cab7` (ledger row `pixart-r2`). The
`instance_hours` / `estimated_cost_usd` fields here split the `pixart-r1`
instance's 1.2061 h evenly between the alpha and sigma runs and **must not be
summed** with the `run-001` attributions.

Collapsed out of the Phase D cost reconciliation (task 5.2) per the
duplicate-execution note in `../../README.md`: one metrics set per model, and
the cost ledger table in `../../README.md` is the single source of truth for
actual spend. `metrics.json` here carries
`billing_reconciliation.status = "superseded-not-attributed"`.

Retained (not deleted) because the images under `outputs/` are the only
surviving copies of that execution's generations — the tagged benchmark bucket
objects for the `pixart-sigma-r1` prefix were overwritten by the canonical run
and the bucket was deleted at teardown.
