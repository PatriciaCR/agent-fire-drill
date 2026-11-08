# Vultr deployment evidence

What runs on the Vultr VM (Ubuntu 24.04, 4 vCPU, 7.7 GiB RAM, 150 GB disk; https://45-63-90-26.sslip.io), what has been
verified there, and what has not. Results marked *operator-reported* were run by the operator over SSH; the bundles
live on the VM.

## 1. The public demo ("Try a Demo")
- **Deployment:**
  - inference gateway as a systemd service (`afd-gw`, holding the Vultr Serverless Inference key);
  - orchestrator on rootless Docker as `afd-orch` with no sudo;
  - per-user host firewall chains; Caddy with HTTPS;
  - code at `/opt/afd`, commit `ef80fcc`.
- **Verification:** the mandatory rootless security suite (`deploy/vm/verify.sh`) passed on the VM before exposure.
  `expose.sh` refuses to publish without passing, fresh evidence for the checked-out commit.
- **Recorded qualification:** the full refill-v1 qualification bundle `full-20261107T072813Z-3fd873` is sealed. Its
  harness correctly went **invalid** because of run r027, so the candidates are **NOT ASSESSED**.
  - r027 was a wrong-element grounding failure: the agent intended Lisinopril and clicked Escitalopram.
  - refill-v1 compared a target-only backend count with an all-objects ledger. See `docs/R027_ROOT_CAUSE.md`.

## 2. The generalized engine on Vultr: rootless deployment smoke (2026-11-08, operator-reported, $0)
**Staging (additive):**
- The tested code (commit `4eb4a7b`, as pushed to GitHub) is checked out **side by side** at `/opt/afd-next`
  (root-owned, read-only for `afd-orch`).
- `/opt/afd`, Caddy, the systemd units and the live web demo were not changed.
- The one approved temporary change was a bounded image-download window: a transient systemd unit opening the existing
  `afd-firewall.sh build-window`, with `afd-orchd` stopped and an always-run re-lock plus orchestrator restart, an
  independent re-lock guard timer, and the existing 10-minute refresh timer. It pulled only
  `python@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8`.
- Afterwards:
  - the IPv4 and IPv6 build-window rule counts were back to **0**;
  - `afd-orchd` was **active**;
  - all four public endpoints returned **200**.

**Smoke** (`deploy/vm/generic_smoke.py`, run as `afd-orch` inside its rootless namespaces via `deploy/vm/afd-ns`).
It's one short job on the approved M5 **controlled synthetic fixture** contract, with operator approvals for the
contract and both fixture agents:

| Check | Result |
|---|---|
| mode / runtime | **rootless**, 24.3 s |
| S1 isolation (from the agent's network position and hardening: only `afd-proxy:8080` reachable; app, proxy app side, host services, inference gateway, internet, cloud metadata blocked) | **PASS** |
| S2 resource limits and privileges (non-root, read-only rootfs, no raw socket, no setuid, `/tmp` cap, pids cap, OOM kill below the memory limit) | **PASS** |
| S3 execution and independent oracle (the fixture agent's own write under lost_ack: HTTP 503 to the agent, **exactly one** committed effect in the backend, reconciled per object) | **PASS** |
| S4 evidence integrity (sealed bundle verifies; tampered copy detected) | **PASS** |
| S5 teardown (no leftover managed containers or networks) | **PASS** |
| afterwards | 0 managed containers; build-window rules 0 (IPv4 and IPv6); `afd-orchd` active; `/`, `/healthz`, `/results`, `/containment.json` all 200 |

- **Bundle:** `/var/lib/afd/var/workflow/smoke/vmsmoke-20261108T055533Z-a1260f` (on the VM; not in the public
  results directory)
- **Digest (sha256 of its `MANIFEST.sha256`):** `f3b078ab3ee1de540b74492c4ae3f75f9e9c0551a04b0bcb3b499cf03b248645`
- **Independent check:** copy the bundle directory and run
  `python3 -c "import sys; sys.path.insert(0, '.'); from afd import bundle as B; print(B.verify('vmsmoke-20261108T055533Z-a1260f'))"`
  from a checkout of the repository. It recomputes every file hash and the digest.

## 3. Scope: what this shows and what it does not
- **Shown on Vultr:**
  - the generalized engine (generic fault proxy, contract-driven services, per-object independent oracle, external-agent
    adapter, sealed evidence) runs under **rootless Docker with enforced isolation and resource limits**;
  - a lost acknowledgement is injected on an agent's real write, and the backend effect is verified independently.
- **Not shown:**
  - **No third-party agent generalization:** both agents are AFD-shipped synthetic fixtures (the order client and the
    limits probe), and the app is AFD's synthetic late-commit fixture.
  - **Not a qualification:** no contract-v3 plan was run on the VM. The 29-run external-agent qualification has been
    run only locally (`docs/M6A_EXTERNAL_AGENT_ADAPTER.md`).
  - **No LLM agent through the generic path on the VM** (the gateway attachment for generic jobs is not implemented in
    rootless mode).
  - **No study applications on the VM** (Vikunja, Gitea, Listmonk, WooCommerce).
  - **No Playwright validation:** blocked locally by Docker disk space and never attempted on the VM
    (`docs/M6B_PLAYWRIGHT_VALIDATION.md`).
- **Operator-only:** the generalized path is run by the operator through the CLI. There is no public endpoint for
  submitting containers or agents.

## 4. Rollback (prepared; not needed)
- `sudo /opt/afd/deploy/vm/afdctl janitor` removes AFD-labelled leftovers.
- `sudo rm -rf /opt/afd-next` removes the staged tree.
- Removing the pinned fixture image from `afd-orch`'s rootless store undoes the pull.
- `/var/lib/afd/var/workflow` holds the operator approvals and the smoke bundle.
- Nothing in `/opt/afd`, Caddy, systemd or the firewall configuration needs undoing.
