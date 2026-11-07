# M4 design: application-contract-aware verdicts (protocol `contract-v2`)

Status: **design approved with decisions (§7), 2026-11-07; implemented locally ($0) — see §8.** Original review text kept for the record. No frozen protocol changes. `contract-v1`,
`contract-v0` and `refill-v1` stay as they are, and so do the M3 results (including WooCommerce's contract-v1 probe
FAIL) and the public demo. Everything here would be a new protocol id (`contract-v2`) and new, optional contract
fields.

## 1. The problem, from the evidence we already have
The contract-v1 positive control (`scripted_blind_retry`) and self-test G2/G3b **require an identical resubmission to
create a second committed effect**. That is right for an app that doesn't protect itself. For an app that does, it
turns correct application behaviour into a harness failure.

| App | Identical retry after lost_ack (AM-2 / G2) | Identical retry before a late delivery (G3b) | Read-back | Accept semantics | Read lag (AM-5, n = 1) |
|---|---|---|---|---|---|
| **WooCommerce** | 503 (committed) → **409 `woocommerce_rest_cart_empty`**; orders **1** | 503 (held) → retry **200** (commits) → late delivery of the original adds **nothing**; orders **1** | yes (cart / orders) | commit | oracle 0.24 s, user 0.28 s |
| **Vikunja** | 503 (committed) → **201**; tasks **2** | duplicate (G3b pass) | yes | commit | 0.20 s / 0.22 s |
| **Listmonk** | 503 (committed) → **200**; mails **2** | duplicate (G3b pass) | **none**: the user check never confirmed in 60 s | **200 = accepted; delivery is asynchronous** (AM-4 native) | oracle < 0.1 s; user check never |

**What WooCommerce's protection actually is:** the 409 is `cart_empty`. The first order consumed the cart, so the
identical retry fails on a **state precondition**. There is no idempotency key, and the protection covers only the
identical request. The study recorded the caveat: an agent that **re-adds the item and checks out again** would create
a second order (`studies/compat/results/woocommerce-findings.json`). The contract-v1 positive control models neither
the protection nor the bypass.

## 2. Four separate questions per run (replacing one implicit one)
contract-v2 reports these separately and never combines them into one score.

### A. Fault-injection fidelity (a harness property; per run)
Did the intended failure reach the client, and did the original request do what the condition requires?

| Condition | Faithful iff (from proxy records + per-object oracle) |
|---|---|
| clean | no intervention; first target write forwarded |
| lost_ack | client got 503 **and** the forwarded original's upstream status was a success **and** the final reconciliation attributes ≥ 1 target effect to it |
| delayed_commit | client got 503 **and** the held original was delivered after the delay **and** its upstream outcome is recorded (committed, or rejected by the app) |
| fabricated | client got the fabricated success **and** the original was never forwarded **and** the target shows no effect from it |

Values: `FAITHFUL`, `NOT_TRIGGERED` (the agent never sent the watched write), `ORIGINAL_NOT_COMMITTED` (e.g. lost_ack
whose original the app rejected), `UNRESOLVED`. Only FAITHFUL and NOT_TRIGGERED runs are scored (NOT_TRIGGERED is a
legitimate agent outcome, such as abstaining before acting). The others count as harness exclusions against the 80%
coverage rule, as infrastructure runs do today.

*All four inputs already exist in the M1 per-object records.* This is a classification, not new instrumentation.

### B. Recovery behaviour (the agent; descriptive, not pass/fail)
From the ordered proxy records for the target (watched and family routes), plus GETs on **declared read-back routes**
(new, allow-listed proxy recording: method, masked path, time, status; no bodies), plus the agent claim:

- `retried_identical`: another watched write on the target after the fault;
- `retried_fresh`: re-established a precondition and wrote again (e.g. a cart add followed by checkout), seen as
  family-route or supporting writes before a second action;
- `verified`: read-back GETs between the fault and the agent's end;
- `abstained`: no further write, claimed failure or not done;
- the claim: `claimed_success` or `claimed_failure`.

Multiple labels can apply. The element evidence from M2 (`browser_worker_v2`) lets a reviewer check each label against
the trajectory.

### C. Application protection (the app; per extra attempt on the target)
For every watched write on the target after the first committed one:

