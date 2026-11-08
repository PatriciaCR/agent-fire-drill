# Submission notes (Vultr Agent Rush)

## One-line pitch
Agent Fire Drill tests what an AI agent or automation **actually does to your backend** when a consequential write
fails ambiguously, by injecting the failure on its real request and checking the database, not the agent's claim.

## Claims, each with its evidence
| Claim | Evidence | Where |
|---|---|---|
| A live demo on Vultr: a browser-use agent drilled on a synthetic refill workflow, with Vultr Serverless Inference behind a key-holding gateway and rootless, firewalled execution | [VULTR_DEPLOYMENT_EVIDENCE.md §1](VULTR_DEPLOYMENT_EVIDENCE.md) | Vultr |
| The demo's full qualification is sealed, and its harness **correctly** went invalid; the root cause is an agent grounding failure (intended Lisinopril, clicked Escitalopram) | [R027_ROOT_CAUSE.md](R027_ROOT_CAUSE.md) | Vultr bundle |
| The generalized engine passed a rootless deployment smoke **on Vultr**: isolation, enforced resource limits, lost ack on an agent's real write with exactly one oracle-verified effect, evidence integrity, clean teardown | [VULTR_DEPLOYMENT_EVIDENCE.md §2](VULTR_DEPLOYMENT_EVIDENCE.md) (bundle `vmsmoke-20261108T055533Z-a1260f`, digest `f3b078ab…`) | Vultr (synthetic fixtures) |
| **An unmodified third-party CLI (Gitea's `tea` 0.16.0) ran as the agent under test**, from a hash-bound read-only workspace; AFD intercepted its real issue-creation request | [M6C_BYOA_WORKSPACE_TEA.md](M6C_BYOA_WORKSPACE_TEA.md) (bundle `generic-20261108T064712Z-f35c41`) | local, and on Vultr (16-run qualification, operator-run, archived off-VM) |
| **An autonomous LLM agent (browser-use, `glm-5.3-flash`) through the same generalized engine on Vultr:** with no retry instruction, it chose to resubmit after a lost-ack 503 in **both** episodes, creating a **duplicate issue** each time while claiming success. Effect safety: FAIL (2/2). Outcome integrity: no contract-v1 false success (0/2; an issue exists). Completion: 0/2. Spend $0.020898 of a $0.10 cap | [P3_LLM_GENERIC_PATH.md](P3_LLM_GENERIC_PATH.md) (bundle `llm-20261108T101437Z-a76ef3`, digest `bd12e1b7…`; independently audited) | Vultr (n = 2) |
| The LLM path's isolation is enforced, not assumed: from inside the actual worker and browser containers, only the gateway + browser control port (worker) and only the proxy (browser) are reachable; the key never enters a container; tokens are capped, model-scoped and revoked; a deliberately mis-wired worker is refused before it can start | [P3_LLM_GENERIC_PATH.md](P3_LLM_GENERIC_PATH.md) | Vultr (rootless) + local negative control |
| `tea` reports **failure** after a lost acknowledgement or a delayed commit while the issue **was** created (independently verified under-claims), and reports **success** on a fabricated response while nothing was created | [M6C_VALIDATION_PASS.md §2](M6C_VALIDATION_PASS.md) (bundle `generic-20261108T065542Z-564f31`) | local |
| A **separately labelled** blind-retry wrapper around `tea` (the retry belongs to the wrapper) creates **duplicate issues** under lost ack and delayed commit, caught in 6/6 runs | [M6C_VALIDATION_PASS.md §3](M6C_VALIDATION_PASS.md) (bundle `generic-20261108T065653Z-dfdd8f`) | local |
| **The redesigned public site is deployed on Vultr** at commit `fa6974e`, after staging on the VM: 51/51 mandatory containment checks pass on that commit; the public qualification is shown as harness invalid / NOT ASSESSED | [VULTR_DEPLOYMENT_EVIDENCE.md §4](VULTR_DEPLOYMENT_EVIDENCE.md), [/containment](https://45-63-90-26.sslip.io/containment) | Vultr |
| Contract-aware verdicts: an app that blocks duplicates itself (WooCommerce) is scored correctly, with "application-protected" labels | [M4_CONTRACT_AWARE_VERDICTS.md §8](M4_CONTRACT_AWARE_VERDICTS.md) | local |
| A verify-then-retry policy that checks too early duplicates when the backend commits late | [M5_PRECOMMIT_AND_LATE_EFFECTS.md](M5_PRECOMMIT_AND_LATE_EFFECTS.md) | local (synthetic fixture) |

Every qualification bundle verifies with `afd contract verify-bundle` (or `sha256sum -c MANIFEST.sha256`). `afd report`
renders qualification and LLM-episode bundles after re-verifying them; an LLM-episode bundle's classifications and
spend are recomputed from its sealed evidence.

## What we do NOT claim
- Broad bring-your-own-agent support. The third-party evidence is **one** CLI (`tea`) on **one** application (Gitea).
- Support for arbitrary developer-supplied LLM containers. An LLM agent runs only as AFD's own browser-use worker,
  behind the inference gateway.
- Broad model compatibility, or any rate. The LLM result is **two** episodes, on **one** application, with **one**
  fault and **one** model.
- That the LLM agents' claims were "false successes" in the contract-v1 sense: an issue was created. The finding is
  a **duplicate** (effect safety), plus a success claim that did not mention it.
- Exactly-once behaviour of any agent. "0 duplicates in N runs" is an observation with an upper bound.
- Real-application late commits. Those were exercised only in a synthetic fixture.

## Remaining onboarding friction (stated in the README)
- The developer must declare the route their tool really writes through.
- They choose a pinned base image with its runtime dependencies (`tea` needs `git`).
- Binaries and approvals are per architecture.
- One wiring line is needed (base URL and target).
- Fake responses must be realistic (an incomplete fake body made `tea` crash: the first, inconclusive result).

## Demo video outline (about 3 minutes)
1. **The problem (15 s):** an agent retries after a lost acknowledgement, and two orders are placed.
2. **Live on Vultr (45 s):** run a drill; open the recorded qualification, then the r027 evidence and why the harness
   went invalid.
3. **Containment (15 s):** the containment page.
4. **Bring your own tool (60 s):**
   - `afd agent check` (shows the workspace hash);
   - `afd qualify --agent-spec` with `tea`;
   - the result table: the under-claims, the false success, and the duplicates from the retry wrapper;
   - `afd contract verify-bundle`.
5. **Honest limits (15 s)** and the architecture (15 s).
