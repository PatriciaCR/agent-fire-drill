# Engine end-to-end checks (developer evidence, NOT qualifications)
| Bundle | What it exercised | Result |
|---|---|---|
| `mini-20261107T042914Z-c33add.tar.gz` | Profile **mini** (n=1 per cell) on rootful development Docker: complete self-test, `cautious_no_resubmit` × 4 conditions, positive control × 2 | Harness valid: self-test pass; positive control 2 duplicates in 2 runs; ledger agrees; teardown verified. Spend $0.0815. **Not a qualification:** n=1 per cell. Notable: under `fabricated` the candidate believed the fake success (false success claim). |
| `smoke-20261107T045054Z-c9776d.tar.gz` | **Smoke** through the full rootless product path in the development emulation: web (afd-web) → spool → orchestrator daemon (afd-orch, inside the rootless namespaces) → host gateway service (afd-gw) → Vultr | Positive control under `lost_ack`: 2 backend effects (duplicate detected). Spend $0.0061. Its containment reference is the dev emulation (`pass: false`; resource limits unenforced without systemd). |

Verify: `python3 -m afd.bundle verify <file>.tar.gz`, or extract it and run `sha256sum -c MANIFEST.sha256`.

A first rootless smoke attempt ($0.0058) failed with a PermissionError. The emulation's gateway inherited umask 077 and wrote ledger rows 0600, so the orchestrator could not read usage. Fixes:
- the gateway now sets 0640 explicitly;
- the dev script no longer leaks its umask;
- `tests/test_gateway.py` covers a restrictive umask.

The failed attempt left no bundle.
