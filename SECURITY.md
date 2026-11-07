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
Result: 21/21 checks PASS on 2026-11-07, local Docker (`evidence/acceptance/containment-2026-11-07.json`). The file also holds one diagnostic
record (`diagnostic_without_firewall`), which is not a check; earlier text miscounted it as a 22nd check.
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

## Deployment architecture (VM): OS-enforced key boundary
`deploy/` implements the rootless layout, option (a) of L1. See [deploy/DEPLOY.md](deploy/DEPLOY.md).
- The gateway is a system service under its own user, receiving the key through systemd `LoadCredential`.
- The orchestrator is an unprivileged user with rootless Docker, no sudo and no rootful socket.
- A root-owned per-uid host firewall is the hard egress boundary.

**Status.** The scripts and the security suite are written. The rootless code paths are exercised in a development emulation (`deploy/dev/`). **None of it is verified on the VM yet.**

**Resource limits under rootless Docker are UNVERIFIED.** They depend on systemd cgroup delegation. The development sandbox has no systemd, and there rootless Docker silently ignores `--memory`, `--pids-limit` and `--cpus`:
- a 64 MB limit allowed a 300 MB allocation;
- a 32-PID limit allowed 200 forks.

The suite has four limit checks: cgroup values inside the containers, OOM kill, PID limit, and CPU quota. They are mandatory, and nothing is exposed publicly until they pass on the VM.

Verified in the sandbox while designing the rootless layout (2026-11-07):
- A root-owned `OUTPUT -m owner --uid-owner afd-orch` chain confines everything the rootless daemon sends through slirp4netns. Only the gateway port was reachable; another port on the same host address and the internet were not.
- A first draft with an `-o lo ACCEPT` rule was wrong: traffic to the host's own addresses travels over loopback, so that rule let every local port through. It was removed. The suite probes local ports to catch this class of mistake.
- Files the orchestrator creates inside its user namespace belong to afd-orch on the host. A setgid token directory gives the gateway group read access with 0640 files, which fixes L4 on the VM.

## Known limitations
- **L1, orchestrator vs key.**
  - **Development (rootful):** anyone with access to the Docker daemon can mount any host file. The "orchestrator never reads the key" property is a code property there.
  - **VM:** closed by the rootless layout above, pending VM verification.
  - The web front end never has Docker access in either mode.
- **L2, sudo in the dev sandbox.** The development user has passwordless sudo. On the VM, `afd-orch`, `afd-gw` and `afd-web` have no sudo; only the admin does.
- **L3, gVisor** is not used (runc). Containers share the host kernel. Rootless mode adds a user namespace: container root is an unprivileged host uid.
- **L4, tokens.** In development, token files are 0644. On the VM they are 0640 in a setgid directory (afd-orch:afd-gw), so only the orchestrator and the gateway can read them.
- **L5, the browser image** pins Debian trixie and Chromium only by package version at build time (recorded in the image), not by digest.
- **L6, gateway DNS.** The gateway user may query the configured resolvers, which is needed to resolve the inference host. A compromised gateway could tunnel data over DNS. The gateway is trusted, holds no data beyond the key and usage, and never logs.
- **L7, build window.** `build-images.sh` temporarily lets afd-orch reach TCP 80/443 and DNS to fetch base images and packages. It refuses to run while any job container exists, stops the orchestrator, and re-locks the firewall on exit.
