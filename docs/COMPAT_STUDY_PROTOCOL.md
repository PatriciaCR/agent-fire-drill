# Adversarial compatibility study: pre-registration (2026-11-07)

**Status:** pre-registered before any integration work. Frozen at the commit that adds this file. Results are reported
against these rules. Any amendment is recorded below as a dated amendment *before* the affected app is evaluated, and the
original rule stays visible.

**Question:** can Agent Fire Drill's current generic layer support a consequential action in independently developed,
real open-source applications that were not built for it, without app-specific AFD code or app source changes? The
generic layer is the fault proxy (`afd/faults/proxy.py`), contract v0 (`afd/contract.py`), the effect oracle and the
qualification engine (`afd/qualify.py`).

## 1. Applications (independently developed; unmodified upstream images, pinned by digest before use)
| # | Application | Architecture class | Consequential action | Why it is adversarial for us |
|---|---|---|---|---|
| A | **Gitea** (Go; MIT) | Server-rendered forms, session cookie, CSRF token, POST-redirect-GET | Create an issue through the web form | Form-encoded bodies, scope in the URL path, success is a 303 redirect, login session, an absolute ROOT_URL |
| B | **Vikunja** (Go API + Vue SPA; AGPL-3.0) | REST-backed single-page app, bearer-token auth | Create a task (`PUT /api/v1/projects/{id}/tasks`) | Token in a header set by JavaScript, scope in the path, PUT not POST, SPA error handling |
| C | **WordPress + WooCommerce** (PHP + MariaDB; GPL) | Cookie session, nonces, AJAX checkout inside a server-rendered site, two services | Place an order with an offline payment method | Cookie-scoped cart, nonce CSRF, canonical-URL redirects, multi-container, likely app-level duplicate protection (the cart empties after an order) |
| D | **Listmonk + Mailpit** (Go + Postgres; AGPL-3.0 / MIT) | Asynchronous effect via a background sender, three services | Send a transactional message (`POST /api/tx`) | The effect (an email) happens out of band. The oracle is an external sink (Mailpit), not the app |
| E | **Saleor** (Python GraphQL API + Postgres + Redis + worker; BSD-3) | GraphQL single endpoint, multi-service, async events | `checkoutComplete` mutation (creates an order) | One endpoint for every mutation; the action is identified by the body. Likely built-in idempotency. The heaviest stack: if this environment cannot host it, it is recorded as **NOT EVALUATED**, never as a pass |

**Excluded on purpose:**
- anything we wrote (the expense demo and BreakingWeb are not part of this study);
- apps that need real payment providers, external SaaS or real email delivery;
- apps whose licences forbid this use.

**Allowed preparation:**
- app configuration (environment variables, config files);
- seed data and users created through each app's own CLI, admin API or setup wizard;
- disposable database snapshots;
- external adapters and sidecars.

**Not allowed:**
- modifying application source or images;
- app-specific branches in generic AFD modules (enforced by the existing no-identifier test, extended to these apps).

