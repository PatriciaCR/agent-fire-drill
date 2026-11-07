# Adversarial compatibility study: results (2026-11-07)

Protocol: `docs/COMPAT_STUDY_PROTOCOL.md` (pre-registered at `7d8e40a`; amendments AM-1 to AM-6 at `08e118b`, recorded
before any app was evaluated). Final evaluation pass: `studies/compat/results/final/summary.json`. The code was unchanged
for the whole pass (hash-checked at start and end). Cost: $0. Scripted HTTP policies and a scripted Chromium only; no
LLM calls; no VM changes.

## 1. Formal decision under the original rules: **GO**
| §3 GO criterion (unchanged) | Result |
|---|---|
| ≥ 3 of the 4 required apps reach COMPATIBLE or COMPATIBLE-WITH-EXTENSION | **4 / 4 COMPATIBLE-WITH-EXTENSION**. 0 / 4 were COMPATIBLE with v0 unchanged (Stage 0) |
| The union of generic extensions is a bounded set: each used by ≥ 2 apps or clearly class-general | **Met** (§5): every extension is generic; none names an app (enforced by a test) |
| lost_ack F2 on ≥ 3 apps | **4 / 4** |

The floors also hold for every app: an oracle at ≥ L1; D4, D7 and D8 pass; zero app-specific AFD lines; zero app source
or image changes. WooCommerce meets D7 through the clause pre-registered for exactly this case: the app's native
duplicate prevention is demonstrated (AM-2) and the harness correctly reports NOT ASSESSED.

**What GO does and does not mean.**
- It means the contract-driven architecture (edge proxy, contract, independent oracle, unchanged engine) reached these 4
  unmodified applications with a bounded set of generic extensions.
- It does **not** mean arbitrary sites or arbitrary agents work.
- It does **not** mean browser-agent compatibility in general: see §2.
- It covers one consequential action per app, in a local sandbox.

## 2. Browser workflow, reported separately (not implied by GO)
| App | Action has a UI? | Real browser (scripted Chromium, no LLM) through the proxy | Level |
|---|---|---|---|
| Vikunja | yes (SPA) | All 3 faults applied and verified. Needed a contract derived from **observed** UI traffic and a generic proxy fix (keep-alive replay) | **B1** |
| Gitea | yes (server-rendered + fetch) | All 3 faults applied and verified. Needed a generic proxy fix (Host preservation) | **B1** |
| WooCommerce | yes (block checkout) | All 3 faults applied and verified, with the proxy as it already was | **B2** |
| Listmonk | **no**: `POST /api/tx` is API-only | not applicable | **B0** for this action (by design, not a proxy failure) |

- **3 of 4 apps' actions were exercised through a real browser**; 2 of those 3 needed a generic proxy fix that only the
  browser path revealed.
- **No LLM browser agent was run on any of these apps.** "Browser plumbing works" is established. How an LLM agent behaves
  on these UIs is not.

## 3. Per-app results (final pass)
| | Vikunja 2.8.0 | Gitea 28.3.0 | Listmonk 6.2.0 | WooCommerce 11.2.2 |
|---|---|---|---|---|
| Architecture class | REST SPA, bearer token | server-rendered + fetch forms, cookie session | async effect, 3 services | cookie/cart session, block checkout, 2 services |
| Services | app + PostgreSQL 17.11 | app + PostgreSQL 17.11 | app + PostgreSQL 17.11 + Mailpit 1.33.0 | WordPress 7.1.4 + MariaDB 11.8.9 |
| Stage 0 (v0 unchanged) | fails at start (no config channel) | fails at start (needs root, writable /data) | not expressible | not expressible |
| **D1** | COMPATIBLE-WITH-EXTENSION | COMPATIBLE-WITH-EXTENSION | COMPATIBLE-WITH-EXTENSION | COMPATIBLE-WITH-EXTENSION |
| **D2** lost_ack / delayed / fabricated | F2 / F2* / F1 | F2 / F2* / F1 | F2 (API client) / F2* / F1 | F2 / F2* / F1 (most convincing) |
| **D3** oracle | L2 read-only SQL; = API on 5/5 | L2 read-only SQL; = API on 5/5 | L3 external SMTP sink; cross-check reads the same sink (**counting consistency only**) | L2 read-only SQL; = app order API on 5/5 |
| **D4** isolation | per-run project; pass | per-run repo; pass | per-run recipient; pass | per-run cart + email; pass |
| **D6** browser | B1 | B1 | B0 (API-only) | B2 |
| **D7** engine | harness valid; positive control 4/4 duplicates | valid; 4/4 | valid; 4/4 | **NOT ASSESSED: native duplicate prevention** (resubmission → 409 `woocommerce_rest_cart_empty`) |
| **D8** containment | pass | pass | pass | pass |
| AM-1 proxy-inferred = verified, all runs | yes | yes | yes (assumption fragile: 200 = *accepted*) | yes in every self-test case |
| AM-2 why no duplicate | duplicate produced | duplicate produced | duplicate produced | **native duplicate prevention** (fault applied; extra attempt rejected by the app) |
| AM-3 unclassified extra writes | 0 | 0 | 0 | 0 (cart `/batch` writes classified as supporting) |
| AM-4 native accepted-then-late-commit | none observed | none observed | **yes** (200 = accepted for sending) | none observed |
| AM-5 read lag (oracle / user check) | 0.2 s / 0.2 s | 0.2 s / 0.2 s | < 0.1 s / **never (no read-back)** | 0.2 s / 0.3 s |
| Native idempotency keys | none | none | none | none (prevention comes from cart state) |
| Candidate policies (scripted) | trust: integrity FAIL; verify: both PASS | same | same (verify can never confirm, so it always abstains) | not assessed |

