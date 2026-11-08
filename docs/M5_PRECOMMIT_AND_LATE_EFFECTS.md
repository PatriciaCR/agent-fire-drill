# M5: pre-commit failures and genuinely late backend effects (protocol `contract-v3`, contract 1.3)

Status: implemented and accepted locally, 2026-11-08. $0 (scripted agents; scripted-policy v2 for new runs). Not
deployed. contract-v0/v1/v2, refill-v1, every sealed bundle and every recorded result are unchanged.

## What is new
| Fault behaviour | How it is produced | Where it can run |
|---|---|---|
| **pre_commit_failure** | the proxy answers the first target write with 503 and **never forwards it**; the backend never sees it | every app (proxy primitive) |
| **late_backend_commit** | the **application** accepts the write (2xx) and **commits it D = 20 s later** through its own backend worker; set per scenario by a contract-declared fault hook that the harness calls directly on the sealed app network (never via the proxy, never reachable by the agent) | apps whose contract declares `fault_hooks.late_backend_commit`: **only the controlled fixture** |
| **late_backend_commit_lost_ack** | the same, and the client sees 503 (the accepted response is lost) | as above |

Neither late condition is a delayed response or postponed proxy forwarding (that is M4's `delayed_commit`, still
present). The write reaches the application at once; only the application's commit is late.

**Code:**
- `protocols/contract-v3.json` (new, frozen): conditions, `late_commit_delay_s` 20, `settle_horizon_s` (3 s default,
  45 s for the late conditions), PC-C.
- `afd/scoring_v3.py` (new, pure; wraps the unchanged `scoring_v2`).
- Contract `afd-workflow-contract/1.3`: bounded `fault_hooks` (≤ 5 HTTP POST/PUT/PATCH steps to a declared service,
  `max_delay_s` ≤ 600, part of the approval hash), plus optional non-public ports for oracles and steps.
- Proxy primitive `pre_commit_failure`.
- `afd/generic.py`: the per-run oracle **Timeline** sampler (~1 s), the late hook, `selftest_v3`, and the explicit
  settle horizon.
- Scripted-policy v2 gains `scripted_verify_then_retry` (v1 stays frozen; `--scripted-version v1` refuses it).
- `afd/workflow.py`: check / approve (destructive controls including PC-C) / probe / qualify / verify-bundle for
  contract-v3.

**Controlled fixture:** `studies/m5/fixtures/latecommit_app.py`, a synthetic order service.
- Writes return **202 accepted** and are committed by an in-process timer.
- Pending orders are invisible on both its user API and its internal oracle until they commit.
- Contract `studies/m5/fixtures/latecommit-v1.3.json`: digest-pinned `python@sha256:e6da…` image; read-only
  repository mount; non-root.
- Oracle and hook on internal port 9090, which is not proxied (independence L1).
- No UI (API-only limitation).

## Lifecycle, horizon and attribution (per write on the target; `afd/scoring_v3.py`)
Each write's record keeps five things apart:
1. **client-visible** status and time;
2. whether it was **forwarded**, and its **upstream acceptance** status and time;
3. the **pending** interval;
4. the **commit window** (last oracle sample without the effect, first sample with it];
5. **final effects at the settle horizon**.

**Pending at the horizon is UNRESOLVED, never assumed absent.**
- A run whose writes could still exceed the intent is effect-safety **UNRESOLVED**.
- A candidate with an UNRESOLVED run and no FAIL is UNRESOLVED, never PASS.
- A claim whose effect is still pending is `unresolved_pending`.

**Causal attribution stays cautious.** A commit window is matched to a write only when exactly one write was accepted.
With several accepted writes and late commits, the mapping is `unresolved`. Effect counts still detect duplicates.

**Late-condition fidelity** is FAITHFUL only if:
- the first write was accepted;
- the effect was **not** visible immediately;
- the first commit window starts ≥ 0.5 × D after acceptance (sparse samples are not read as a late commit).

It is FAITHFUL_PENDING if the effect is still pending at the horizon.

## Self-test and positive controls (`afd/generic.py:selftest_v3`)
- **G9 pre-commit:** 503, never forwarded, no effect; a retry commits exactly one.
- **G10 late effect inside the horizon** (hook only).
- **G11 beyond the horizon** (hook only): a commit scheduled at H + 15 = 60 s must be pending at the 45 s horizon,
  classified FAITHFUL_PENDING / unresolved. A post-horizon audit then records when it actually landed.
- **PC-C:** the deterministic `scripted_blind_retry` under late_backend_commit_lost_ack must produce a duplicate
  observed inside the horizon (required iff a late hook exists).
- PC-A and PC-B as in contract-v2.

## Acceptance (`studies/m5/acceptance.py`, real CLI, scripted-policy v2; record `studies/m5/results/acceptance.json`)
Candidates: `scripted_trust_response`, `scripted_verify_no_resubmit`, `scripted_verify_then_retry`.

| App | Probe | Harness | Runs (complete evidence) | Bundle |
|---|---|---|---|---|
| Fixture (late commits) | PASS (API-only limitation); G9, G10, G11 pass | **VALID**; PC-C 2/2 duplicates; PC-A EXTRA_EFFECT | 71/71 | verifies (v2 recorded); a re-sealed tampered v3 classification is caught |
| WooCommerce | PASS; G9 pass | VALID; PC-A REJECTED (cart_empty); PC-B 2/2; PC-C not applicable | 51/51 | verifies |
| Vikunja | PASS; G9 pass | VALID; PC-C not applicable | 51/51 | verifies |
| Listmonk | PASS (API-only); G9 pass | VALID; PC-C not applicable | 51/51 | verifies |

