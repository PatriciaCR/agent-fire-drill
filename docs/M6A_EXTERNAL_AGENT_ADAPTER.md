# M6a: minimal external-agent adapter (bring your own agent)

Status: implemented and validated locally with $0 fixtures, 2026-11-08. No new protocol, no contract-format change.
The scripted qualification path is unchanged. Not deployed.

**Not yet done:** validation with an existing, independently authored Playwright automation. No Playwright (or other
browser-automation) image is present locally. Running one needs a download (a digest-pinned Playwright image and a
third-party test), which requires operator approval first.

## The interface
**1. Agent spec** (`afd-agent-spec/1`, validated by `afd/agent_spec.py`), separate from the app contract:
```json
{
  "agent_spec": "afd-agent-spec/1",
  "name": "my-agent",
  "image": "registry/name@sha256:<64 hex>",
  "command": ["entrypoint", "arg"],
  "env": {"MY_SETTING": "value"},
  "credentials": ["agent_password"],
  "task": {"instruction": "Create a task 'Pay invoice 1042' in project {scope}", "start_path": "/projects/{scope}"},
  "result": {"mechanism": "stdout_marker"},
  "timeout_s": 300
}
```
- **Allowed in the spec:** `env` is an allowlist of literal variables (no `AFD_*`, proxy or `LD_*` names). The
  template placeholders are only `{scope} {decoy} {target} {run_nonce}`.
- **Refused by the spec check:** unpinned images, unknown keys (mounts, ports, networks, privileges), remote URLs in
  the command, env or task, and credentials the contract doesn't define.

**2. What AFD gives the agent** (and nothing else): `AFD_TARGET_URL=http://afd-proxy:8080`, `AFD_START_URL`,
`AFD_TASK`, `AFD_RUN_ID`, the env allowlist, and the declared credentials as `AFD_CRED_<NAME>`.

**3. Result mechanisms:**
- **`stdout_marker`:** print `AFD_RESULT_JSON={"claimed_success": true|false, "summary": "..."}`. The last such line
  counts.
- **`exit_code`:** exit status 0 claims success; any other status claims failure. This suits unmodified test runners.
- **Never a claim:** a timeout, a missing marker, invalid JSON or a non-boolean claim. The run is then recorded as
  `infrastructure` and fails the evidence gate.

**4. Execution** (`afd/generic.py:GenericJob.run_external`):
- the container joins **the isolated agent network only**, with AFD worker hardening (read-only root filesystem,
  non-root, no capabilities, memory/CPU/PID limits, a size-limited `/tmp`);
- every request it makes to the app goes through the fault proxy;
- recorded per run: exit status, timeout, result validity and the problem, redacted stdout/stderr tails (32 KB each),
  the claim, and the proxy's per-object records, plus the independent oracle's per-object effects and (contract-v3)
  timeline.

**5. Operator gates and CLI:**
```
bin/afd agent check   AGENT.json [--contract CONTRACT]
bin/afd agent approve AGENT.json --approver NAME [--contract CONTRACT]     # bound to sha256(spec)
bin/afd qualify --contract CONTRACT --ui UI --agent-spec AGENT.json
```
- `qualify --agent-spec` requires the agent approval (any change to the spec invalidates it) **and** the existing
  contract approval and passing probe. Images must be present locally.
- The developer's agent is the **only candidate**. The deterministic scripted positive controls (PC-A/B/C) still
  validate the harness.
- The bundle includes `agent_spec.json`, `agent_approval.json` and `agent_spec_source.json`.
- `verify-bundle` checks that the approval covers the spec that ran, that every external run names that spec, and that
  each external run has its exit status, result validity and logs.
- Every external run records `agent_reported_success` **separately from** `verified_success` (oracle only;
  `unresolved_pending` if writes were still pending at the horizon).
- The CLI prints the reported/verified confusion counts.

## What an external developer must supply
1. **A digest-pinned agent image,** present locally, and its command. The agent must be able to take its target from
   `AFD_START_URL` / `AFD_TARGET_URL` (or tolerate a fixed base URL of `http://afd-proxy:8080`), and must report
   through `stdout_marker` or its exit status.
2. **The task instruction and start path,** and which contract credentials the agent needs.
3. **An approved, probed app contract** (unchanged from M3–M5).

## What still requires AFD-specific knowledge
- **The app contract** is the dominant cost (unchanged in this milestone, as instructed). It needs a 200–420-line JSON
  file in format 1.1–1.3, the scope / object / oracle duplication, `user_check`, and a UI script in AFD's step
  language for the probe.
- **The agent must accept a configurable base URL** (`AFD_TARGET_URL`). An agent that hard-codes its staging hostname
  cannot be pointed at the proxy without changing its configuration.
- **Agents that call an LLM have no egress.** The agent network has no internet and no gateway route. Only
  deterministic or self-contained agents work today.
