# M6b: independently authored Playwright automation through the external-agent adapter

**Status: BLOCKED (2026-11-08).** Nothing ran on the agent path. No AFD code, contract, protocol, test or image was
changed. The only operator-approved action on the shared Docker environment was pruning the *unused build cache*.
No images, containers or volumes were deleted, and no further pulls were attempted.

## What was attempted
| Item | Value |
|---|---|
| Approved image | `mcr.microsoft.com/playwright:v1.63.0-noble`: index digest `sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27`, linux/amd64 manifest `sha256:bc6ab0d6d44ff4826e4cb8c1e6d801e185bfc42bb0753f8e2a30efc70db054c7` (7 layers, 0.96 GB compressed; roughly 2.5–3 GB unpacked) |
| Upstream suite selected | **Gitea v28.3.0** (tag object `f1cd050b55a46ae556cf992576d01b4a08bd793a`; the version AFD's Gitea contract runs), `tests/e2e/issue-project.test.ts`, test "create issue with multiple projects pre-selected"; helpers `tests/e2e/utils.ts`; `@playwright/test` pinned at 1.63.0 in `package.json` |
| Why that test | its consequential write is the UI's `POST /{owner}/{repo}/issues/new`, the exact route the existing Gitea contract watches. It is configured only by `GITEA_TEST_E2E_URL`, `GITEA_TEST_E2E_USER` and `GITEA_TEST_E2E_PASSWORD` |
| Modifications to the test | none (nothing was downloaded beyond reading the files) |

**Suite considered and rejected: Vikunja v2.8.0** (tag object `5dff8b9e09426ce5d4b955d7dd66eb81dff29d5d`,
`frontend/tests/e2e`).
- Every test's fixture runs `Factory.truncateAll()` and seeds through Vikunja's testing API, which requires starting the
  app with `VIKUNJA_SERVICE_TESTINGTOKEN`. That exposes a database-truncation endpoint, through the proxy, to the agent
  under test.
- It needs exclusive ownership of the database, which would wipe AFD's own setup and the scripted positive controls.
- It navigates to a hard-coded `/projects/1/1`.
- This is unsupported infrastructure; forcing it was declined.

## Observed blocker (exercised)
**The shared Docker disk is out of space.**
- The first pull failed with `no space left on device`; no partial data remained (ingest 4 KB).
- Pruning the unused build cache (operator-approved) freed 747 MB, leaving **288 MB free** on the 9.8 GB volume
  (97% used).
- **Nothing removable under the operator's rules frees enough:**

| Image | ID | Unique size | Referenced by | Removable? |
|---|---|---|---|---|
| `fire-drill/browser:0`, `fire-drill/site:0/1/2` | a06abdda0b5f, 51456fc058d2, f750d3f56ccc, c5c698c7fcd1 | 2–32 KB each (layers shared with `afd/*`) | nothing in the repo | yes, but frees nothing |
| `gitea/gitea:latest` | 8967bf9deaf3 | 249 MB | nothing (contracts pin the rootless image `40c5a68a…`) | yes; insufficient alone |
| `diffkin/env-*`, `reprocheck/env-*` | 461400f63db7, b84d33963edc, 167a3924ee3d, a8ee82a4596a | ~2.0 GB together | the operator's other projects | no |
| `wordpress`, `mariadb`, `postgres`, `vikunja`, `afd/*`, `node:24-slim`, `python:*` | – | up to 984 MB | frozen contracts (by digest) or the AFD build | no (frozen reproduction) |

**Decision (operator):** do not delete further data, do not expand the disk, do not pull again. The attempt stays
BLOCKED.

## Predicted integration issues (NOT exercised; from reading the upstream code and AFD's adapter)
1. **Dynamically created targets.** The test creates its own randomly named repository and writes there. AFD's harness
   creates the target object and arms faults on it. The test's write would therefore most likely be recorded as an
   **other-object effect**, and the armed lost_ack would **not hit it**.
2. **Cleanup that removes final-state evidence.** The test deletes its repository in `finally`. Under Gitea's
   contract-v1 (no oracle timeline), the oracle's final reading would likely show **no effect**, while the proxy
   records an accepted write. That is a per-object accounting mismatch.
3. **Packaging external test code.** The agent spec allows no mounts, so the test files (and `@playwright/test`, if the
   official image does not already include it; unverified) must live inside the agent image. That means a derived,
   locally built image.
4. **Registry-only image pinning.** `afd agent check` accepts only `name@sha256:` registry digests. A locally built
   derived image cannot be approved without publishing it to a registry.

Issues 3 and 4 would block the run before any AFD-specific behaviour is reached. Issues 1 and 2 would affect what the
evidence shows.

## Recommendation: the smallest general-purpose improvement (not implemented)
**An approved, read-only, hash-bound agent workspace.**
- An optional `workspace` directory in the agent spec, confined to the repository, is mounted **read-only** at
  `/afd/agent`.
- Its content tree hash (sha256 over every file path and content) becomes part of the agent approval, exactly like the
  spec itself. Any change invalidates the approval.
- Writable space stays the existing size-limited `/tmp`.
- No host write path and no network change.

**What it enables:** a developer runs their **unmodified** test suite plus its pre-installed dependencies (e.g.
`node_modules` with `@playwright/test`) on an **official, registry-pinned** image such as Playwright's, with the
command pointing into `/afd/agent`. This removes blockers 3 and 4 for every independently authored automation, without
custom image builds, a registry, or any knowledge of the app.

**Coupling and security:** it isn't coupled to Gitea. It adds one read-only, approval-bound input; containment,
non-root execution, the read-only root filesystem and the agent-network-only routing are unchanged.

**Left for later decisions (each general, none Gitea-specific):**
- binding the target to the object the agent's first watched write names (issue 1);
- running the oracle timeline under every protocol, so cleanup can't erase evidence (issue 2).
