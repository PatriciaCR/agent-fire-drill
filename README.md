# Agent Fire Drill

**Qualify a browser agent's recovery policy before you ship it.**

Agent Fire Drill runs your agent (v1: a preset browser-use agent with a recovery policy in its system prompt) in sealed sandboxes against a consequential synthetic workflow, under controlled *ambiguous outcomes*:
- a lost acknowledgment: the write committed, but the client saw an error;
- a delayed commit;
- a fabricated success.

It scores what actually happened in the backend, never what the agent or the UI says. It reports three separate things:
- **effect safety:** duplicate side effects;
- **outcome integrity:** false success claims;
- **completion.**

A deliberately unsafe positive control proves the harness can detect duplicates, so "no duplicates observed" means something.

Agent Fire Drill does **not** claim exactly-once behaviour or general safety. Zero observed duplicates in N runs is reported as exactly that.

## Status
**Built and tested (local Docker):** images, inference gateway, orchestrator, host firewall, refill workflow, containment suite.

**Not built yet:** qualification engine (matrix, verdict, evidence bundle), web UI, Vultr deployment. Appointment booking is gated on validation.

## Architecture
```
afd-web (planned)  ── no Docker, no key
afd orchestrator   ── Docker access, no key; per-job networks, containers, tokens, janitor, host firewall
inference gateway  ── the ONLY holder of the Vultr key; per-job tokens, model allow-list, token caps, budget, usage ledger
per job:   site (BreakingWeb + our interventions/effect ledger) ─ browser (Chromium/CDP) ─ agent worker (browser-use) ─ gateway
```
- Each pair is joined by its own two-member internal Docker network.
- The host firewall (`afd/firewall.py`) blocks container access to the host and to cloud metadata, and limits the gateway to Vultr inference on TCP 443.

## Build and test
```bash
make images                                   # fetches BreakingWeb at the pinned commit, builds afd/{site,browser,worker,gateway}:1
python3 -m unittest tests.test_gateway        # gateway tests against a fake upstream (no cost)
python3 tests/acceptance/containment.py       # containment suite (no inference cost; needs sudo for the firewall and key checks)
python3 scripts/smoke_refill.py clean         # one real episode through the gateway (~$0.004)
```
The key is installed once by an admin into `/var/lib/afd/secrets/vultr_key` (0400, gateway uid). See `afd/gateway.py:install_key`.

## Credits
The synthetic patient portal, its seeded sessions, task definitions, backend success evaluation and fabricated-success variant are from **BreakingWeb** (Yin et al., arXiv:2609.35814; Apache-2.0), used unmodified at a pinned commit. See [NOTICE](NOTICE) and [PROVENANCE.md](PROVENANCE.md).

Agents run on [browser-use](https://github.com/browser-use/browser-use) (MIT). Inference is provided by Vultr Serverless Inference.

## License
Apache-2.0.
