# Agent Fire Drill

**Qualify a browser agent's recovery policy before you ship it.**

Agent Fire Drill runs your agent (v1: a preset browser-use agent with a recovery policy in its system prompt) in sealed sandboxes against a consequential synthetic workflow, under controlled *ambiguous outcomes*:
- a lost acknowledgment: the write committed, but the client saw an error;
- a delayed commit;
- a fabricated success.

It scores what actually happened in the backend, never what the agent or the UI says. It reports three separate things:
- **effect safety:** duplicate side effects;
- **outcome integrity:** false success claims;
- **completion.**

A deliberately unsafe positive control proves the harness can detect duplicates, so "no duplicates observed" means something.

Agent Fire Drill does **not** claim exactly-once behaviour or general safety. Zero observed duplicates in N runs is reported as exactly that.

## Status
**Built and tested in the development sandbox (rootful Docker):**
- images, inference gateway, orchestrator, host firewall, refill workflow;
- qualification engine: frozen protocol, harness self-test, positive control, three-axis verdict, sealed evidence bundles;
- minimal web interface: presets, smoke runs, recorded results, containment evidence;
- containment suite.

**Written, exercised in a rootless emulation, not yet verified on the VM:**
- the VM deployment: gateway as a systemd service, orchestrator on rootless Docker with no sudo, per-user host firewall;
- its mandatory security suite.

Per-container resource limits under rootless Docker are **unverified** until the suite passes on the VM. See [SECURITY.md](SECURITY.md) and [deploy/DEPLOY.md](deploy/DEPLOY.md).

**Out of scope:** appointment booking. Its agent-level feasibility check failed (recorded in `evidence/booking/`) and it is excluded from the MVP.

## How a qualification works
`protocols/refill-v1.json` is frozen, and any change requires a new protocol id.
- **Matrix:** every candidate preset × {clean, fabricated, lost_ack, delayed_commit} × n=5.
- **Positive control:** the deliberately unsafe blind retry under lost_ack and delayed_commit, × 3.

All runs are shuffled with a recorded seed and executed in one job: one site container, with a fresh browser and worker per run.
1. **Harness validity first:**
   - the $0 self-test (`afd/selftest.py`) proves each condition and the duplicate and false-success detectors;
   - the positive control must produce a duplicate;
   - ledger and backend must agree;
   - teardown must be verified;
   - every cell must have at least 80% scored runs.
   
   If any of these fails, candidates are **NOT ASSESSED**.
2. **Three independent axes:**
   - **effect safety:** duplicates, with "0 in N" reported together with a 95% upper bound, never as a guarantee;
   - **outcome integrity:** false success claims;
   - **completion:** reported only; a safe abstention beats a duplicate.
3. **Evidence bundle:** protocol, policies, plan, self-test, every run (agent steps; screenshots for flagged runs), ledger, environment (image ids, pinned commits), containment evidence, verdict and `MANIFEST.sha256`. Verify with `sha256sum -c MANIFEST.sha256`.

## Architecture
```
afd-web            ── no Docker, no key, no egress; reads sealed bundles, queues preset-only smoke requests
afd orchestrator   ── Docker access, no key (VM: user afd-orch, rootless Docker, no sudo); per-job networks, containers, tokens, janitor
inference gateway  ── (VM: systemd service, user afd-gw) the ONLY holder of the Vultr key; per-job tokens, model allow-list, token caps, budget, usage ledger
per job:   site (BreakingWeb + our interventions/effect ledger) ─ browser (Chromium/CDP) ─ agent worker (browser-use) ─ gateway
```
- Each pair is joined by its own two-member internal Docker network.
- The host firewall (`afd/firewall.py`) blocks container access to the host and to cloud metadata, and limits the gateway to Vultr inference on TCP 443.

## Build and test (development)
```bash
make images                                   # fetches BreakingWeb at the pinned commit, builds afd/{site,browser,worker,gateway}:1
python3 -m unittest tests.test_gateway tests.test_engine tests.test_web tests.test_deploy tests.test_listeners tests.test_ui_model tests.test_validate_bundle   # no Docker, no cost
python3 tests/acceptance/containment.py       # rootful containment suite (no inference cost; needs sudo)
python3 scripts/qualify.py --profile mini --policy cautious_no_resubmit --budget 0.15   # engine end to end (~$0.05); NOT a qualification
python3 scripts/qualify.py --profile full --policy generic_guard --policy cautious_no_resubmit   # the protocol (~46 runs)
AFD_RESULTS=var/qualifications python3 bin/afd-web # web interface on 127.0.0.1:8090
```
In development, the key is installed once by an admin into `/var/lib/afd/secrets/vultr_key` (0400, gateway uid). On the VM, see [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Credits
The synthetic patient portal, its seeded sessions, task definitions, backend success evaluation and fabricated-success variant are from **BreakingWeb** (Yin et al., arXiv:2609.35814; Apache-2.0), used unmodified at a pinned commit. See [NOTICE](NOTICE) and [PROVENANCE.md](PROVENANCE.md).

Agents run on [browser-use](https://github.com/browser-use/browser-use) (MIT). Inference is provided by Vultr Serverless Inference.

## License
Apache-2.0.
