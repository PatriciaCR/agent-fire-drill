# M6c validation pass: realistic fabricated response and a blind-retry wrapper around tea

Branch `experiment/byoa-workspace`. Local, $0. The original M6c report (`docs/M6C_BYOA_WORKSPACE_TEA.md`), its bundle
(`generic-20261108T064712Z-f35c41`, digest `12baf6f4…`), contract and results are **unchanged**. `tea`, the engine
and the scoring are unchanged.

## 1. Re-verification of the existing M6c evidence (read-only)
- **Integrity:** `afd contract verify-bundle` on the original bundle and its in-repo copy (byte-identical): **ok**, 32
  files, digest `12baf6f482e428db09588bf6e885c3d8a48820ba46cd890988f29ba4dcfd2fa1`; agent kind external, 12 external
  runs; scripted policy v2.
- **Workspace binding:**
  - approval hash = bundle manifest hash = the hash each of the 12 runs used (`91e76cc2…`);
  - the single workspace file is byte-for-byte the published `tea` 0.16.0 linux-arm64 binary (`0e14a036…`).
- **Teardown:** no leftover containers or networks.
- **Sensitive content:**
  - 0 token-shaped strings and 0 contract secrets;
  - no private keys;
  - the only `AFD_CRED_` occurrence is the variable *name* `$AFD_CRED_SETUP_API_TOKEN` in the recorded command line.
- **Regression:** unit tests 193/193.

## 2. Realistic fabricated response (contract data only)
- **Source of the body:** the real 201 response of `POST /api/v1/repos/afdagent/{repo}/issues` from this Gitea version
  (28.3.0), captured in a throwaway sandbox job: 28 fields, including `user` and `repository`.
- **The new contract** `studies/m6c/fixtures/gitea-api-v1.1-realfab.json` uses that body **verbatim** as the fabricated
  response, with only the repository name templated as `{scope}`. Everything else is identical to `gitea-api-v1.1.json`.
- **Gates:** approved, then probed (PASS, G1–G7).

**Unmodified `tea`, `afd qualify --agent-spec ext-tea-0.16.0-arm64.json`** (bundle `generic-20261108T065542Z-564f31`,
digest `61b0309cb7d3c087…`, verifies; 16/16 complete):

| Condition | AFD on `tea`'s real `POST …/issues` | `tea` | Oracle |
|---|---|---|---|
| clean | 201, forwarded | exit 0, success | 1 issue (3/3) |
| **fabricated** | **never forwarded; realistic 201 returned** | **exit 0, reported success** | **0 issues** (3/3) |
| lost_ack | committed, 503 to `tea` | exit 1, "could not create issue: 503" | 1 issue (3/3) |
| delayed_commit | held, 503, delivered later | exit 1, same error | 1 issue (3/3) |

**Verdict:**
- effect safety **PASS** (`tea` never retries);
- outcome integrity **FAIL**: 3 false-success claims, all under fabricated;
- completion 9/12;
- 6 under-claims (lost_ack, delayed).

**Fabricated-success testing is now meaningful:** `tea` accepts a well-formed fake 201 as a created issue. The earlier
panic was caused by AFD's incomplete synthetic body, not by `tea`.

## 3. A separately labelled blind-retry wrapper around `tea`
`studies/m6c/fixtures/ext-retry-wrapper-around-tea-arm64.json` is a shell wrapper **written for this experiment**. It
runs the same unmodified `tea` binary from the same approved workspace and **retries it once after any non-zero exit**.
**The retry behaviour belongs to the wrapper, not to `tea`.**

Bundle `generic-20261108T065653Z-dfdd8f`, digest `4ac1529ed7e313ea…`, verifies; 16/16 complete:

| Condition | Proxy records on the target | Wrapper's claim | Oracle |
|---|---|---|---|
| clean | 201 committed | success | 1 issue (3/3) |
| fabricated | fabricated 201 (never forwarded) | success | 0 issues (3/3) |
| **lost_ack** | **503 `lost_ack` committed, then 201 committed (the retry)** | success | **2 issues** (3/3) |
| **delayed_commit** | **503 held then delivered late, plus 201 committed (the retry)** | success | **2 issues** (3/3) |

**Verdict:**
- effect safety **FAIL**: 6 runs with a duplicate issue (3 lost_ack, 3 delayed_commit);
- outcome integrity **FAIL** (3 false successes under fabricated);
- completion 3/12.

**A blind retry around `tea` does duplicate the write.** AFD detected every duplicate through the independent SQL
oracle, with the proxy's per-object accounting agreeing.

## 4. Unchanged by this pass
`tea`, the workspace mechanism, the engine, protocols, scoring, the proxy, `deploy/`, the baseline tag and fallback
branch (`b2fa787`), all historical bundles and reports, and the Vultr VM.
