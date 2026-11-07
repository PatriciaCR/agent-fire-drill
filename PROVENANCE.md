# Provenance

| Path | Origin | Notes |
|---|---|---|
| BreakingWeb app, sessions, seeds, tasks, `evaluate`, fabricated-success variant | **BreakingWeb** (Apache-2.0, commit `2de1598`) | fetched by `scripts/fetch_breakingweb.sh`, never modified, not vendored |
| `images/site/afd_site.py` (lost-ack / delayed-commit interventions, effect ledger, control endpoint) | Agent Fire Drill | wraps BreakingWeb's ASGI app from outside; derived from our experiment 06 wrappers |
| `images/site/Dockerfile` | Agent Fire Drill | builds BreakingWeb's `patient_portal` frontend from the pristine export |
| `images/worker/worker.py` | Agent Fire Drill | browser-use agent loop. Its banned-action list and goal wording follow BreakingWeb's stock harness (`stock_browseruse_eval.py`, Apache-2.0) for comparability with our experiments |
| `images/browser/*`, `images/gateway/*`, `afd/*`, `presets/*`, `tests/*` | Agent Fire Drill | — |
| browser-use | upstream, MIT | installed from PyPI at a pinned version |
