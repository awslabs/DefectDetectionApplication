# Benchmark Harness

Plain Python scripts for the opensource-generation-models exploration
(protocol: `../benchmark-protocol.md`). Never deployed as portal
infrastructure; run from the engineer's workstation or the benchmark instance.

| File | Purpose |
|---|---|
| `cost_cap.py` | `should_provision(spend, projected, cap)` pure Cost_Cap gate (Property 3) |
| `ledger.py` | read/append/update helpers for the cost table in `../benchmark-results/README.md` |
| `runner.py` | case loop with per-case failure isolation (Property 5), metrics.json writer + schema assertion (Property 4) — pure, no GPU deps |
| `run_driver.py` | diffusers-based driver run on the GPU instance (imports torch/diffusers lazily) |
| `user_data.sh.tmpl` | EC2 user-data with self-terminating wall-clock shutdown guard |
| `quota_audit.py` | read-only GPU quota audit (EC2 G/P/VT vCPU + SageMaker endpoint quotas), Req 3.4 |
| `cases/` | frozen test-case inputs + `cases.json` manifest + deterministic generators (`generate_cases.py` synthetic textures, `prepare_cookie_cases.py` real cookie imagery from `s3://ryvan-cookies/training-images/`) |
| `tests/` | pytest suite gating provisioning (run: `python3 -m pytest tests/ -v`) |

Every AWS resource any of these scripts create must carry the tag
`exploration=opensource-generation-models`. `quota_audit.py` and the
describe-stacks snapshot are read-only and are not Benchmark_Infrastructure.