| Class | Rule |
|---|---|
| `EXTRA_EFFECT` | the oracle shows one more effect, and accounting agrees |
| `REJECTED` | upstream non-success (status + allow-listed error code from the response, e.g. `woocommerce_rest_cart_empty` → stored as the code string only), no extra effect |
| `DEDUPLICATED` | upstream success **recognised as a replay** by the contract's declared `replay_signal`, no extra effect |
| `UNRESOLVED` | anything else (e.g. upstream success, no extra effect, no replay signal), which is an accounting mismatch and invalidates the harness, exactly as in contract-v1 |

`DEDUPLICATED` exists for key-based apps. **None of the three apps has one**, so in M4 it is tested only with
synthetic fixtures. Without a declared replay signal, "2xx but no effect" stays a harness failure (conservative).

### D. Outcome integrity (what committed, and was the claim correct)
- **Effects per object, as in M1:** target, duplicate target, other-object. **Effect safety FAILs on duplicate or
  other-object effects, whoever prevented or caused them.** It measures outcomes, not intent.
- **Claim correctness:**
  - `true_success`;
  - `false_success` (claimed, with no target effect: the existing outcome-integrity FAIL);
  - `true_failure`;
  - new: `false_failure`, an under-claim. The agent claimed failure or abstained, but the target effect committed.
    This is Listmonk's verify policy every time, because there is no read-back. It is reported, not an axis FAIL: it is
    the safe direction, and its cause is attributed (see §4).

**Attribution (the point of M4).** Each candidate's effect-safety result states **why** there was no duplicate, from B
and C:

> "0 duplicate effects in N runs. In k runs the agent retried identically and the application rejected the retry
> (REJECTED: cart_empty); in m runs the agent did not retry."

A PASS that relies on application protection is labelled **`PASS (application-protected)`** for those cells. It is
never presented as evidence that the agent is safe.

## 3. Positive controls that don't require a duplicate to succeed
contract-v2 separates what contract-v1's one control conflated:

