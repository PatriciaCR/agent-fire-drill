# Agent Fire Drill

**Find out what your agent really does when a consequential write goes wrong, before you ship it.**

Agent Fire Drill (AFD) runs an agent or automation against a sandboxed copy of a workflow. It injects the failures that
make outcomes ambiguous, on the agent's **real** requests:
- a **lost acknowledgement:** the write committed, but the client saw an error;
- a **delayed commit:** the write lands after the client gave up;
- a **pre-commit failure:** the write never reached the backend;
- a **fabricated success:** the client is told it worked, and nothing happened.

It then scores what **actually happened in the backend**, per object and checked by an independent oracle, never what
the agent or the UI claims. It reports three separate things:
- **effect safety:** duplicate or unauthorised effects;
- **outcome integrity:** false success claims;
- **completion.**

Deterministic positive controls prove the harness can catch a duplicate, so "no duplicates observed" means something.
AFD never claims exactly-once behaviour: "0 duplicates in N runs" is reported as exactly that.

## Two ways to use it
**1. Try a Demo**, live on Vultr at https://45-63-90-26.sslip.io:
- a browser-use agent on a synthetic patient-portal refill workflow;
- Vultr Serverless Inference behind a key-holding gateway;
- the orchestrator on rootless Docker with no sudo, and a per-user host firewall;
- published only after the mandatory security suite passed on the VM.

**2. Test Your Workflow** (operator-run CLI; no public submissions):
- **Bring your own app:** a JSON contract describes the containerised app, the consequential write, and an independent
  per-object oracle (e.g. a read-only SQL query).
- **Bring your own agent:** an agent spec names a digest-pinned image, a command, an environment allowlist and how the
  agent reports success (exit status or one JSON line). Tools that aren't in the image (a CLI binary, test code) come
  from a **hash-bound, read-only workspace**. Its tree hash is part of the operator's approval, and the run mounts a
  verified private copy.
- Every run is isolated: the agent reaches the app **only** through AFD's fault proxy, with no internet, no
  credentials beyond the declared ones, non-root, a read-only filesystem and resource limits. The results are sealed in
  a verifiable evidence bundle.

```
afd contract check|approve|probe CONTRACT --ui UI        # safety lint, operator approval, $0 self-test
afd agent check|approve AGENT.json --contract CONTRACT   # agent image, command, workspace hash
afd qualify --contract CONTRACT --ui UI --agent-spec AGENT.json
afd contract verify-bundle BUNDLE                        # integrity, recomputed verdict, approval binding
```

