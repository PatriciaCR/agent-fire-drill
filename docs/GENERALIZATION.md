# Generalisation: "Try a Demo" + "Test Your Workflow" (evaluation, 2026-11-07)

## Recommendation
Build the two-path product:
1. **Try a Demo** stays exactly as deployed: the instrumented synthetic refill workflow, preset LLM policies, real recorded results. It remains the zero-setup entry point and the reference implementation.
2. **Test Your Workflow** is built on the generic layer evaluated here: a developer-supplied contract + container image, reusable HTTP fault primitives, and the *same* scorer and qualification engine.

The executable proof below shows that the fault injection, scoring and qualification mechanics transfer to an independent application, configured only by a contract. It does **not** yet show that LLM browser agents behave the same way through the generic path. That needs one approved inference run (see "Next steps"). Nothing here claims arbitrary-agent or arbitrary-site support.

## What is reusable, and what was app-specific
| Layer | Before | Now |
|---|---|---|
| Fault injection | In-process ASGI wrapper bound to BreakingWeb (`images/site/afd_site.py`) | **Generic HTTP reverse proxy** (`afd/faults/proxy.py`): lost_ack, delayed_commit, fabricated; configured by the contract only |
| Effect truth | BreakingWeb-specific reads (`afd/adapters/refill.py`) | **Contract-declared verifier endpoint**: count of items, or decrease of a value |
| Scorer / verdict / plan / bundles | `afd/qualify.py`, `afd/bundle.py` | **Unchanged and reused**. One backward-compatible generalisation: `plan()` takes the positive control from the protocol and accepts a policy registry (refill-v1 behaviour identical, tested) |
| Containment | Job + hardening + internal networks | **Reused**: `afd/dockerops.py` hardening, labels and janitor; app, proxy and agent on internal networks |
| Agents | browser-use + LLM presets | Unchanged for the demo. The proof uses **deterministic scripted policies** (`afd/agents/scripted.py`) so that it costs $0 and is reproducible |

## Minimum safe integration contract (v0, `afd/contract.py`)
A developer supplies one JSON file plus a container image:
- **authorization**:
  - owner and attestation;
  - `synthetic_data: true`;
  - `runs_in_sandbox: true`.
  
  v0 accepts **only container images run inside AFD's sealed networks**. Remote URLs are refused, so the fault injector and agents can never be pointed at a third-party or production site.
- **app**: image, command, port, uid and health path. Code mounts are read-only and repo-relative only.
- **scenario**: an endpoint that creates fresh, isolated state and returns a scope id. This is how runs stay independent, and how a staging app avoids touching real data.
- **scope**: where each action request carries that id (JSON body field, query parameter or header).
- **action**: the consequential write (method, path template, body template, path regex).
- **user_check**: the user-visible state an agent may consult. It is part of the app's normal surface.
- **effects**: a **verifier-only** endpoint that counts committed effects. This is the backend truth. It is cross-checked against the proxy's own ledger in every run: if they disagree, harness validity fails.
- **intent**: the authorised count (`exactly: N`).
- **fabricated**: the success-shaped response the fabricated primitive returns.

**Developer cost:** a scenario-setup endpoint and an effect-count endpoint on a staging build. The expense demo needed 2 small routes. BreakingWeb already had both.

## Executable proof (`scripts/reuse_proof.py`, $0)
**Apps:**
- `examples/expense-app`: an independent Node.js app (different stack, domain, routes and data model; no AFD hooks).
- BreakingWeb refill served **without** our in-process wrapper.

**What both runs used:**
- the same proxy, contract runner, engine and scorer;
- protocol `contract-v0` (frozen; scripted policies; n = 3 per cell; positive control 2 × 2; delay 6 s);
- 28 runs per app.

**Result: all 10 pre-registered criteria PASS** (`evidence/reuse/report-2026-11-07.json`, bundles alongside):

| # | Criterion | Result |
|---|---|---|
| C1 | Same fault injector: identical `proxy.py` sha256 in both bundles | PASS |
| C2 | Same scorer and engine: identical `qualify.py` sha256; each `verdict.json` recomputes exactly | PASS |
| C3 | Both bundles verify | PASS (37 files each) |
| C4 | Generic self-test G1–G5 passes on both apps, against each app's own effect endpoint | PASS |
| C5 | Harness valid on both; positive control duplicates detected | PASS (4/4 each) |
| C6 | Same discrimination on both. **Verify-no-resubmit:** safety and integrity PASS. **Trust-the-response:** integrity FAIL, with false claims only under fabricated (3/3). Every cell identical across apps | PASS |
| C7 | On refill, the generic proxy reproduces the existing wrapper's self-test effect counts exactly (clean 1; lost_ack 1→2; fabricated 0; delayed 0→1) | PASS |
| C8 | No app identifier in any generic module; all app knowledge in `contracts/*.json` | PASS |
| C9 | The agent network cannot reach the app directly, cannot use the proxy's control API (403), and has no internet | PASS |
| C10 | `protocols/refill-v1.json` byte-identical; existing unit tests pass | PASS |

## What this does not show yet (honest limits)
- **LLM browser agents through the generic path.** The scripted policies call the HTTP API through the proxy. An LLM agent driving the app's UI through the proxy is plausible, because UI requests are the same HTTP writes, but it is untested.
- **Cookie or session-based scope.** Many real apps carry identity in a session cookie. v0 supports body, query and header scope only; cookie scope is straightforward to add.
- **Effects not visible as one HTTP write:** asynchronous jobs, emails, webhooks, websockets, multi-request transactions.
- **Multi-container apps** (app + database). v0 runs one image; compose-style stacks are a next step.
- **Fabricated success is now generic.** It returns the contract's body. That differs from BreakingWeb's own fabricated variant (used by the demo), so cross-protocol comparisons need care.
- **Trust in the verifier endpoint.** The developer supplies it. The proxy-ledger cross-check catches disagreement, not a verifier that lies consistently.

## Next steps (each needs your approval where noted)
1. **(Approval: about $0.02–0.05)** One LLM smoke episode on the expense app through the generic proxy (the existing worker, the app's UI), to test the browser path.
2. Cookie-scope support plus a minimal contract linter CLI (`afd contract check`), $0.
3. A "Test Your Workflow" page that documents the contract and runs the linter. Executing developer images publicly needs its own security review: untrusted images, resource quotas, abuse. Until then, developer workflows would run only in operator-approved sessions.
