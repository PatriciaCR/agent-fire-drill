# Security model and verified properties

## Trust boundaries
| Component | Trust | Holds | Must not have |
|---|---|---|---|
| Agent worker (per run) | **untrusted** (page content, LLM-chosen actions) | a per-job gateway token | inference key, Docker socket, host files, internet |
| Browser (per run) | untrusted | — | secrets, internet, host access |
| Site (per job) | trusted code, synthetic data | per-job controller secret | internet, host access |
| Inference gateway | trusted | the Vultr key (0400, gateway uid) | Docker access; any egress except Vultr inference |
| Orchestrator | trusted (TCB) | Docker access | the key (see limitation L1) |

## Verified by `tests/acceptance/containment.py`
Result: 22/22 PASS on 2026-11-07, local Docker (`evidence/acceptance/containment-2026-11-07.json`).
- **Key isolation:** the orchestrator user cannot read the key file. The key is absent from the worker's environment and readable files (7 MB scanned, compared on the admin side).
- **Worker:**
  - **Blocked:** no Docker socket, no internet, no direct inference, no cloud metadata, no host ports, no direct site access.
  - **Allowed:** it reaches only the gateway (401 without a token, 403 for a disallowed model) and its browser's CDP.
  - **Identity:** uid 10002, CapEff 0, NoNewPrivs, read-only root.
  - **Limits enforced:** a 3 GiB allocation is OOM-killed; a fork bomb is stopped at the PID limit.
- **Browser:** reaches only the site; uid 65534; CapEff 0; no secrets in its environment.
- **Gateway:** egress only to the resolved Vultr inference address on 443 (1.1.1.1:443, metadata and 8.8.8.8:53 blocked); uid 10001; CapEff 0.
- **Lifecycle:**
  - job teardown removes all containers, networks and the token, and verifies removal;
  - a revoked token is rejected (401);
  - after a SIGKILLed orchestrator, the janitor removes the orphaned site container and networks.
- **Gateway unit tests** (`tests/test_gateway.py`, 7/7):
  - token auth and revocation;
  - model, streaming and size limits;
  - the key is sent only upstream, and client headers are dropped;
  - output tokens clamped;
  - usage metered on every response, including truncated ones;
  - responses without usage and transport failures charged at the worst case;
  - budget enforced before any upstream call;
  - the key never appears in logs or the ledger.

## Assumptions that failed in practice (and the fixes)
1. **"Docker internal networks isolate containers from the host": false.**
   - With the host firewall removed, the worker reached a host service on two of its bridge addresses (diagnostic in the containment evidence).
   - **Fix:** host firewall chains `AFD-IN` / `AFD-FWD` are mandatory.
2. **"chown makes the key unreadable to the orchestrator": false on host-shared mounts.**
   - The project directory (a host-shared mount) silently ignored ownership changes.
   - **Fix:** gateway state lives in `/var/lib/afd` on a native filesystem. The misplaced copy was shredded; it was never committed.
3. **"The browser is ready when its container is running": false.**
   - Chromium under resource limits took about 10.6 s to open CDP.
   - **Fix:** the orchestrator waits for CDP readiness before starting the worker.

## Known limitations (to address in deployment)
- **L1, orchestrator vs key.** Anyone with access to the Docker daemon can mount any host file into a container, so the orchestrator could read the key indirectly. The "orchestrator never reads the key" property is therefore a code property, not an OS-enforced one, while they share one Docker daemon. **Options for the VM:**
  - (a) run the gateway as a systemd service under its own user, outside the orchestrator's Docker daemon, with the orchestrator on rootless Docker; or
  - (b) accept the orchestrator as part of the TCB and keep it minimal and non-public.

  The web front end never has Docker access either way.
- **L2, sudo in the dev sandbox.** Here the orchestrator user has passwordless sudo, which defeats any file-permission boundary. On the VM, firewall installation and key installation are separate admin (root) steps, and the orchestrator user has no sudo.
- **L3, gVisor** is not used (runc). Containers share the host kernel.
- **L4, tokens.** Token files are world-readable on the host (0644), so local host users could read live job tokens. A token only allows budget-capped calls to allow-listed models through the gateway, and it is revoked at job end.
- **L5, the browser image** pins Debian trixie and Chromium only by package version at build time (recorded in the image), not by digest.