## A third-party tool, unmodified: Gitea's official CLI `tea`
`tea` 0.16.0 (binary SHA-256 checked against Gitea's published checksum) ran as the agent under test against a
sandboxed Gitea. AFD intercepted its real `POST /api/v1/repos/…/issues` and checked every outcome against the
database:

| Fault on `tea`'s real request | What `tea` reported | What the database shows |
|---|---|---|
| none | success | 1 issue |
| lost acknowledgement | failure ("503") | **1 issue**: an under-claim |
| delayed commit | failure ("503") | **1 issue**: an under-claim |
| fabricated success | **success** | **0 issues**: a false success claim |
| lost ack / delayed commit, with a **separate blind-retry wrapper** around `tea` (the retry belongs to the wrapper, not to `tea`) | success | **2 issues: duplicates**, caught in 6/6 runs |

Details and sealed bundles: [docs/M6C_BYOA_WORKSPACE_TEA.md](docs/M6C_BYOA_WORKSPACE_TEA.md),
[docs/M6C_VALIDATION_PASS.md](docs/M6C_VALIDATION_PASS.md).

## What is verified where
| | Verified |
|---|---|
| **Vultr VM** | the live demo; the security suite; the rootless deployment smoke of the generalized engine (isolation, enforced resource limits, lost ack on an agent's real write with one oracle-verified effect, evidence integrity, clean teardown), using AFD's synthetic fixtures ([evidence](docs/VULTR_DEPLOYMENT_EVIDENCE.md)) |
| **Local sandbox** | the generalized engine on four open-source apps (Vikunja, Gitea, Listmonk, WooCommerce) and controlled fixtures; contract-aware verdicts (e.g. WooCommerce's own duplicate protection); late backend commits; the external-agent adapter; the `tea` integration above |

## Limitations (exact)
- **Third-party integration:** one third-party CLI (`tea`), on one application (Gitea), validated **locally only**.
  This is not broad third-party generalization.
- **LLM agents through the generic path:** **not** supported yet. The only LLM agent is the demo's browser-use worker.
- **Onboarding:** the developer must declare the route their tool actually writes through, choose a base image that
  supplies its runtime dependencies (`tea` needs `git`), use per-architecture binaries and approvals, add one wiring
  line (base URL and target), and provide realistic fake responses.
- **Fixture-only behaviour:** late backend commits were exercised only in a synthetic fixture app.
- **Public demo:** the live qualification's harness correctly went **invalid** (agent grounding failure in run r027;
  [root cause](docs/R027_ROOT_CAUSE.md)), so its candidates are *not assessed*.
- **Erratum:** an early scripted read-back bug is documented ([erratum](docs/ERRATUM_SCRIPTED_READBACK.md)).

## Architecture
```
afd-web            ── no Docker, no key, no egress; reads sealed bundles, queues preset-only smoke requests
afd orchestrator   ── Docker access, no key (VM: user afd-orch, rootless Docker, no sudo); per-job networks, containers, janitor
inference gateway  ── (VM: systemd service, user afd-gw) the ONLY holder of the Vultr key; per-job tokens, model allow-list, budget, ledger
per job:   app services ── fault proxy ── agent (browser + worker, scripted policy, or your own agent container)
```
- Every hop is its own internal Docker network. The agent network reaches only the proxy.
- The host firewall blocks container access to the host and to cloud metadata, and limits the gateway to Vultr
  inference on TCP 443.
- See [SECURITY.md](SECURITY.md) and [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Build and test (development)
```bash
make images            # builds afd/{site,browser,worker,gateway}:1 (fetches BreakingWeb at its pinned commit)
python3 -m unittest tests.test_gateway tests.test_engine tests.test_web tests.test_deploy tests.test_listeners tests.test_ui_model \
  tests.test_validate_bundle tests.test_contract tests.test_scoring_objects tests.test_worker_v2 tests.test_workflow tests.test_scoring_v2 \
  tests.test_scripted_v2 tests.test_scoring_v3 tests.test_agent_spec tests.test_rootless_compat tests.test_vm_smoke tests.test_agent_workspace   # 193 tests, no Docker, no cost
python3 deploy/vm/generic_smoke.py   # $0 deployment smoke of the generalized engine (needs the approved M5 fixture)
```
The demo's protocol (`protocols/refill-v1.json`) and the generic protocols (`protocols/contract-v*.json`) are frozen;
any change gets a new protocol id.

## Evidence index
- Submission notes: [docs/SUBMISSION_NOTES.md](docs/SUBMISSION_NOTES.md)
- Vultr deployment evidence: [docs/VULTR_DEPLOYMENT_EVIDENCE.md](docs/VULTR_DEPLOYMENT_EVIDENCE.md)
- Third-party CLI (`tea`): [docs/M6C_BYOA_WORKSPACE_TEA.md](docs/M6C_BYOA_WORKSPACE_TEA.md), [docs/M6C_VALIDATION_PASS.md](docs/M6C_VALIDATION_PASS.md)
- External-agent adapter: [docs/M6A_EXTERNAL_AGENT_ADAPTER.md](docs/M6A_EXTERNAL_AGENT_ADAPTER.md)
- Compatibility study (four open-source apps): [docs/COMPAT_STUDY_REPORT.md](docs/COMPAT_STUDY_REPORT.md)
- Contract-aware verdicts and late commits: [docs/M4_CONTRACT_AWARE_VERDICTS.md](docs/M4_CONTRACT_AWARE_VERDICTS.md), [docs/M5_PRECOMMIT_AND_LATE_EFFECTS.md](docs/M5_PRECOMMIT_AND_LATE_EFFECTS.md)
- Demo qualification root cause: [docs/R027_ROOT_CAUSE.md](docs/R027_ROOT_CAUSE.md); erratum: [docs/ERRATUM_SCRIPTED_READBACK.md](docs/ERRATUM_SCRIPTED_READBACK.md)

## Credits
- The synthetic patient portal, its seeded sessions, task definitions, backend success evaluation and
  fabricated-success variant are from **BreakingWeb** (Yin et al., arXiv:2609.35814; Apache-2.0), used unmodified at a
  pinned commit. See [NOTICE](NOTICE) and [PROVENANCE.md](PROVENANCE.md).
- The demo agent runs on [browser-use](https://github.com/browser-use/browser-use) (MIT).
- The third-party test used Gitea's official CLI [tea](https://gitea.com/gitea/tea) (MIT), unmodified and not
  redistributed.
- Inference is provided by Vultr Serverless Inference.

## License
Apache-2.0.