\* F2 relative to **edge-deferred delivery** (AM-4). This is not "accepted, then committed late inside the app".

## 4. Generic code reuse vs developer integration effort (kept separate)
**Generic AFD code** (reused by every app; `studies/compat/results/effort.json`): +355 / −149 lines across
`afd/contract.py`, `afd/generic.py`, `afd/faults/proxy.py` and `afd/agents/scripted.py` since pre-registration.
**App-specific lines: 0**, enforced by `tests/test_contract.py::TestNoHardcoding`, which now also lists every study app's identifiers.
The engine (`afd/qualify.py`) and bundles were unchanged during the study.

**Developer integration effort per app** (contract data and configuration; no code, no source or image changes):

| | Contract (JSON lines) | Services | Env vars | Writable paths | Sysctls | Entrypoint overrides | Setup steps (once / per run, exec) | Browser steps (data) | Recorded workarounds |
|---|---|---|---|---|---|---|---|---|---|
| Vikunja | 216 | 2 | 11 | 2 | 0 | 1 | 3 / 1 (1 exec) | 12 | 3 |
| Gitea | 235 | 2 | 13 | 4 | 0 | 0 | 4 / 1 (3 exec) | 12 | 3 |
| Listmonk | 258 | 3 | 13 | 2 | 0 | 1 | 5 / 1 (1 exec) | — | 2 |
| WooCommerce | 338 | 2 | 8 | 5 | 1 | 0 | 7 / 3 (7 exec) | 19 | 3 |

The integration effort was real: exploring each app's actual traffic, auth model and datastore took the majority of the
work. Section 7 lists every workaround.

## 5. The extension set (what unlocked the apps)
| Generic extension | Apps that needed it |
|---|---|
| Multi-service apps on the sealed network (service names as hosts) | all 4 |
| Service configuration: env vars; writable paths owned by the service uid (mode 0755) | all 4 |
| Setup steps (HTTP or exec in a service) with captures; secret values redacted from evidence | all 4 |
| Agent identity headers (token, cookie, cart token) | all 4 |
| Configurable success statuses (proxy inference only) | all 4 |
| Independent oracles: exec sidecar (read-only SQL) | 3 (Vikunja, Gitea, WooCommerce) |
| Independent oracles: external sink with settle policy | 1 (Listmonk; class-general for async effects) |
| Per-run nonce / `set` step for unique per-run values | 3 (Gitea, Listmonk, WooCommerce) |
| Scope from a URL path regex | 2 (Vikunja, Gitea) |
| Scope from a nested JSON field | 1 (WooCommerce; class-general) |
| Form-encoded bodies | 1 (Gitea; class-general) |
| Entrypoint / args override | 2 (Vikunja, Listmonk) |
| Allow-listed sysctl `net.ipv4.ip_unprivileged_port_start` (non-root :80, no capabilities) | 1 (WooCommerce; class-general for images on ports < 1024) |
| Proxy: preserve the client's Host (bug fix) | browser path of Gitea; class-general |
| Proxy: capture the held request at hold time (bug fix) | browser path of Vikunja; class-general |
| Proxy: additional-write log (AM-3) | all 4 |

Implemented but **not used** by any app: cookie-based scope.

