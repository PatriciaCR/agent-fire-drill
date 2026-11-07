# M3: Test Your Workflow, the minimum operator-approved developer workflow

Status: implemented and accepted locally, 2026-11-07. $0: scripted agents only, no inference. Not deployed, no public
submission. Try a Demo, refill-v1, the sealed bundles, the bundle validator and the frozen study results are unchanged.

## The workflow
```
bin/afd contract check   CONTRACT --ui UI                       # static lint; lists the images to approve
bin/afd contract approve CONTRACT --ui UI --approver "NAME"     # operator approval record
bin/afd contract probe   CONTRACT --ui UI                       # $0, local: self-test G1-G7 + real UI traffic
bin/afd qualify --contract CONTRACT --ui UI [--seed N]           # $0 contract-v1 qualification, scripted agents
bin/afd contract verify-bundle BUNDLE_DIR                        # integrity + recomputed verdict + approval binding
```
Exit status: 0 pass, 1 fail or refused. Operator state lives in `var/workflow/{approvals,probes,runs}` (gitignored).

**Order: check → approve → probe → qualify.** Approval comes *before* anything runs (the plan had probe first), so no
contract image ever starts unapproved.

**Inputs:**
- **Contract:** `afd-workflow-contract/1.1` (`docs/M1_PER_OBJECT_ACCOUNTING.md`).
- **UI script:** `{"steps": [...]}` or `{"none": "<why there is no UI>"}`; the study apps' `browser` format is
  accepted. Steps: nav (proxy origin only), wait, fill, select, click, submit, key, sleep, dump, shot. They are run by
  the strict CDP driver (`afd/cdp_driver.py`), which stops at the first missing element instead of typing into
  whatever has focus.

