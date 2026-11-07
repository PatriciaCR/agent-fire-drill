# Erratum: scripted read-back verification on Vikunja, Gitea and Listmonk (M4.1, 2026-11-07)

## What was wrong
The historical scripted policy `scripted_verify_no_resubmit` (`afd/agents/scripted.py`, v1) walked its user-check
JSON pointer with **string keys only** (line 57, `v = v[part]`). When a pointer step indexes an **array**, this raises
`TypeError`, which the policy caught as "not confirmed". Affected checks:

| App | User-check pointer | Affected |
|---|---|---|
| Vikunja | `/0/title` | **yes** |
| Gitea | `/0/title` | **yes** |
| Listmonk | `/data/results/0/attribs/afd_sent` | **yes** (it also has no read-back, so it could not confirm either way) |
| WooCommerce | `/items_count` | no |
| Expense demo, refill (reuse proof) | `/status`; `/items` → `/refills_remaining` (object keys) | no |

**Consequence:** on Vikunja, Gitea and Listmonk, `scripted_verify_no_resubmit` **never confirmed a committed
effect**. It claimed failure in every run, including the runs where its effect had committed. This affects every
earlier result: the compatibility study (contract-v0), M1 (contract-v1), M3 (`afd qualify`, contract-v1) and M4
(contract-v2).

## What this does and does not change in earlier claims
- **Unsupported, and withdrawn:** any statement or reading that the scripted verify policy *verified, then claimed
  success* on Vikunja, Gitea or Listmonk.
  - In `docs/COMPAT_STUDY_REPORT.md` (candidate row), "verify: both PASS" for Vikunja and Gitea stands as an axis
    result. But the policy did **not** confirm through the user check there; it abstained exactly as it did on
    Listmonk.
  - The study attributed only Listmonk's abstention to "no read-back". On Vikunja and Gitea the abstention was caused
    by this bug.
- **Still correct:** effect safety (PASS), outcome integrity (PASS, i.e. no false-success claims) and completion counts
  (backend-based, 9/12). None of them depended on the policy's claims being successes.
- **Under-claims reported by contract-v2 (M4):** Vikunja's 9 `false_failure` under-claims (verify policy) came from this
  bug. For Listmonk they are genuine (no read-back), as confirmed below.
- **Unchanged and still valid:** WooCommerce, and the reuse proof's expense and refill demos.

## Correction (new code; historical code kept)
- `afd/agents/scripted_v2.py` (scripted-policy **v2**):
  - RFC 6901 pointers (array indices, `~0`/`~1`, empty pointer);
  - an explicit read-back record per check: `attempted`, `lookup` (`found` | `missing` | `malformed_pointer` |
    `no_json` | `http_error`) and `confirmed`;
  - a 200 response, or a lookup that ran, is **not** a confirmation unless the expected value was found and matched.
- `afd/agents/scripted.py` (**v1**) is unchanged; `tests/test_scripted_v2.py` pins it byte-for-byte to its
  pre-correction commit. It remains the library default (`afd.generic.run`), so reproduction scripts reproduce the
  frozen results.
- `afd qualify --scripted-version {v1,v2}` (**default v2** for new qualifications).
- Every new bundle records `scripted_policy_version` and the policy file's sha256 (summary, environment, per-run
  records). `afd contract verify-bundle` reports the version, checks that summary, environment and every run result
  agree, and labels older bundles "unrecorded (bundle predates M4.1; v1 implied)".

## Fresh $0 comparison (same contracts, UI scripts and seed 20261107; `studies/m41/results/comparison.json`)
`scripted_verify_no_resubmit`, 12 scored runs per app (9 with a committed target effect; 3 under `fabricated`, where
nothing commits):

| Protocol: app | Version | Read-back attempted | Read-back **confirmed** | Lookup outcomes | Claimed success | Claimed failure *with* a committed effect | Effect safety | Outcome integrity | Completion |
|---|---|---|---|---|---|---|---|---|---|
| contract-v1: Vikunja | v1 | 12/12 | **0** | (not recorded by v1) | 0 | 9 | PASS | PASS | 9 |
| | **v2** | 12/12 | **9** | found, missing | **9** | **0** | PASS | PASS | 9 |
| contract-v1: Gitea | v1 | 12/12 | **0** | (not recorded) | 0 | 9 | PASS | PASS | 9 |
| | **v2** | 12/12 | **9** | found, missing | **9** | **0** | PASS | PASS | 9 |
| contract-v1: Listmonk | v1 | 12/12 | 0 | (not recorded) | 0 | 9 | PASS | PASS | 9 |
| | **v2** | 12/12 | **0** | missing | 0 | 9 | PASS | PASS | 9 |
| contract-v2: Vikunja | v1 | 12/12 | 0 | (not recorded) | 0 | 9 (9 under-claims) | PASS | PASS | 9 |
| | **v2** | 12/12 | **9** | found, missing | **9** | **0** | PASS | PASS | 9 |
| contract-v2: Listmonk | v1 | 12/12 | 0 | (not recorded) | 0 | 9 (9 under-claims) | PASS | PASS | 9 |
| | **v2** | 12/12 | 0 | missing | 0 | 9 (9 under-claims, genuine) | PASS | PASS | 9 |
| contract-v2: WooCommerce (regression) | v1 | 12/12 | 9 (inferred from claims) | (not recorded) | 9 | 0 | PASS | PASS | 9 |
| | **v2** | 12/12 | 9 | found | 9 | 0 | PASS | PASS | 9 |

- **Verdicts:** no candidate verdict or completion count changed in any case. `scripted_trust_response`, which never
  reads back, is identical under v1 and v2 everywhere (effect safety PASS, outcome integrity FAIL with 3
  false-success claims, completion 9).
- **The correction changes only the verify policy's claims and read-back record:**
  - on Vikunja and Gitea, the 9 committed runs now confirm and claim success (true success). The 3 fabricated runs
    correctly do not confirm (`missing`) and claim failure (true failure);
  - on Listmonk, nothing confirms, because the expected value is genuinely absent.
- **Integrity:** all 12 bundles verify, and verification reports the producing version (v1 or v2), matching the
  current files.

## Not changed
- Original bundles, protocol files and the published result data (`studies/compat/results/`,
  `studies/compat/results/m1/`, `studies/m2/`, `studies/m3/results/`, `studies/m4/results/`) are unchanged.
- This erratum and short pointers in the affected reports are the only edits to earlier documents.