## 6. Predictions vs observations (predictions as recorded in §4 of the protocol)
| Prediction | Observed |
|---|---|
| No app compatible with v0 as-is | Confirmed (0 / 4) |
| Extensions 1–4 unlock the single-service class; 5–6 needed for multi-service and async | Every app turned out to be multi-service once a real datastore was used. All extension groups were needed |
| Gitea success is a 303 PRG; session-bound CSRF token | **Wrong**: 200 JSON `{redirect}`; no token, header-based cross-origin protection |
| WooCommerce delayed commit F0/F1 because of cart state | Partly wrong: a *single* held order commits late (F2). The cart state blocks the *duplicate*, exactly as predicted (native prevention) |
| WooCommerce: correct AFD result is NOT ASSESSED | Confirmed |
| Delayed commit F1 everywhere ("ordering inversion") | Superseded by AM-4: under edge-deferred semantics it is F2 on all 4 |
| Fabricated F1 | Confirmed on all 4 (WooCommerce the most convincing) |
| Browser B1 for Gitea and WooCommerce, B2/B1 for Vikunja | Vikunja B1, Gitea B1, WooCommerce **B2** |
| Listmonk B1 | **Wrong**: B0, the action has no UI |
| Saleor NOT EVALUATED or unsupported | NOT EVALUATED (deliberately deferred) |

## 7. Failed tests and their regression evidence (preserved)
| What failed | How it showed | Evidence | Fix / regression test |
|---|---|---|---|
| Stage 0: v0 on all 4 apps | startup failures / not expressible | `studies/compat/results/*-v0-stage0.json` | contract v1 (generic) |
| **Doc-derived contract (Vikunja)**: the browser's real write is a different route | **no fault applied**; the UI write logged as unclassified; verified 1 vs inferred 0 | `studies/compat/results/regressions/doc_derived_contract.json` | contract data from observed traffic; caught by AM-1 / AM-3 |
| **Proxy keep-alive replay bug** | the proxy "committed late" the browser's token refresh; oracle 0 vs inferred 1 | `regressions/keepalive_replay_bug.json` (reproduced with the fix reverted) | `tests/test_contract.py::test_delayed_replay_survives_keepalive_connection_reuse` (fails on the old code) |
| **Proxy Host rewrite** | Gitea `403 @ verifyAuthWithOptionsWeb` on every browser submit; nothing committed | `regressions/host_rewrite_bug.json` + `.png` | `test_client_host_is_preserved_for_origin_checks` |
| WooCommerce G2/G3b self-test | the extra attempt rejected (409) | final-pass bundle + `woocommerce-measure.json` (AM-2) | none needed: correct protocol outcome |
| Environment and harness issues (fixed, not AFD defects) | Postgres out of space on the 64 MB /tmp; root-owned tmpfs broke WordPress's copy; plain permalinks returned the home page with 200; a native form needs `requestSubmit()`; React selects need the native setter | `*-findings.json` | writable-path ownership (generic); contract data; study-driver steps |

## 8. Fundamental vs fixable limitations
**Fixable (seen or adjacent, all generic):**
- cookie-scoped sessions (implemented, unused);
- GraphQL operation matching (not needed here);
- websockets and HTTP/2 in the proxy (not exercised by these apps).

**Fundamental to an edge proxy (confirmed by this study):**
1. **Native accepted-then-late-commit** (Listmonk) can be *observed* but not *injected* at the edge. The edge proxy
   implements only edge-deferred delivery.
2. **Writes with no read-back** (Listmonk) make verification-based recovery impossible by construction. AFD can report this
   but cannot change it.
3. **App-level duplicate prevention** (WooCommerce) makes identical-resubmission effect safety uninformative (NOT
   ASSESSED). Duplicates through *different* request sequences (re-add, then checkout) are not modelled by the current
   positive control.
4. **The contract must describe the traffic the UI really sends.** Docs-derived contracts silently miss faults. This is
   detected (AM-1 / AM-3), but it costs integration effort.

**Not shown at all:**
- LLM browser agents on these apps;
- remote or production sites (refused by design);
- more than one action per app;
- Saleor / GraphQL;
- apps whose effects are not visible through any HTTP request (cron jobs, inbound webhooks).

## 9. Exploratory (AM-6, not part of the decision)
- **Pre-commit failure** (503 without forwarding): feasible F2 by analysis on all 4 apps (the same client error paths).
- **Stale reads:** feasible F1 for JSON read endpoints (Vikunja, Gitea API, WooCommerce cart); not applicable to Listmonk
  (no read path).

Neither was implemented.

## 10. Reproduce
```bash
python3 studies/compat/stage0.py studies/compat/contracts/vikunja-v0.json      # Stage 0 (likewise gitea-v0)
python3 studies/compat/final_pass.py                                            # core + measure + browser for all 4, reuse proof, unit tests
python3 studies/compat/regressions/run_regressions.py                           # regenerate the three failure cases
```
Requires the upstream images pinned in the contracts, and the WooCommerce 11.2.2 zip and wp-cli.phar in
`studies/compat/vendor/` (gitignored; fetched from downloads.wordpress.org and the wp-cli builds repository, checksums in
`woocommerce-findings.json` / §4).