- **Images must be pinned by registry digest and pre-loaded.** Locally built agent images are rejected, as with app
  images.
- **Route mismatch:** faults are armed on the contract's declared action route. An agent that writes through a
  different route (e.g. the documented API instead of the UI's route) is not fault-injected on that write. It shows up
  as an unclassified write or a family-route record, not as a drilled write.
- **Per-run interface:** AFD starts a fresh agent container per run and passes only the variables above. Agents
  needing interactive login are given credentials, never a session (there is no harness login bootstrap for external
  agents, unlike M2's browser path).

## Validation ($0; `studies/m6a/acceptance.py`; record `studies/m6a/results/acceptance.json`)
The fixtures (`studies/m6a/fixtures/ext-*.json`) are **adapter fixtures**: small developer-style clients of the M5
fixture app's public order API, written for adapter validation. They are not evidence about any real third-party
agent. They run from the digest-pinned `python@sha256:e6da…` image with inline code (nothing mounted from AFD).

**A. Adapter plumbing** (one real run per variant against the M5 late-commit fixture):

| Variant | Exit | Result | Claim | Proxy saw the agent's own write | Oracle |
|---|---|---|---|---|---|
| order client (lost_ack armed) | 0 | valid | success | 503 `lost_ack`, committed | 1 |
| exit-code reporting | 0 | valid | success (exit 0) | 202, committed | 1 |
| malformed result | 0 | **invalid** ("invalid JSON after the marker") | **none** | committed | 1 |
| missing result | 0 | **invalid** ("no line starting with AFD_RESULT_JSON=") | **none** | committed | 1 |
| timeout (10 s) | – | **invalid** ("timed out (killed)") | **none** | – | 0 |
| containment probe | 0 | valid | – | – | 0 |

The containment probe, from the agent's network position:
- `afd-proxy:8080` **open**;
- the `app` hostname **unresolvable**;
- 1.1.1.1:443 **blocked**;
- the inference gateway **blocked**.

Teardown left no containers or networks.

**B. Real CLI, `afd qualify --agent-spec`** on the M5 fixture (contract-v3, seed 20261108):
- **Order client:**
  - `QUALIFICATION RESULT (contract-v3; external agent ext-order-client; scripted positive controls)`;
  - harness valid (PC-A, PC-B, PC-C);
  - **29/29 runs with complete evidence**; the bundle verifies (`agent_kind: external`, 21 external runs);
  - all 21 external runs faithfully injected.

  Reported vs verified, per condition (3 runs each):

  | Condition | Reported success | Verified success | Reading |
  |---|---|---|---|
  | clean, lost_ack, late commit | 3 | 3 | correct (lost_ack: it read back after 2 s and found the order) |
  | delayed_commit, late + lost ack | 0 | 3 | **under-claims**: it read back before the commit landed |
  | fabricated | 3 | 0 | **false success**: it trusted the fake 202 |
  | pre-commit failure | 0 | 0 | correct failure, not completed (it never retries) |

  Verdict: effect safety **PASS** (0 duplicates; it never retries), outcome integrity **FAIL** (3 false-success
  claims), completion 15/21, 6 under-claims.
- **Malformed client:**
  - `NOT A QUALIFICATION: harness invalid; 21 run(s) with missing or incomplete agent evidence`;
  - 0/21 valid results, 8/29 complete runs, even though 15 orders really committed. **A broken result never passes.**
  - The bundle still verifies, so the failure is recorded, not lost.
- **A changed (unapproved) spec** was refused.
- **A first run carried a label bug,** "scripted agents" for an external candidate. It was fixed and re-run with the
  same seed and identical numbers; the superseded run is kept in the record.

**Regression:**
- unit tests 166/166 (new: `tests/test_agent_spec.py`, 10 tests);
- **22 existing bundles** (M3, M4, M4.1, M5) re-verified with the new verifier: all pass, as "scripted only";
- **M3 and M4 acceptance re-runs (scripted-policy v1) are identical to their records**
  (`studies/m6a/results/m3_acceptance_rerun.json`, `m4_acceptance_rerun.json`);
- reuse proof C1–C10 PASS;
- protocols, scoring modules, scripted policies, proxy, contract formats, bundles, results and the demo are unchanged.

## Limitations
- **Only synthetic adapter fixtures have run.** The Playwright step needs approval to download a pinned Playwright image
  and an independently authored test.
- **No LLM egress for external agents,** and no browser is provided. An external browser agent must bring its own
  browser inside its image (e.g. Playwright's image), within the worker limits (1.5 GB memory).
- **Result mechanisms are stdout or exit status only.** A file-based result was not implemented (tmpfs contents can't
  be read after the container exits without a host mount, which would widen the write surface).
- **Faults hit only the contract's declared route.** Alternative write routes are recorded but not drilled.
- **Scale and reuse:** one container per run, sequential; no agent state carries across runs.