## What each step enforces
**check** (`afd/workflow.py:check`), static:
- schema;
- version 1.1 (per-object target, decoys, family oracle);
- images pinned by digest (`name@sha256:<64 hex>`);
- service keys allow-listed: no privileged mode, capabilities, devices, ports, volumes, network or security options;
- non-root uid; allow-listed sysctls; writable paths not `/`, `/proc`, `/sys`, `/dev`, `/etc`, …;
- read-only, repository-confined mounts;
- **no remote URLs anywhere** (any `scheme://host` whose host isn't a declared service or `afd-proxy`);
- authorisation attestation;
- **adequate oracles**: both the scalar and the per-object oracle present, each declaring an independence level
  L1–L3. Exec oracles must have pinned images and no write statements. HTTP oracles must use GET against a declared
  service, never the proxy;
- UI steps confined to the proxy origin.

Warnings (not failures): undeclared `success_status`, and an action provenance that doesn't say it was observed.

**approve:** requires a passing check. Records sha256 of the contract file, the image list, sha256 of the UI script,
the protocol, a hash of the check report, the approver and the time. **Any change** to the contract bytes, the image
set or the UI script invalidates it, and probe/qualify say which.

**probe:** requires a matching approval and all approved images **present locally** (it never pulls). On the M2
topology without the gateway:
1. self-test G1–G7;
2. a scripted UI session through the proxy, which must
   - be **intercepted** as the declared action (`action.path_regex`) on the target object,
   - produce a verified oracle effect,
   - reconcile per object.

Otherwise it fails with "declared action never observed" and lists the writes the UI actually sent. It derives
capabilities (duplicate-detectable, late-commit-detectable, other-object-detectable) and limitations.

**qualify:**
- **Refuses** unless there is a matching approval, local images, and a **passing probe bound to that approval and UI
  script**.
- `--agent scripted` only (`llm` refused). The positive control is the protocol's deterministic `scripted_blind_retry`,
  never a candidate. Candidates are scripted policies.
- Runs contract-v1 (per-object scoring, independent oracle) and seals a bundle that includes the approval, the
  contract source hash and the probe summary.
- **Evidence gate:** every planned run must have a scored agent record with a claim and steps. Any gap is listed, and
  the bundle is labelled `NOT A QUALIFICATION`.

**verify-bundle:**
- manifest integrity;
- per-run outcomes and the verdict **recomputed** from `runs.jsonl`;
- evidence completeness recomputed;
- status consistent with validity and evidence;
- the approval covers the contract hash and images that ran.

## Acceptance (`studies/m3/acceptance.py` drives the real CLI; record `studies/m3/results/acceptance.json`)
| App | check | approve | probe | qualify | verify-bundle |
|---|---|---|---|---|---|
| Vikunja | PASS | ok | **PASS**: UI write intercepted as `create_task` (201, committed), reconciled; G1–G7 | QUALIFICATION RESULT, 28/28 runs with complete evidence | ok |
| Gitea | PASS | ok | **PASS**: same | QUALIFICATION RESULT, 28/28 | ok |
| Listmonk | PASS | ok | **PASS (with limitations)**: no UI for `POST /api/tx`; UI traffic NOT_APPLICABLE, API only | QUALIFICATION RESULT, 28/28 | ok |
| WooCommerce | PASS | ok | **FAIL**: G2/G3b; identical resubmission did not create a second effect (native duplicate prevention); UI checkout intercepted and reconciled | **REFUSED** (the probe did not pass) | – |
| Vikunja doc-route fixture | PASS (warning: provenance not observed) | ok | **FAIL: "declared action never observed"**; the UI sent `POST /api/v2/projects/:id/tasks/bulk`. The self-test passes on the documented route, so only the UI check fails. | **REFUSED** | – |

- Candidate results match the frozen study (scripted_trust_response: outcome integrity FAIL; scripted_verify_no_resubmit:
  PASS; both 0 unauthorised effects in 12 runs; 9/12 completed).
- Tampering:
  - an edited `runs.jsonl` fails the manifest;
  - the same edit **re-sealed** fails the per-object recomputation (`stored target_effects=2 but recomputed 1`).
- The known limitations are preserved:
  - Listmonk: API-only; no read-back for the verification policy;
  - WooCommerce: native duplicate prevention, so the duplicate positive control cannot validate the harness.

Unit tests: **106/106 OK**. New:
- `tests/test_workflow.py`: 20 unsafe-contract fixtures rejected, UI-script confinement, the approval gate (unapproved /
  changed contract / changed image / changed or removed UI / tampered record), qualify refusals, CLI exit codes;
- `tests/test_worker_v2.py`: large-record export (30 steps × 40 actions, ~100 KB strings, deep nesting, non-ASCII,
  embedded tokens, password fields), plus depth and odd types.

## Security limitations
- **Integrity, not authenticity.** Bundles and approvals are hash-chained and recomputed, not signed. Someone with
  write access to `var/` and the repository can forge a consistent approval or bundle. The approval binds hashes; it
  doesn't authenticate the approver.
- **Local operator trust.** The approver name is free text. Approval files are protected only by filesystem
  permissions, and there's no second-person rule.
- **Static oracle lint is shallow.** "Read-only" is checked by keyword and by the declared independence level. Real
  read-only enforcement depends on the credentials the contract creates (e.g. a SELECT-only role), which `check`
  cannot verify.
- **Setup steps are powerful.** `setup_once`/`setup_per_run` exec steps run inside the contract's own services, by
  design. They are approved as part of the contract hash, but not sandboxed beyond the service containers.
- **Probe coverage is the UI script's coverage.** A UI path the script doesn't exercise is not checked. Effects on
  routes or object kinds the contract doesn't declare are caught only if the family oracle sees them.
- **Images:** digest pinning plus "never pull" means only images already on the host run. Their provenance before
  that point (who loaded them) is outside AFD.
- **Rootful dev mode only.** The VM's rootless topology for generic jobs is M6.

## Integration friction found
- **Locally built images can't be digest-pinned.** `afd/site:1`, built locally, has no registry digest, so
  `contracts/refill-breakingweb-v1.1.json` is rejected by `check`. That contract also lacks an independence level on
  its scalar oracle. Supporting local images needs a pinning scheme by image ID.
- **Each contract needs a UI script as well as the contract.** The probe needs one; authoring selectors (and keeping
  them current) is the main per-app effort.
- **Native duplicate prevention blocks qualification** (WooCommerce) even though the app is drillable for other
  faults. A different positive control for such apps is a design decision for later (M4/M5).
- **API-only actions** (Listmonk) can only be probed at the API, and that is reported as a limitation.
- **Run time:** probe 47–72 s; qualify 4–8 minutes per app (28 runs).
- Approver names are restricted to `[\w .@-]`; my acceptance driver first used parentheses and was refused.


> **Erratum (2026-11-07, M4.1):** the scripted `scripted_verify_no_resubmit` policy (v1) could not read array-indexed user checks, so on **Vikunja, Gitea and Listmonk it never confirmed a committed effect** and claimed failure in every run. Claims or readings that it verified and then claimed success on those apps are unsupported. Its effect-safety, outcome-integrity and completion results stand. Corrected policy (v2), side-by-side comparison and scope: `docs/ERRATUM_SCRIPTED_READBACK.md`. The original result data are unchanged.