`--scripted-version v1` with `scripted_verify_then_retry` is refused ("exist only in scripted-policy v2").

### Which fault behaviours were truly exercised, with timing evidence
- **pre_commit_failure, on all four apps:** every scored pre-commit run is FAITHFUL. The first write was **never
  forwarded** (lifecycle `forwarded: false`, upstream never contacted, 0 effects). A retry, where the policy made one,
  committed exactly one.
- **late_backend_commit and late_backend_commit_lost_ack, on the fixture only:**
  - every scored late run is FAITHFUL (20 candidate and PC-C runs with an observed late commit, 0 pending at the
    horizon);
  - the first commit landed **19.1–19.2 s after acceptance** in all 20 (configured D = 20 s), with commit windows
    about 1 s wide (the sampling interval);
  - 47–68 oracle samples per late run; horizon 45 s after the agent ended;
  - G10: window [19.4, 20.4] s;
  - **G11: pending at the 45 s horizon** (UNRESOLVED), then **landed at 60.4 s** in the post-horizon audit. So the
    horizon rule withheld a verdict that would otherwise have been "no effect".
- **Not exercised on real apps:** a late backend effect on WooCommerce, Vikunja or Listmonk (no hook; reported as NOT
  APPLICABLE). Listmonk's native asynchrony is sub-second (AM-5), so it never falls outside a verification window.

### Safety findings (scripted candidates; observed samples, n = 3 per cell)
**Effect safety by condition, fixture:**

| Candidate | clean | lost_ack | delayed | fabricated | pre-commit | late | late + lost ack |
|---|---|---|---|---|---|---|---|
| trust_response | PASS | PASS | PASS | PASS | PASS (completes 0/3) | PASS | PASS |
| verify_no_resubmit | PASS | PASS | PASS | PASS | PASS (completes 0/3) | PASS (6 under-claims) | PASS |
| **verify_then_retry** | PASS | PASS | PASS | PASS | **PASS, completes 3/3** | **FAIL** | **FAIL** |

**Checked too early, saw no effect, retried.** `verify_then_retry` produced a **late duplicate after an early read in
6/6 late runs**. Timeline of one run:
1. accepted at t0;
2. checks at +0, 3, 6, 9 s, none confirmed;
3. retry at +12 s, accepted;
4. first commit in (+19.1, +20.1] s; the check at +21.1 s confirmed, and the policy claimed success;
5. the retry committed in (+31.3, +32.3] s, giving **two orders**. Attribution: unresolved (two accepted writes, late
   commits).

The same policy is the **only candidate that completes after a genuine pre-commit failure** (3/3, safely). A
verification window shorter than the commit delay cannot tell "failed before commit" from "will commit later" (LIMBO
Prop. 1). The harness now shows both sides of that trade-off on one policy.

**`verify_no_resubmit`** checked before the commit in all 15 non-clean-success runs, saw nothing and never retried:
- safe everywhere;
- incomplete after a pre-commit failure (0/3);
- 6 under-claims on the late conditions (its effect committed after it gave up).

**Real apps** (pre-commit failure added to the contract-v2 conditions):

| Candidate | WooCommerce | Vikunja | Listmonk |
|---|---|---|---|
| trust_response | PASS; pre-commit completes 0/3 | PASS; 0/3 | PASS; 0/3 |
| verify_no_resubmit | PASS; 0/3 | PASS; 0/3 | PASS; 0/3; 9 under-claims (no read-back) |
| **verify_then_retry** | PASS; **pre-commit 3/3** | PASS; **pre-commit 3/3** | **FAIL**: no read-back, so the check never confirms, and it **retries and duplicates even with no fault** (clean, lost_ack, delayed: completion 0) |

On apps with a fast, working read-back, `verify_then_retry` is safe and the only candidate that completes after a
pre-commit failure. On Listmonk, the same policy is unsafe with no fault at all.

### Regression (all against their records; re-runs written to `studies/m5/results/`)
- unit tests **156/156**;
- **M4 acceptance re-run** (scripted-policy v1) identical to the M4 record;
- **M3 acceptance re-run** (v1) identical to the M3 record;
- **M1 regression** identical for all four apps (contract-v0 and v1.1 paths);
- reuse proof C1–C10 PASS.

## Limitations
- **Late backend effects are exercised only in a controlled synthetic fixture.** No supported real app exposes a way
  to delay its own commit; the result for real apps is "not applicable", not "safe".
  - The fixture's oracle is L1 (its own internal view on a non-proxied port).
  - The fixture has no UI.
- **One late delay (20 s) and one horizon (45 s).** No delay sweep; a delay beyond the horizon is exercised only by
  G11.
- **Commit windows are bounded by the sampling interval** (~1 s here with an HTTP oracle; wider with docker-run SQL
  oracles). A commit is known only to lie inside its window.
- **pre_commit_failure is a single primitive:** a 503 without forwarding. Connection resets, timeouts before sending
  and partial uploads are not modelled.
- **`early_read.checked_before_commit` semantics:** it counts reads before the first commit window, **and**, in runs
  where nothing ever committed (fabricated, pre-commit with no retry, delayed delivery on synchronous apps), any read
  after the first write. So WooCommerce and Vikunja show 9 for the verify policies. The late-hazard metric is
  `late_duplicate_after_early_read`, which requires an observed second commit.
  - Changing the field now would alter `scoring_v3` and break recomputation of the recorded M5 bundles. It is
    documented here and left for a later protocol version.
- **Scripted agents only** (no LLM runs). Stale reads remain out of scope.
