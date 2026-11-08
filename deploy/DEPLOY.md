# Deploying Agent Fire Drill on a VM (Ubuntu 24.04)

The VM layout makes the key boundary **OS-enforced**, closing limitation L1 of the development setup:

| User | Runs | Can | Cannot |
|---|---|---|---|
| `afd-gw` | `afd-gateway.service` (system unit) | use the key, which it receives from systemd `LoadCredential`; reach Vultr inference on TCP 443 | read `/etc/afd/vultr_key`; reach anything else |
| `afd-orch` | rootless Docker (user unit) + `afd-orchd` inside its namespaces | create containers in its own user namespace; reach the gateway address | sudo; the rootful Docker socket (disabled and masked); read the key or the gateway process; send any packet except to the gateway address and port |
| `afd-web` | `afd-web.service` on 127.0.0.1 | read sealed bundles and containment evidence; drop schema-checked smoke requests | Docker; the key; any outbound connection |
| root | `afd-firewall.service` | install the per-user egress rules (before the rootless daemon can start) | — |

Rootless Docker cannot mount the key file: root inside afd-orch's user namespace is afd-orch on the host, so even `--privileged -v /:/host` can only read what afd-orch can read. The security suite tests exactly that.

## What I need from you (nothing leaves your Mac)
- **SSH:** none. Your private key stays on your Mac, and you run every command below in your own SSH session. Paste the output of `verify.sh` back to me.
- **Code:** the VM clones the public GitHub repository, so the commits must be pushed first (see the GitHub section of the hand-off).
- **Key:** you paste it once into `install-key.sh` (hidden input) on the VM. It is stored as `/etc/afd/vultr_key`, root:root 0400.

## Steps (as `linuxuser`, with sudo)
```bash
ssh -i ~/.ssh/agent_fire_drill linuxuser@45.63.90.26
sudo git clone https://github.com/PatriciaCR/agent-fire-drill.git /opt/afd
sudo git -C /opt/afd checkout <commit I give you>
sudo /opt/afd/deploy/vm/install.sh          # packages, users, firewall, systemd units, rootless Docker
sudo /opt/afd/deploy/vm/install-key.sh      # paste the Vultr inference key (hidden)
sudo /opt/afd/deploy/vm/build-images.sh     # ~15-25 min; opens a build-only egress window, then re-locks
sudo /opt/afd/deploy/vm/verify.sh | tee ~/verify.txt   # mandatory security suite, $0 inference
```
Send me `~/verify.txt`, plus `sudo cat /var/lib/afd/acceptance/latest.json` if anything fails.

Only after `verify.sh` reports `ROOTLESS SUITE (vm): PASS`:
```bash
sudo /opt/afd/deploy/vm/afdctl set-access-code   # prints the smoke-run access code once
sudo /opt/afd/deploy/vm/expose.sh                # Caddy + HTTPS on <ip>.sslip.io; refuses without passing evidence
```
`expose.sh` refuses unless the latest evidence (a) comes from `environment: vm`, (b) is for the checked-out commit, (c) is less than 24 hours old, and (d) has every mandatory check PASS, with none skipped. Withdraw at any time with `sudo /opt/afd/deploy/vm/expose.sh --withdraw`.

## Mandatory checks (`tests/acceptance/rootless_suite.py` + `containment_rootless.py`)
- **Privilege:**
  - afd-orch has no sudo;
  - no service user is in a privileged group;
  - the rootful Docker daemon is disabled and its socket is absent;
  - the only container daemon is rootless and owned by afd-orch;
  - cgroup driver systemd v2, with cpu, memory and pids delegated.
- **Key custody:**
  - the key file is root:root 0400 and unreadable by every service user;
  - the gateway's credential is unreadable by afd-orch and afd-web;
  - afd-orch's Docker cannot obtain the key (bind mount, `--privileged` + host root, credentials directory);
  - afd-orch cannot read the gateway's `/proc/<pid>/environ` or memory;
  - the key appears in no file under the repository, state, tmp or logs, and not in the journal.
