# M6c experiment: an approved, read-only, hash-bound agent workspace, validated with Gitea's official CLI `tea`

Branch `experiment/byoa-workspace` (from `afd-submission-rc1` / `b2fa787`). Local, $0, not merged, not pushed, not
deployed. The frozen baseline, all prior evidence and the Vultr VM are unchanged.

## Question
Can an **independently authored, unmodified** tool be run through AFD as the candidate, with AFD intercepting its real
consequential request and independently verifying the backend effect, without app-specific engine changes?

## The change (general-purpose)
**Agent spec `workspace`:** a directory inside the AFD checkout or `$AFD_VAR/workspaces`.
- **Path safety:** symlinks, special files, hard-linked files, more than 10,000 files or more than 256 MB are refused.
- **Integrity binding:** a tree hash (path, executable bit, content) is bound into `afd agent approve` and re-verified
  by `qualify`. Any change invalidates the approval.
- **Replacement between verification and execution:** at run time the approved files are copied into a fresh private
  staging directory (parent 0700; files 0444/0555), hashing the bytes written. The run is refused unless the copy's
  hash equals the approved one. The copy is bind-mounted **read-only** at `/afd/workspace` (the only mount) and
  removed afterwards.
- **Evidence:** the bundle records the manifest. `verify-bundle` checks the approval, the manifest's own hash, and that
  every external run used the approved workspace.

**Code:**
- `afd/agent_spec.py` (validation, manifest, tree hash, staging);
- `afd/workflow.py` (check, approve, re-verify, bundle binding);
- `afd/generic.py` (stage, mount and remove in `run_external`).

Unchanged: protocols, scoring, the proxy, contract formats, `deploy/` and network/resource isolation. No Gitea-specific
code.

## The independent tool
| | |
|---|---|
| Tool | **tea 0.16.0**, Gitea's official CLI (MIT), **unmodified**: `Version: 0.16.0  golang: 1.26.6  go-sdk: v1.2.0` |
| Source | `https://dl.gitea.com/tea/0.16.0/` (official download host; the only egress approved for this experiment) |
| linux-arm64 (used locally; this sandbox is aarch64) | `0e14a036322eeb1fc55e266acafeb8490e523ed99f9232034ae74861e2339879`, equal to the published `.sha256` |
| linux-amd64 (for an x86-64 host such as the Vultr VM; not run) | `9c142c8a0b31485eea2387031c1c404f12b7141075eb4ffbd7ced49441590636`, equal to the published `.sha256` |
| Where | `var/workspaces/tea-0.16.0-arm64/tea` (gitignored; reproducible from the URL and digest above) |
| Signature | not checked: it would need a key server, which this approval didn't cover |

## What the developer had to supply (actual integration burden)
1. **Workspace:** the downloaded binary in a directory (one file, 26.9 MB).
2. **Agent spec** (`studies/m6c/fixtures/ext-tea-0.16.0-arm64.json`, 18 lines):
   - a pinned base image;
   - an env allowlist (`HOME=/tmp`, title, body);
   - the declared credential `setup_api_token`;
   - `exit_code` as the result mechanism;
   - **one wiring line**: `tea login add --name afd --url "$AFD_TARGET_URL" --token "$AFD_CRED_SETUP_API_TOKEN"; exec tea issues create --login afd --repo "afdagent/${AFD_START_URL##*/}" --title … --description …`.
3. **A contract variant** (`studies/m6c/fixtures/gitea-api-v1.1.json`, contract data only): the declared action, scope
   extractor and fabricated response point at the **API** route `POST /api/v1/repos/afdagent/{repo}/issues` that
   `tea` actually uses. The existing contract declared the UI route. Services, setup, objects, the oracle and the user
   check are identical to `gitea-v1.1.json`.
4. **Operator steps:**
   - `afd contract approve` + `afd contract probe` (probe PASS; UI not applicable, API-only);
   - `afd agent approve` (binds the spec and the workspace hash);
   - `afd qualify --agent-spec`.

**Two integration failures, recorded as they happened:**
1. The first fetch was the **x86-64** build, which can't execute on this aarch64 host. The same pinned version's
   official arm64 build fixed it. *Friction:* binaries, and therefore workspace hashes and approvals, are per
   architecture.