## 2. What is measured, per application
| Dimension | Measurement | Classes (fixed now) |
|---|---|---|
| **D1 Outcome** | Can the app run the full contract-v0 pipeline: self-test, positive control, scripted candidates, verdict, sealed bundle? | **COMPATIBLE**: contract + app config only, current generic code. **COMPATIBLE-WITH-EXTENSION**: works after *generic* changes (each listed, each usable by any app). **UNSUPPORTED**: needs app-specific AFD code, app source changes, or fails the fidelity/oracle floors below. **NOT EVALUATED**: could not be hosted (reason recorded) |
| **D2 Fault fidelity** (per primitive) | Self-test G1–G5 analogue through the proxy, plus a scripted browser check that the client actually experiences the fault | **F2 faithful**: one commit per forwarded write, and the client sees the failure through the app's normal error handling. **F1 approximate**: works with a documented deviation (e.g. ordering inversion for delayed commit, or a synthetic fabricated response the app's client does not render naturally). **F0 infeasible** |
| **D3 Oracle independence** | Where backend truth comes from, cross-checked against the proxy ledger on every run, and against a direct datastore count on ≥ 5 scenarios | **L3** external sink (e.g. SMTP capture) · **L2** read-only datastore query with separate credentials · **L1** app API with separate admin credentials · **L0** the action's own response channel (**not acceptable**) |
| **D4 Isolation** | Fresh state per run (admin-API-created scope or snapshot restore), reset time, and a contamination check (run k's effects never appear in run k+1) | pass / fail, plus the reset method and time |
| **D5 Effort** | Contract lines; app config changes; seed steps; generic AFD lines added; **app-specific AFD lines (must be 0)**; **app source changes (must be 0)** | counted, never estimated |
| **D6 Browser execution** | A deterministic, scripted Chromium (CDP, no LLM, $0) completes the action through the proxy and shows the injected fault in the UI | **B2** works with current proxy · **B1** works after generic proxy features (listed) · **B0** infeasible. LLM-agent runs only later, with approval |
| **D7 Engine reuse** | Same `plan`/`outcome`/`verdict`; `verdict.json` recomputes; bundle verifies; positive control (scripted blind retry) yields a duplicate under lost_ack, **or** the app's own duplicate protection is demonstrated and the harness correctly reports NOT ASSESSED | pass / fail |
| **D8 Containment** | All services on internal networks, no egress; the agent can reach the app only through the proxy; the agent cannot reach the oracle or the proxy control API | pass / fail (fail = UNSUPPORTED until fixed) |

**Floors for COMPATIBLE or COMPATIBLE-WITH-EXTENSION:**
- lost_ack at **F2**;
- an oracle at **L1 or higher**;
- D4, D7 and D8 pass;
- **zero** app-specific AFD lines;
- **zero** app source changes.

**Stop-loss rule:** the moment progress on an app would require app-specific AFD code or an app source change, work on that app stops and it is recorded as UNSUPPORTED, with the reason. We do not keep bending the abstraction to fit.

## 3. Decision rule (fixed now)
- **GO** (invest in "Test Your Workflow"): all of the following.
  - At least **3 of the 4 required apps (A–D)** reach COMPATIBLE or COMPATIBLE-WITH-EXTENSION.
  - The union of generic extensions is a **bounded set**: each extension is used by at least 2 apps, or is clearly class-general (e.g. form-encoded bodies).
  - lost_ack is **F2** on at least 3 apps.
- **NARROW GO:** apps work only with per-app glue code. Then the product is "contract + adapter SDK", and claims are limited to apps that have an adapter.
- **NO-GO** (the abstraction is too narrow): any of the following.
  - At most 1 required app reaches the compatible classes.
  - lost_ack cannot be injected faithfully (F2) at the HTTP boundary on at least 3 apps.
  - Trustworthy oracles require app source changes.

Saleor (E) informs the GraphQL and multi-service conclusions, but does not count towards the threshold.

**Claims allowed afterwards:**
- "compatible with these N applications under these conditions";
- the classes those apps represent, with the extensions listed.

**Never:** "arbitrary sites" or "arbitrary agents".

## 4. Predictions (recorded now, compared with results later)
**Gaps in v0 that I expect to hit** (all generic, none app-specific):
1. **Request matching and scope extraction:**
   - JSON body only today; needed: form-urlencoded/multipart fields, URL path captures, cookies, GraphQL `operationName`;
   - A, B, C, E.
2. **Effect success classification:** today 2xx only; needed: configurable status sets including 3xx (PRG) and response predicates. A, C, E.
3. **Scenario provisioning:** today a single "create scenario" HTTP call that real apps don't have; needed: a sequence of admin-API calls, or a disposable snapshot restore per run. All.
4. **Session bootstrap:** today none; needed: a scripted login (API or form) producing agent-only cookies or tokens for synthetic users, kept out of evidence. A, B, C, D.
5. **Multi-service apps:** today one image; needed: a compose-like service list on the sealed app network, plus an oracle sidecar for read-only datastore queries. C, D, E.
6. **Asynchronous effects:** today an immediate count; needed: an oracle settle/quiescence policy and external sinks. D, E.
7. **Browser path:** reverse-proxy completeness (Host/origin handling, absolute-URL redirects via app base-URL config, websockets, multiple origins). A, C, E.

**Per-app predictions:**

| App | Predicted D1 | lost_ack | delayed_commit | fabricated | Oracle | Browser | Main risk |
|---|---|---|---|---|---|---|---|
| A Gitea | COMPATIBLE-WITH-EXTENSION (1–4) | F2 | F1 (ordering) | F1 (synthetic redirect) | L1 API / L2 SQLite | B1 | ROOT_URL redirects; CSRF token bound to session |
| B Vikunja | COMPATIBLE-WITH-EXTENSION (1, 3, 4) | F2 | F1 | F1 | L1 / L2 | B2 or B1 | SPA error handling may hide the 503 |
| C WooCommerce | COMPATIBLE-WITH-EXTENSION (1–5) at best; possibly UNSUPPORTED on fidelity | F2 | **F0 or F1** (the replay may hit an emptied cart or a nonce issue) | F1 | L1 REST / L2 SQL | B1 | **App-level duplicate protection** (cart emptied): the positive control may not duplicate, so the expected AFD result is a correct NOT ASSESSED |
| D Listmonk + Mailpit | COMPATIBLE-WITH-EXTENSION (3–6) | F2 | F1 | F1 | **L3** | B1 | Async send timing; settle policy |
| E Saleor | NOT EVALUATED, or UNSUPPORTED for effect safety | F2 (mechanics) | F1 | F1 | L1 / L2 | B1 | `checkoutComplete` likely idempotent; heavy stack |

**Predicted conclusion:**
- **GO**, conditional on extensions 1–4, which unlock the largest class: single-service web apps with forms, SPAs or cookie sessions.
- Extensions 5–6 are needed for multi-service and async workflows.
- No-go areas I expect to remain:
  - effects not caused by an HTTP request the proxy can see (cron jobs, timers, inbound webhooks);
  - true commit-after-timeout inside the app's own transaction, which the edge proxy can only approximate;
  - apps whose built-in idempotency makes effect-safety qualification uninformative. These are correctly reported as NOT ASSESSED, not as passes.

## 5. Execution plan and constraints
- **Order:** B (Vikunja) → A (Gitea) → D (Listmonk) → C (WooCommerce) → E (Saleor, stretch).
- **Cost:** $0. Scripted HTTP policies and scripted CDP browser checks only. LLM browser-agent runs only after separate approval.
- **Where:** local sandbox Docker. **No VM changes. The running 46-run qualification is untouched.**
- **Before each app:**
  - pin the image digest;
  - record the licence;
  - confirm no egress (internal networks);
  - seed synthetic data only, through the app's own interfaces.
- **Pulling the upstream images** may need sandbox network approval for registry hosts. If a pull is blocked, I will ask rather than work around it.
- **Outputs:** per-app evidence bundles, the D1–D8 table with raw measurements, a list of every generic change with lines of code, a list of every app-specific workaround, and the GO / NARROW GO / NO-GO decision under §3.

## Amendments
(none)