- **Host egress (the hard boundary):**
  - afd-orch reaches only the gateway address and port (IPv4 and IPv6);
  - afd-gw reaches only the inference address on 443;
  - afd-web reaches nothing;
  - the gateway is not reachable on host interfaces;
  - no public listeners except 22, 80 and 443;
  - the firewall is required before the rootless daemon at boot.
- **Containers:**
  - the worker has no Docker or other Unix sockets, no internet, no direct inference, no metadata, no host ports, and no direct site access;
  - the worker reaches the gateway (401 without a token, 403 for a disallowed model) and its browser's CDP;
  - the browser reaches only the site;
  - identities: uid, CapEff 0, NoNewPrivs, read-only root;
  - **the host boundary holds with the in-namespace rules removed.**
- **Resource limits (UNVERIFIED until they pass on the VM):**
  - the cgroup values inside site, browser and worker containers equal the configured limits;
  - a 3 GiB allocation is OOM-killed;
  - a fork bomb stops below the PID limit;
  - four busy processes get at most the CPU quota.
- **Lifecycle:**
  - teardown removes everything;
  - a revoked token is rejected;
  - the janitor cleans up after an orchestrator SIGKILL.

## Generalized engine: side-by-side staging and deployment smoke (operator-run, $0)
**Staging (additive):**
- Check out the tested commit at `/opt/afd-next` (root-owned, read-only for `afd-orch`). `/opt/afd`, Caddy, systemd
  units and the live demo are left unchanged.
- Pull the fixture image (`python@sha256:e6da…`) in a **bounded build window**: a transient root unit
  (`systemd-run … -p ExecStartPre=…/afd-firewall.sh build-window -p ExecStopPost=…/afd-firewall.sh apply -p
  ExecStopPost=systemctl --user -M afd-orch@ start afd-orchd.service`, `RuntimeMaxSec=300`, pull under
  `timeout 240`).
  - `afd-orchd` is stopped first, with zero managed containers.
  - An independent re-lock guard (`systemd-run --on-active=8min …/afd-firewall.sh apply`) and the existing 10-minute
    refresh timer back it up.
  - Verify that the `AFD-ORCH` `dports 80,443` rule count is 0 (IPv4 and IPv6) and that `afd-orchd` is active before
    continuing.

**Approvals:** as `afd-orch` via `afd-ns`: `afd contract approve` for the M5 fixture (`--accept-destructive-controls`)
and `afd agent approve` for `ext-order-client` and `ext-smoke-limits`.

**Smoke:** `sudo -u afd-orch -H /opt/afd-next/deploy/vm/afd-ns python3 /opt/afd-next/deploy/vm/generic_smoke.py`
checks S1 isolation, S2 limits, S3 lost_ack and the oracle, S4 evidence, S5 teardown. It writes to
`$AFD_VAR/workflow/smoke`; exit 0 iff all pass.

**First result (2026-11-08):** PASS on all five, rootless, 24.3 s; bundle `vmsmoke-20261108T055533Z-a1260f`, digest
`f3b078ab…48645` (`docs/VULTR_DEPLOYMENT_EVIDENCE.md`).

## Operations
- `sudo /opt/afd/deploy/vm/afdctl status`: services, firewall, latest verification, 24-hour spend.
- `sudo /opt/afd/deploy/vm/afdctl qualify generic_guard cautious_no_resubmit`: queue a full qualification (about 46 runs; gateway budget $1.50).
- **Spend guards:**
  - the gateway enforces each job's budget before every model call;
  - the orchestrator enforces a daily cap (`AFD_DAILY_BUDGET_USD`, default $2.00) and a smoke quota;
  - the web accepts only preset policies, one run at a time, behind an access code with rate limiting.
- **Rebuilding images:** `build-images.sh` refuses to run while any Agent Fire Drill container exists, stops the orchestrator, and always re-locks the firewall on exit.
