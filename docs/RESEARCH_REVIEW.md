# Research review: fault injection and ambiguous-outcome recovery (2026-11-07)

Scope: BreakingWeb, LIMBO, AgentChaos, WAREX, Toxiproxy/Envoy, Filibuster, idempotency, metamorphic testing, Jepsen and
recent recovery work, read against AFD's code and the pre-registered compatibility study. **[V]** = verified in a primary
source during this review. **[I]** = my engineering inference.

## 1. Sources and what they establish
| Work | Verified findings |
|---|---|
| **LIMBO**: *Where Does Exactly-Once Live?* ([arXiv 2609.29095](https://arxiv.org/abs/2609.29095), [code](https://github.com/jaxblack/limbo-bench/releases/tag/v1.0)) | **[V]** Six simulated services, faults injected "at the service boundary", graded only from "the ground-truth effect ledger". **[V]** Twelve fault modes: timeout_pre / post / late / late_tail, http500_pre / post, partial_timeout, duplicate_delivery, http503_transient, rate_limit, outage, schema_drift. **[V] Proposition 1:** "no verification-only policy is exactly-once under late commits without a bound on in-flight time". A known bound Δ restores safety at a latency cost of Δ+λ. **[V]** When a read-back can resolve the fault, the model matters: frontier models duplicate 0.5% of lost-ack episodes. When it cannot (late commit, redelivery), the contract matters: 56–74% duplicates, and keys on every write cut duplicates from 28% to 4%. "The harness barely matters"; transparent client retries hurt. **[V]** Reads lag by λ (120–180 s on some services); agents measured the wait from the error rather than from the commit; a lag-blind read path was "worse than no read-back path at all". **[V]** Limits: simulated services; tool / MCP agents; no browser agents or real applications |
| **BreakingWeb** ([arXiv 2609.35814](https://arxiv.org/abs/2609.35814)) | **[V]** 519 task pairs on 7 self-hosted sites. Interventions on content, server state, network responses or the rendered page. Scored on final backend state. 75% of agent failures are "belief failures" (finished, but the change was not made). **[V]** The abstract says nothing about duplicate effects or retries |
| **AgentChaos** ([arXiv 2608.06790](https://arxiv.org/abs/2608.06790)) | **[V]** Non-intrusive injection at the **LLM-API HTTP layer** (crash, omission and value faults in model responses). It does not target tool or environment side effects |
| **WAREX** ([arXiv 2510.03285](https://arxiv.org/html/2510.03285v1)) | **[V]** A mitmproxy addon on existing web-agent benchmarks: delays, error pages, HTTP 408/429/500/502/503, JS 504. Measures success, cost and reloads. **Does not measure duplicate requests**, and does not state whether faulted requests reach the server |
| **Toxiproxy** ([repo](https://github.com/Shopify/toxiproxy)) | **[V]** A TCP proxy: latency, bandwidth, timeout, reset_peer, slicer, limit_data, packet loss. No HTTP awareness: it cannot target a route or a request |
| **Envoy fault filter** ([docs source](https://github.com/envoyproxy/envoy/blob/main/docs/root/configuration/http/http_filters/fault_filter.rst)) | **[V]** Injects delays and aborts with chosen codes, plus response throughput limits. Selectable by header or percentage; the docs warn the control headers are dangerous for untrusted clients. **The docs do not say that an aborted request is forwarded first**, so "commit, then error" (our lost-ack) is not a documented built-in |
| **Filibuster / SFIT** ([SoCC '21](https://people.eecs.berkeley.edu/~rohanpadhye/files/filibuster-socc21.pdf)) | **[V]** Systematically explores inter-service failures (timeouts, unavailability, standard error responses). It needs instrumented RPC/HTTP client libraries and **intercepts before the request reaches the service** (pre-commit failures) |
| **Evidence-guided recovery** ([arXiv 2610.41453](https://arxiv.org/html/2610.41453)) | **[V]** Track every latent state consistent with what has been observed. "A negative read does not justify a non-idempotent retry if an earlier request may still commit". Idempotency keys have validity windows; certify completion across all compatible states. Uses a *separate private effect ledger* for adjudication, never as controller input. Synthetic workloads |
| **Idempotency-Key header** ([IETF draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)) | **[V]** Defines a client-supplied key for POST/PATCH, unique per request payload. Still an Internet-Draft (work in progress); check its status before citing it as a standard |
| **Jepsen / Knossos** ([Aphyr](https://aphyr.com/posts/314-computational-techniques-in-knossos), [docs](https://cljdoc.org/d/io.jepsen/generator/0.1.1/doc/nemeses)) | **[V]** Operations whose outcome is unknown are recorded as indeterminate (`:info`). Checking cost explodes as they accumulate; the remedy is many short, independent histories |
| **Metamorphic testing** ([ACM CSUR 51(1) 2018](https://i.cs.hku.hk/~tse/Papers/2010s/hlmtCSUR.html)) | **[V]** Metamorphic relations address the oracle problem: check relations between executions instead of exact expected outputs |

## 2. Answers
**Q1. Injecting failures without modifying source code.** Four layers exist:
- **network (TCP):** Toxiproxy;
- **HTTP edge or proxy:** Envoy, mitmproxy (WAREX), AFD's proxy;
- **client library:** Filibuster;
- **LLM API:** AgentChaos.

Benchmarks like LIMBO and BreakingWeb control the environment they inject into. **[I]** Only a proxy that understands HTTP and **forwards before failing** can create "committed, but the client saw an error" without app changes. Of the tools above, only programmable proxies (mitmproxy, or ours) do this without custom code.

**Q2. What generalises at the HTTP boundary.**
- **Generalisable [I]:**
  - pre-commit failure (error or timeout without forwarding);
  - post-commit failure (lost ack: forward, then error or hang);
  - late commit (hold, then forward later; this is LIMBO's definition of late commit **[V]**);
  - transient 503 / 429 / outage;
  - redelivery (forward twice);
  - fabricated success (respond without forwarding);
  - **stale reads** (serve a cached read response for λ after a write; needs the read routes declared).
- **Needs app-level access [I]:**
  - commits that happen after a timeout *inside* the app's own transaction;
  - effects with no HTTP request we can see (jobs, cron, inbound webhooks, email);
  - partial batches within one request;
  - schema drift of internal data.

**Q3. Recovering safely from ambiguous outcomes.**
- **[V]** Read-back resolves lost acks only. For late commits, verification alone cannot be exactly-once without a bound on in-flight time (LIMBO Prop. 1).
- **[V]** Idempotency keys are the decisive control.
- **[V]** Stale reads mislead; a negative read never licenses a non-idempotent retry while an earlier request may still commit (2610.41453).
- **[V]** Indeterminate outcomes should be modelled explicitly (Jepsen `:info`).
- **[V]** Overclaiming is pervasive: 90% of LIMBO's duplicate episodes, and BreakingWeb's belief failures.

**Q4. Are our abstractions sound?**
- **Edge proxy:** consistent with LIMBO's service-boundary injection **[V]**. Sound.
- **Independent effect oracle and ledger:** consistent with LIMBO's ground-truth ledger and BreakingWeb's backend-state scoring **[V]**. Sound; we additionally cross-check two sources.
- **Contract:** sound in shape, but it **models too little of what decides the outcome.** LIMBO shows the contract's capabilities (idempotency keys, an in-flight bound Δ, read lag λ) explain most of the variance on faults a read-back cannot resolve **[V]**; our contract declares none of them **[I]**.
- **Proxy implementation:** adequate for our sealed HTTP/1.1 apps, but not a complete reverse proxy (no HTTP/2, websockets or streaming; buffered bodies; Host rewritten) **[I]**. mitmproxy is the established alternative for the browser path.

**Q5. What differentiates AFD.**
- **The others [V]:**
  - LIMBO: simulated services and tool-calling agents.
  - BreakingWeb: measures task success and belief failures, not duplicates.
  - WAREX: success rates, not duplicates.
  - AgentChaos: LLM-API faults.
  - Envoy, Toxiproxy, Filibuster: no agent or effect semantics.
- **AFD's defensible niche [I]:** effect-level qualification of *browser-agent* recovery on *real, sandboxed web workflows*, with a positive control, sealed evidence and containment. That means LIMBO-style questions on real UIs, where read-back is UI-based and possibly stale, and nobody has published that.
- **The science is LIMBO's [I]:** our contribution is operational, not theoretical, and must cite it.
- **The strongest product extension [I]:** qualify the *workflow*, meaning agent policy × app contract × fault regime, and recommend contract changes such as adding idempotency keys, not only agent prompts.

**Q6. Assumptions most likely to fail on real apps [I]** (ordered by expected frequency):
1. One consequential action = one identifiable HTTP request (GraphQL, multi-step checkout, websockets).
2. Effects are visible synchronously (queues, read lag λ).
3. Scope travels in the request (cookie or server-side sessions).
4. 2xx means committed (303 PRG; 202 Accepted).
5. A held request can be replayed later (CSRF, nonce or session expiry).
6. Fresh isolated state can be created through one endpoint (real apps need snapshots).
7. The app is not idempotent. If it deduplicates, the positive control cannot create a duplicate and the harness reports NOT ASSESSED: correct, but uninformative.
8. The browser path is single-origin HTTP/1.1 without service-worker caching.

## 3. Our code, read against these findings
| Component | Status | Finding |
|---|---|---|
| `protocols/refill-v1.json` | frozen; qualification running | **[I]** Its single delayed-commit delay (D = 45 s) sits inside the cautious policy's patience (3 × 30 s), so it tests only the *bounded* regime where verification can succeed. Results must not be read as covering late commits beyond the agent's wait (LIMBO Prop. 1). **[I]** It has **no pre-commit failure condition**: a policy that never retries is not challenged on completion when the first write really failed. Both points belong in a new protocol id (refill-v2); v1 stays as is |
| `afd/faults/proxy.py` | tested | Has lost-ack, late commit and fabricated. **Missing generic primitives:** pre-commit failure, hang-then-drop (timeout rather than 503), stale read, redelivery. Effects count as 2xx only. Single-origin HTTP/1.1, buffered |
| `afd/contract.py` (v0) | tested | Missing capability declarations: idempotency-key support, commit bound Δ, read lag λ, compensation. Without them a verdict cannot say *why* a policy can or cannot be safe |
| Oracle | sound | Two sources: proxy ledger and app verifier. **[I]** Could add (a) a metamorphic state-diff oracle (clean run vs faulted run on identical snapshots, normalised), reducing developer burden; (b) an "unrequested writes" count (all non-GET requests through the proxy), which LIMBO reports as collateral damage |
| Scorer (`qualify.py`) | sound | Matches LIMBO's metrics in substance: duplicates, and overclaiming as outcome integrity. Could also *report* LIMBO's composite "exactly-once success" (completed and no duplicate) without changing the axes |
| Compatibility protocol | pre-registered | Measures what matters. Gaps relative to the literature are listed below as proposed amendments. One of my predictions (delayed commit = F1 "ordering inversion") is weaker than I wrote: holding a request matches LIMBO's own definition of late commit |

## 4. Recommendation: keep the architecture; modify specific components
The literature supports AFD's core design: boundary injection, an independent ground-truth effect ledger, backend-state scoring, and paired clean and faulted runs. Nothing found argues for a different approach. Changes worth making, in priority order:
1. **Protocol v2** (new id, after the running v1 qualification finishes; v1 unchanged):
   - add a **pre-commit failure** condition;
   - sweep the **late-commit delay** across the policy's patience, including beyond it, and report the in-flight bound;
   - add a **stale-read** condition;
   - report exactly-once success and unrequested writes.
2. **Contract v1 capability fields:** idempotency keys (and validity window), Δ, λ, compensation. Verdicts then explain which regime applies.
3. **Proxy primitives:** pre-commit failure, hang, stale read and redelivery. Configurable success statuses; matchers for form, path, cookie and GraphQL (already predicted in the study).
4. **Proxy transport, decided by evidence:** if the compatibility study's browser path fails on HTTP/2, websockets or streaming, re-host the primitives as a **mitmproxy addon** rather than growing our proxy into a full reverse proxy. Trade-off: maturity and protocol coverage against a larger, less auditable dependency in the trusted path. Don't switch pre-emptively.
5. **Product positioning:**
   - cite LIMBO and present AFD as the browser- and real-application counterpart;
   - add contract recommendations (e.g. "this endpoint needs an Idempotency-Key") to the evidence report;
   - no claims of theoretical novelty.

**Reconsider the approach** only if the compatibility study reaches its pre-registered NO-GO.

## 5. Proposed amendments to the compatibility protocol (not applied; need approval before any app is evaluated)
| ID | Proposal | Effect on pass floors |
|---|---|---|
| PA-1 | Also measure **pre-commit failure** and **stale-read** feasibility per app (F2 / F1 / F0) | Additive. Floors unchanged |
| PA-2 | Record each app's **read lag λ** (user check and oracle), **native idempotency-key support**, and any **in-flight bound** | Additive. Descriptive only |
| PA-3 | Record **unrequested writes** (non-GET requests other than the watched action) in every run | Additive |
| PA-4 | Note in §4 that the delayed-commit prediction was weaker than written (LIMBO defines late commit as delayed execution). The prediction is kept as recorded; the rule is unchanged | None (a note on a prediction, not a rule) |