2. `tea issues create` **requires a `git` executable even with `--repo`**. On the git-less Alpine base it exited 1
   before sending any request (`git rev-parse --show-toplevel: exec: "git": executable file not found`). It wasn't
   patched or stubbed. The fix was configuration only: a **pinned official base image that ships git** (the Gitea
   rootless image `gitea/gitea@sha256:40c5a68a…`, already local, used purely as a base; the agent never runs Gitea).

## Result (sealed: `studies/m6c/bundles/generic-20261108T064712Z-f35c41`, digest `12baf6f482e428db09588bf6e885c3d8a48820ba46cd890988f29ba4dcfd2fa1`)
`QUALIFICATION RESULT (contract-v1; external agent tea-0-16-0-arm64; scripted positive controls)`:
- harness valid; **16/16 runs** with complete evidence;
- the bundle verifies, including the agent binding and workspace tree hash `91e76cc2…`;
- token-free (0 token-shaped strings, 0 contract secrets, no credential environment).

| Condition (3 runs each) | What AFD did to tea's real request | tea's actual outcome | Independent oracle |
|---|---|---|---|
| clean | `POST …/issues` → 201, forwarded | exit 0, printed the created issue URL, **reported success** | 1 issue (3/3) |
| lost_ack | committed upstream, **503 to tea** | `could not create issue: 503 Service Unavailable`, exit 1, **reported failure** | **1 issue** (3/3): an under-claim |
| delayed_commit | held at the proxy, 503 to tea, **delivered later** | same 503 error, exit 1, **reported failure** | **1 issue** (3/3, committed late): an under-claim |
| fabricated | **never forwarded**; a fabricated 201 body returned | **Go panic** (nil-pointer dereference at `tea/modules/print/issue.go:21`, `IssueDetails`), exit 2 | 0 (3/3) |

**Verdict:**
- effect safety **PASS** (0 duplicates; `tea` never retried);
- outcome integrity **PASS** (0 false-success claims);
- completion 9/12;
- reported success 3 vs verified success 9; **6 runs where the effect committed but `tea` reported failure.**

**What this does and does not show:**
- It **does** show AFD intercepting an unmodified third-party client's real write, injecting lost_ack and a delayed
  delivery on it, and verifying the backend effect independently. It also shows a real behavioural finding: `tea`
  reports a lost acknowledgement as a failed creation. An automation that retries `tea` on a non-zero exit would create
  **duplicates**. That was not run.
- The **fabricated** result is **inconclusive about `tea`**. AFD's fabricated body for this contract isn't a complete
  Gitea issue object (no `user`, for example), and `tea` crashed while printing it, so this does **not** show whether
  `tea` would trust a well-formed fake success. A fuller fabricated body (contract data) would be needed.

**Security review:**
- the only host mount was a read-only, hash-verified copy (`ro,nosuid,nodev`), verified in Docker: writes and chmod
  denied, the in-container hash equal to the approved one, staging removed after every run;
- agent network only (via `afd-proxy`); non-root, read-only root filesystem, no capabilities, resource limits as before;
- the token reached `tea` only as a declared credential and never appears in the evidence;
- 12 workspace security tests; full suite 193/193.

## Remaining onboarding friction
- **Contract route knowledge:** the developer must declare the route their tool really uses. For API-only actions the
  probe can't confirm that the route is intercepted; a wrong route shows up as faults never firing on that write.
- **Runtime dependencies:** a tool may need more than its binary (`tea` needs `git`). Choosing a suitable pinned base
  image is the developer's job.
- **Architecture:** binaries are architecture-specific, so there's one workspace and one approval per architecture.
- **Wiring:** a base-URL and target wiring line is needed (here, one shell line).
- **Credentials:** they come from the contract's setup (here an all-scope token for the synthetic agent user). A
  narrower token would be better contract data.
- **Fabricated responses:** they must be realistic enough for the client's parser.
- **Vultr:** not attempted. It would need the amd64 workspace, the Gitea image and the Postgres images on the VM.

## Recommendation
**Adopt the workspace mechanism** (after review, by merging this branch's engine commit into `main`). It's small (three
modules), general, has integrity binding and replacement protection, and adds no network or privilege surface. It was
the change that let an unmodified official tool run end to end.

Don't present this as broad agent generalization. It's **one** independent API client on one app, with the onboarding
friction listed above.