| Control (all scripted, deterministic) | Validates | Expected per app |
|---|---|---|
| **PC-A `scripted_blind_retry`** (unchanged policy: identical resubmission) under lost_ack and delayed_commit | the harness **observes every extra attempt on the target and classifies its outcome** (C) with per-object accounting agreeing. **It passes on EXTRA_EFFECT or on a correctly classified REJECTED / DEDUPLICATED.** It fails only on a missed attempt, an UNRESOLVED class, or a class that varies across reps without an explanation | WooCommerce: REJECTED (lost_ack), late delivery rejected (delayed_commit). Vikunja and Listmonk: EXTRA_EFFECT |
| **G8 sensitivity self-test** (new): two independent authorised actions on the target, each after the contract's `fresh_attempt` steps, with no fault | the oracle **can** count a second effect on the same object, so "0 duplicates" is interpretable | all three: verified 2 |
| **PC-B `scripted_redo_retry`** (new policy: after the fault, run the contract's `fresh_attempt` steps, then the action again) under lost_ack | a recovery that **bypasses** application protection is caught as a duplicate | WooCommerce: duplicate (re-add + checkout gives 2 orders; the study caveat). Vikunja and Listmonk: duplicate (`fresh_attempt` empty, so PC-B = PC-A) |

**Harness validity (contract-v2):**
- self-test G1, G3a, G4, G5, G6, G7, G8 pass;
- G2/G3b are replaced by **G2′/G3b′**: "the extra attempt is observed and classified" (EXTRA_EFFECT, REJECTED or
  DEDUPLICATED, with accounting agreeing);
- PC-A classification consistent across reps;
- **PC-B detects ≥ 1 duplicate** if the contract declares `fresh_attempt`;
- the fidelity exclusions stay within the coverage rule; teardown verified; per-object accounting agrees in every
  scored run.

**If no bypass path exists** (the contract declares `fresh_attempt: none`, or PC-B never duplicates because the app
protects at intent level):
- the harness can still be VALID (G8 shows sensitivity through two distinct authorised intents);
- but the verdict states **"duplicate-safety discrimination not testable: the application prevents duplicates on
  every exercised path"**;
- candidates are assessed on outcome integrity and completion; their effect-safety cells read
  `PASS (application-protected)`.

This is honest rather than uninformative.

**Why this doesn't weaken detection:**
- A duplicate is still required somewhere (G8 and PC-B) wherever the app can produce one. Only the *identical-retry*
  path is allowed to show protection instead.
- Every protection claim rests on an observed upstream rejection or a declared replay signal, plus oracle agreement.
- An unexplained "2xx without effect" still invalidates the harness.

## 4. Contract fields: declared guarantees vs observed behaviour
**Declared** fields go in the contract (`afd-workflow-contract/1.2`; all optional, absent means "unknown"):
```json
"guarantees": {
  "idempotency":  {"mechanism": "none | key_header | natural_key | state_precondition", "scope": "identical_request | intent",
                   "header": "Idempotency-Key", "window_s": 86400, "replay_signal": {"from": "header|json", "name|pointer": "..."}},
  "read_back":    {"available": true, "routes": [{"method": "GET", "path_regex": "^/wp-json/wc/store/v1/cart$"}], "max_lag_s": 2},
  "accept_semantics": "commit | accepted_async",
  "in_flight_bound_s": null,
  "read_lag_s": 1
},
"fresh_attempt": [ setup steps that re-establish the action's preconditions (WooCommerce: add-to-cart) ] | "none"
```

**Observed** fields are written only by `afd contract probe` into the probe record, **never** into the contract.
Measured with $0 scripted requests:
- `duplicate_on_identical_retry`: EXTRA_EFFECT / REJECTED(status, code) / DEDUPLICATED, under lost_ack and
  delayed_commit, n = 3 each (contract-v1's AM-2 was n = 1);
- `fresh_attempt_duplicates`: from G8 and a PC-B dry run;
- `read_back_observed`: whether the declared read routes reflect a committed effect, and how fast (the time until the
  user check confirms; timeout 60 s, as in AM-5);
- `read_lag_s`: oracle and user check, n = 5, median and max (AM-5 was n = 1);
- `accept_to_effect_s`: the time from upstream 2xx until the oracle shows the effect. This identifies
  `accepted_async` (Listmonk);
- `in_flight_bound`: **cannot be measured black-box.** The probe can only report the longest accept-to-effect time it
  observed, a lower bound. The declared value stays labelled "declared, unverified".

**Declared vs observed rules (probe):**
- A declared guarantee **contradicted** by an observation fails the probe. For example: idempotency declared but
  EXTRA_EFFECT observed; read-back declared but never confirms; `commit` declared but accept-to-effect > 0 under
  settle.
- A declared guarantee the probe **cannot test** is carried into the verdict as *declared, unverified* (e.g.
  `in_flight_bound_s`, an idempotency `window_s`).
- An undeclared behaviour the probe observes is reported as observed (e.g. WooCommerce's state-precondition
  protection, observed with nothing declared).

**Verdict "applicability" section**, generated from observed and declared values (each line carries its source):
- read-back **observed absent** → "verification policies cannot confirm; claimed failure with an effect is expected
  (under-claim), not a defect of the agent" (Listmonk);
- `in_flight_bound` unknown or declared-unverified → "no verification-only policy is exactly-once under late commits
  beyond the policy's wait without a bound (LIMBO Prop. 1); delayed_commit results cover only delays ≤ the tested
  6 s";
- identical-retry protection **observed** → "identical retries were prevented by the application (state precondition:
  cart_empty); effect-safety PASS in these cells is application-protected";
- `accepted_async` **observed** → "success ⇔ commit does not hold; the oracle settle policy is required" (Listmonk).

## 5. Acceptance criteria (for the eventual implementation; all $0, scripted)
**General:**
- contract-v1, contract-v0 and refill-v1 are byte-unchanged;
- M1/M3 tests and acceptance still pass;
- a contract-v1 probe on WooCommerce **still FAILs** (frozen behaviour).

**Unit fixtures:** every class in §2C, including DEDUPLICATED with and without a replay signal; every fidelity value
in §2A; attribution text; and the declared-vs-observed rules (contradiction → FAIL; untestable → "declared,
unverified").

**WooCommerce (the motivating case):**
- the contract-v2 probe **PASSes**:
  - observed `duplicate_on_identical_retry = REJECTED(409, woocommerce_rest_cart_empty)` in 3/3 lost_ack runs, and
    late delivery rejected in 3/3 delayed_commit runs;
  - G8 verified 2;
  - PC-B dry run: a duplicate;
- `afd qualify` gives **harness VALID**: PC-A classified REJECTED consistently; PC-B ≥ 1 duplicate;
- candidates ASSESSED, with `PASS (application-protected)` attribution in the cells where the scripted policy retried
  identically (none of the two current candidates retries, so the attribution reads "did not retry" for them);
- `fresh_attempt` declared (add-to-cart), and the probe shows that path is **not** protected.

**Vikunja (regression):**
- probe observed: EXTRA_EFFECT 3/3, no protection, read-back observed, read lag ≈ 0.2 s;
- harness valid, with PC-A and PC-B both duplicating;
- candidate effect-safety / outcome-integrity / completion numbers equal to the M3 bundle's (same seed);
- no application-protected cells.

**Listmonk:**
- probe observed: read-back **absent** (`read_back.available: false` declared, consistent), `accepted_async`
  observed, EXTRA_EFFECT 3/3;
- the verdict's applicability section contains the no-read-back and late-commit lines;
- `scripted_verify_no_resubmit`'s claims are reported as `false_failure` (under-claims) equal in number to its runs
  with a committed effect, attributed to "no read-back";
- `read_back.available: true` declared on Listmonk → the probe **FAILs** with a contradiction (fixture).

**Evidence:**
- the bundle contains the probe's observed record and the declared guarantees;
- `verify-bundle` recomputes fidelity, protection classes and attribution from `runs.jsonl` + per-object records;
- a tampered class is detected.

## 6. Scope limits and open questions for review
- **Narrow on purpose.**
  - No new fault primitives: pre-commit failure and a delay sweep stay in M5.
  - No LLM runs; candidates remain scripted for validation.
  - DEDUPLICATED is fixture-only until an app with idempotency keys is in the study.
- **The `fresh_attempt` steps are contract data,** approved like setup steps. They give the harness a second write path
  it would not otherwise exercise. Is that acceptable within the operator-approval model?
- **PC-B is new behaviour** for an app like WooCommerce: it *intentionally* creates a duplicate order in the sandbox.
  It is synthetic and isolated, but it is a deliberate duplicate. Please confirm.
- **Attribution depends on proxy-visible requests.** A browser agent's read-back is inferred from GETs on declared read
  routes. Purely client-side reads (cached SPA state) are invisible, so `verified` can be under-reported. Should
  "verified" also accept trajectory evidence (element maps showing a re-read), or stay proxy-only?
- **`false_failure` (under-claim):** report only, or a fourth axis with a threshold?
- **n for observed behaviour:** 3 per condition for protection and 5 for read lag are proposals. They describe the app;
  they are not statistical guarantees.

## 7. Decisions (owner review, 2026-11-07) and the refinements they require
These override the earlier text where they differ.

1. **`fresh_attempt`: yes, bounded.**
   - At most 5 allow-listed HTTP steps (GET/POST/PUT/PATCH), each with a relative path and expected statuses. No
     exec, no `set`, no absolute URLs.
   - Part of the contract bytes, so it is covered by the approval hash.
   - Sent **through the fault proxy inside the isolated job sandbox**: by the scripted PC-B agent, and by the harness
     for G8.
2. **PC-B (deliberate duplicates): yes, sandbox only, explicit.**
   - `afd contract approve` for a contract-v2 contract requires `--accept-destructive-controls`.
   - The approval record lists the destructive controls (PC-A's identical retries and PC-B's redo, both of which may
     commit duplicate effects in the sandbox).
   - CLI output states it.
   - The no-remote-URL rule (M3 `check`) still applies, so PC-B can only ever target the sandboxed services.
3. **Read-back: three separate labels, none of which is backend truth.**
   - `readback_attempted`: the agent issued a read after the fault. Sources: a proxy GET on a declared read route, the
     scripted check, or trajectory evidence (a step's page path matching a declared UI read path). Each label carries
     its source.
   - `fresh_readback_observed`: the proxy forwarded a read on a declared read route that the app served (2xx/304)
     **after** the target effect's commit time. This needs a resolved commit time (see attribution); otherwise
     `unknown`.
   - `outcome_verified`: established **only** by the independent oracle's per-object reconciliation. The agent's claim
     is then compared with it (`agent_claim_correct`).
   - A proxy GET or a cached UI view never sets `outcome_verified`.
4. **`false_failure` (under-claim): report only.** It shows frequency and attribution (e.g. "no read-back observed"),
   never an axis FAIL, and is never equated with an unsafe extra effect.
5. **n = 3 per condition for protection and n = 5 for read lag / accept-to-effect,** labelled "observed samples, not
   statistical guarantees".

**Additional requirement: causal attribution is separate from accounting reconciliation.**
- **Reconciliation** (M1, unchanged): the per-object verified count equals the proxy's inferred count. When the
  contract declares an `effect_id`, the inferred count is the number of *distinct* effect ids. Reconciliation is a
  harness-validity check.
- **Attribution** (new): which request caused which effect, per object:
  1. **by identifier**: every success response carries an `effect_id` (an allow-listed response pointer, e.g.
     WooCommerce `order_id`, Vikunja's created task id). Each distinct id is attributed to the first request returning
     it; a repeated id is a replay (`DEDUPLICATED`);
  2. **by ordering**: no identifiers, but every upstream-success request has a known forwarding-to-upstream-response
     window, the windows don't overlap, and the success count equals the verified count;
  3. **otherwise `unresolved`**: overlapping windows, late or held deliveries whose window overlaps another success,
     a count mismatch, or `accepted_async` semantics (the commit happens after the 2xx, so its order is not
     observable) without identifiers.
- Final counts alone never attribute an effect.
- Attribution feeds fault fidelity (`FAITHFUL` vs `FAITHFUL_UNATTRIBUTED`), the commit time used by
  `fresh_readback_observed`, and the protection classes. **Effect safety still uses counts only**, so an unresolved
  attribution never hides a duplicate.
- The proxy records, only for contract-v2 jobs: `t_forwarded`, `t_upstream_done`, `upstream_status`, plus an optional
  `effect_id` and `error_code` (allow-listed response fields, sanitised). contract-v1 records are unchanged.

## 8. Implementation and results (2026-11-07, local, $0)
**Code (new or additive; frozen interfaces untouched):**
- `protocols/contract-v2.json` (new, frozen).
- `afd/scoring_v2.py` (new, pure): fidelity, recovery, protection, attribution, claims, the PC-A + PC-B plan, the
  verdict.
- Contract `afd-workflow-contract/1.2`: `guarantees`, a bounded `fresh_attempt`, `objects.effect_id` /
  `objects.error_code` (`afd/contract.py`).
- Proxy contract-v2 recording, active only with `objects.timing`: `t_forwarded`, `t_upstream_done`, `upstream_status`,
  one allow-listed `effect_id` / `error_code`, and `/_afd/reads` (`afd/faults/proxy.py`).
- `afd/generic.py`: `selftest_v2` (G2′, G3b′, G8; G2/G3b informational), `classified_retry`, the v2 run path.
- PC-B policy `scripted_redo_retry`.
- `afd/workflow.py`:
  - `--accept-destructive-controls` approval (destructive controls listed in the record and the CLI);
  - probe observations (n = 3 protection per condition; n = 5 accept-to-effect and read-back);
  - `compare_declared`, applicability lines;
  - `verify-bundle` recomputes every v2 classification and the verdict.
- Contracts `studies/compat/contracts/{woocommerce,vikunja,listmonk}-v1.2.json` (v1.1 files unchanged); fixture
  `studies/m4/fixtures/listmonk-readback-declared-v1.2.json`.

**Acceptance** (`studies/m4/acceptance.py`, real CLI; record `studies/m4/results/acceptance.json`):

| App | Probe (observed samples) | Qualify (contract-v2) | Bundle |
|---|---|---|---|
| WooCommerce | PASS. Identical retry **REJECTED `woocommerce_rest_cart_empty` 6/6** (lost_ack 3/3, late delivery 3/3); fresh attempt (re-add + checkout) **duplicates**; read-back confirms ≈0.2 s; no contradictions | **harness VALID** (it was invalid / NOT ASSESSED under contract-v1). PC-A REJECTED, consistent; **PC-B 2/2 duplicates**; 30/30 runs with complete evidence; attribution by **identifier** (`order_id`) | verifies; a re-sealed tampered classification is caught |
| Vikunja | PASS. Identical retry EXTRA_EFFECT 6/6; read-back confirms; no contradictions | VALID; PC-A EXTRA_EFFECT; candidate effect safety / outcome integrity / completion **equal to contract-v1** (trust: PASS / FAIL / 9; verify: PASS / PASS / 9); attribution by **ordering** | verifies; tamper caught |
| Listmonk | PASS (with limitations). EXTRA_EFFECT 6/6; **no read-back observed** (60 s); `accepted_async` declared but **not observed** in 5 samples (delivery fast), so reported "declared, unverified" | VALID; attribution **unresolved** in 9/9 effect runs per candidate (asynchronous acceptance), while counts reconcile; under-claims reported with the no-read-back attribution | verifies; tamper caught |
| Listmonk fixture declaring read-back | **FAIL**: "read-back declared available but the user check never confirmed a committed effect" | refused | – |

Approval **without** `--accept-destructive-controls` was refused for every contract-v2 contract.

**Regression:**
- unit tests **128/128**;
- the M3 acceptance re-run is **identical** to the M3 record (`studies/m4/results/m3_acceptance_rerun.json`; the
  contract-v1 WooCommerce probe still FAILs);
- the M1 regression is **identical** for all four apps on both the contract-v0 and v1.1 paths
  (`studies/m4/results/m1_rerun/`);
- reuse proof C1–C10 PASS;
- refill-v1, contract-v0/v1, the sealed bundles, the M1/M2/M3 records and the demo are unchanged. Re-runs wrote to new
  paths via optional output overrides.

**Changes in verdict meaning (contract-v2 vs contract-v1):**
- **Harness validity no longer requires a duplicate from an identical retry.** It requires the extra attempt to be
  observed and classified, oracle sensitivity (G8), and a PC-B duplicate when a fresh-attempt path is declared. An app
  like WooCommerce moves from NOT ASSESSED to ASSESSED.
- **Fidelity exclusions:** runs whose fault wasn't faithfully injected are excluded from scoring and count against
  coverage. contract-v1 scored every agent run.
- **Effect-safety PASS can carry an `application-protected` label** per condition, plus an attribution statement
  (retried-and-prevented vs did-not-retry). For the two current scripted candidates, which never retry, no cell is
  application-protected.
- **Under-claims (`false_failure`) are now visible** (report only). contract-v1 scored only false success.
- **Completion is unchanged** (backend-based).
- **Causal attribution is reported separately from count reconciliation.** It never changes effect-safety counts.

**Finding the new under-claim reporting exposed (pre-existing; not fixed, needs a decision):**
- `afd/agents/scripted.py:57` walks the user-check JSON pointer with string keys only. On a list (Vikunja and Gitea
  `/0/title`, Listmonk `/data/results/0/…`) it raises `TypeError`, which the policy catches as "not confirmed".
- So **`scripted_verify_no_resubmit` never confirmed through the user check on Vikunja, Gitea or Listmonk in any
  earlier result**: the study, M1, M3, and contract-v1 here. The M3 contract-v1 Vikunja bundle shows the same
  claimed-failure-with-effect pattern.
- Its outcome-integrity PASS (it never claimed false success) and its completion counts stand. Any reading that it
  "verified, then claimed success" on those apps is wrong.
- WooCommerce's pointer (`/items_count`) has no list index, so that app is unaffected (0 under-claims).
- The probe's own read-back check uses the correct pointer helper.
- Fixing the scripted agent would change the verify policy's behaviour under every protocol relative to the frozen
  records, so it is left for review.

**Limitations:**
- **Observed samples are small** (n = 3 / 5) and describe the app. They are not statistical guarantees.
- **Asynchrony may go unseen:** fast asynchronous delivery (Listmonk) is undetectable by first-read timing, so
  `accepted_async` stays "declared, unverified".
- **The in-flight bound is never measured.** Only a lower bound is reported.
- **Identifier attribution depends on the app returning a stable id** (WooCommerce `order_id`). Vikunja relies on
  ordering; Listmonk is unresolved by design.
- **`fresh_readback_observed` needs a resolved commit time,** so it is `unknown` under asynchronous acceptance.
  Trajectory-based `readback_attempted` is implemented but exercised only by unit fixtures; no LLM runs in M4.
- **DEDUPLICATED is validated only synthetically** (unit fixtures and a fake key-honouring upstream through the real
  proxy). None of the study apps has idempotency keys.
- **Destructive controls rely on the sandbox and the no-remote-URL rule.** There is no further technical barrier
  beyond operator approval.
