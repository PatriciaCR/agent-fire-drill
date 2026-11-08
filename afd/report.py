"""`afd report BUNDLE`: a human-readable safety report (self-contained HTML + Markdown) from a sealed bundle. Read-only.

Rules:
  - Manifest integrity (MANIFEST.sha256) is checked FIRST; a bundle that fails it gets only a refusal report.
  - The full verifier (recomputed classifications, approval binding) runs next; any problem is shown prominently, never
    hidden, and the report says the results are unverified.
  - Verdict text (statuses, statements) is quoted verbatim from verdict.json / summary.json. Every count in the
    per-condition table is computed from runs.jsonl and labelled as such. Fields a bundle does not record are shown as
    "not recorded"; nothing is inferred or reinterpreted (e.g. historical status strings are shown as written).
  - Supported: generic bundles (protocols contract-v1, contract-v2, contract-v3). Others are refused, not guessed at.
  - All bundle text is untrusted: HTML-escaped, Markdown-escaped; no scripts, no external assets."""
import html, json, re
from pathlib import Path
from . import bundle as B

SUPPORTED = ("contract-v1", "contract-v2", "contract-v3")
NR = "not recorded"

def _j(d, name):
    try: return json.loads((d / name).read_text())
    except (OSError, ValueError): return None

def _cell_runs(runs, policy, cond):
    return [r for r in runs if r.get("policy") == policy and r.get("condition") == cond]

def _condition_row(rs, protocol):
    """Counts computed from stored run fields only."""
    agent = [r for r in rs if r.get("category") == "agent"]
    t = lambda r: r.get("target_effects", r.get("backend_effects"))
    claim = lambda r: (r.get("claim") or {}).get("claimed_success")
    row = {"planned_or_recorded": len(rs), "scored_agent_runs": len(agent), "no_valid_result": len(rs) - len(agent),
           "reported_success": sum(claim(r) is True for r in agent), "reported_failure": sum(claim(r) is False for r in agent),
           "backend_target_effects": sorted(t(r) for r in agent if t(r) is not None),
           "duplicate_runs": sum((r.get("duplicate_target_effects") or 0) > 0 for r in agent),
           "other_object_runs": sum((r.get("other_object_effects") or 0) > 0 for r in agent),
           "false_success": sum(claim(r) is True and t(r) == 0 for r in agent),
           "under_claim": sum(claim(r) is False and (t(r) or 0) >= 1 for r in agent),
           "proxy_on_target": sorted({" → ".join(f"{x.get('status_to_client')} {x.get('intervention')}/{x.get('outcome')}"
                                                 for x in sorted([o for o in r.get("object_records") or [] if o.get("object") == r.get("target")], key=lambda o: o.get("t_received", 0)))
                                       for r in agent} - {""})}
    if protocol in ("contract-v2", "contract-v3"):
        row["fidelity"] = dict(sorted(_count(r.get("fidelity") for r in agent).items()))
        row["application_protection"] = dict(sorted(_count(p["class"] for r in agent for p in r.get("protection") or []).items()))
    else:
        row["fidelity"] = row["application_protection"] = NR + " (contract-v1)"
    row["pending_at_horizon_runs"] = sum(bool(r.get("pending_at_horizon")) for r in agent) if protocol == "contract-v3" else NR
    return row

def _count(xs):
    out = {}
    for x in xs: out[x] = out.get(x, 0) + 1
    return out

def build(bundle_dir, verifier=None):
    """The report model. verifier: callable(bundle_dir) -> {"ok", "problems", ...} (afd.workflow.verify_bundle)."""
    d = Path(bundle_dir); m = B.verify(d)
    model = {"bundle": d.name, "integrity": {"ok": m["ok"], "digest": m.get("digest"), "files": m.get("files"), "problems": m.get("problems") or []}}
    if not m["ok"]: model["refused"] = "the bundle failed its manifest integrity check (MANIFEST.sha256); nothing in it can be trusted"; return model
    summ, prot = _j(d, "summary.json") or {}, _j(d, "protocol.json") or {}
    protocol = prot.get("protocol") or summ.get("protocol")
    if protocol not in SUPPORTED:
        model["refused"] = f"unsupported bundle format (protocol {protocol!r}); this report renders generic bundles ({', '.join(SUPPORTED)}) only"; return model
    ver = verifier(d) if verifier else {"ok": None, "problems": ["verifier not run"]}
    v, plan = _j(d, "verdict.json") or {}, _j(d, "plan.json") or {}
    runs = [json.loads(l) for l in (d / "runs.jsonl").read_text().splitlines() if l.strip()] if (d / "runs.jsonl").exists() else []
    env, appr, src = _j(d, "environment.json") or {}, _j(d, "approval.json"), _j(d, "contract_source.json")
    spec, aappr, ws = _j(d, "agent_spec.json"), _j(d, "agent_approval.json"), _j(d, "agent_workspace.json")
    hv = v.get("harness_validity") or {}
    conds = plan.get("conditions") or sorted({r.get("condition") for r in runs})
    cand_names = sorted({r["policy"] for r in plan.get("runs", []) if r.get("role") == "candidate"})
    model.update({
        "verification": {"ok": ver.get("ok"), "problems": ver.get("problems") or []},
        "protocol": protocol, "status_verbatim": summ.get("qualification_status", NR),
        "workflow": {"contract_name": summ.get("contract", NR), "contract_sha256": (src or {}).get("sha256", NR),
                     "services": env.get("services") or NR, "approved_by": (appr or {}).get("approver", NR), "approved_utc": (appr or {}).get("approved_utc", NR)},
        "agent": ({"kind": "external agent (the candidate under test)", "name": spec.get("name"), "image": spec.get("image"), "command": spec.get("command"),
                   "result_mechanism": (spec.get("result") or {}).get("mechanism"), "spec_note_verbatim": spec.get("note", NR),
                   "spec_sha256": (aappr or {}).get("agent_spec_sha256", NR), "approved_by": (aappr or {}).get("approver", NR),
                   "workspace": ({"tree_sha256": ws.get("tree_sha256"), "files": [(f["path"], f["bytes"], f["sha256"]) for f in ws.get("files", [])], "mount": ws.get("mount")} if ws else None)}
                  if spec else {"kind": "AFD scripted policies (deterministic; not an external agent)", "policies": cand_names,
                                "scripted_policy_version": summ.get("scripted_policy_version", "unrecorded (bundle predates M4.1; v1 implied)")}),
        "positive_controls": {"verbatim": hv.get("positive_control") or hv.get("positive_controls") or NR,
                              "note": "deterministic scripted positive controls validate the harness; they are not the candidate"},
        "harness": {"valid": hv.get("valid"), "checks": hv.get("checks") or {}, "failed": [k for k, x in (hv.get("checks") or {}).items() if not x],
                    "cells": hv.get("cells") or {}, "accounting_mismatches": hv.get("accounting_mismatches") or {}, "fidelity_exclusions": hv.get("fidelity_exclusions") or {}},
        "evidence_completeness": summ.get("evidence_completeness", NR),
        "candidates": {p: {"verdict_verbatim": (v.get("candidates") or {}).get(p, NR),
                           "conditions": {c: _condition_row(_cell_runs([r for r in runs if r.get("role") == "candidate"], p, c), protocol) for c in conds}} for p in cand_names},
        "applicability_verbatim": v.get("applicability") or [],
        "runs": [{"run_id": r.get("run_id"), "role": r.get("role"), "control": r.get("control"), "policy": r.get("policy"), "condition": r.get("condition"),
                  "category": r.get("category"), "reported": (r.get("claim") or {}).get("claimed_success"), "exit_code": r.get("agent_exit_code"),
                  "target_effects": r.get("target_effects", r.get("backend_effects")), "duplicates": r.get("duplicate_target_effects"),
                  "other_object_effects": r.get("other_object_effects"), "fidelity": r.get("fidelity", NR), "pending": r.get("pending_at_horizon"),
                  "proxy_on_target": [f"{x.get('status_to_client')} {x.get('intervention')}/{x.get('outcome')}" for x in sorted([o for o in r.get("object_records") or [] if o.get("object") == r.get("target")], key=lambda o: o.get("t_received", 0))]}
                 for r in sorted(runs, key=lambda r: r.get("run_id", ""))],
        "verify": [f"afd contract verify-bundle {d.name}", f"cd {d.name} && sha256sum -c MANIFEST.sha256   # digest of MANIFEST.sha256: {m.get('digest')}"]})
    return model

# ------------------------------------------------------------------------------------------------ rendering
E = lambda x: html.escape(str(x), quote=True)
def M(x):
    s = str(x).replace("\\", "\\\\").replace("\n", " ").replace("\r", " ")
    return re.sub(r"([`*_\[\]<>|#!~])", r"\\\1", s)

def C(x):
    """A Markdown code span: content is literal (backslash escapes don't apply inside it), so the fence is sized instead."""
    s = str(x).replace("\n", " ").replace("\r", " ")
    f = "`" * (max((len(m) for m in re.findall(r"`+", s)), default=0) + 1)
    return f"{f} {s} {f}" if s.startswith("`") or s.endswith("`") else f + s + f

def _fmt(x):
    if isinstance(x, (dict, list)): return json.dumps(x, ensure_ascii=False, sort_keys=True)
    return "—" if x is None else str(x)

def _banner(model):
    out = []
    if model.get("refused"): return [("bad", f"REPORT REFUSED: {model['refused']}")]
    if model["verification"]["ok"] is not True: out.append(("bad", "VERIFICATION PROBLEMS — results below are UNVERIFIED: " + "; ".join(map(str, model["verification"]["problems"])) ))
    if model["harness"]["valid"] is False: out.append(("bad", "HARNESS INVALID — candidates are NOT ASSESSED. Failed checks: " + ", ".join(model["harness"]["failed"])))
    st = model["status_verbatim"]
    out.append(("ok" if str(st).startswith("QUALIFICATION RESULT") and model["verification"]["ok"] else "warn", f"Status (verbatim): {st}"))
    return out

COND_COLS = [("planned_or_recorded", "runs"), ("no_valid_result", "no valid result"), ("reported_success", "agent reported success"), ("reported_failure", "agent reported failure"),
             ("backend_target_effects", "backend effects on target (per run)"), ("duplicate_runs", "runs with duplicates"), ("other_object_runs", "runs with other-object effects"),
             ("false_success", "false success claims"), ("under_claim", "under-claims"), ("proxy_on_target", "what the proxy did on the target"), ("fidelity", "fault fidelity"),
             ("application_protection", "application protection (extra attempts)"), ("pending_at_horizon_runs", "pending at horizon")]

def render_html(model):
    h = ["<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
         f"<title>AFD safety report {E(model['bundle'])}</title><style>body{{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1200px;color:#111;background:#fff}}"
         "table{border-collapse:collapse;margin:8px 0 18px;width:100%}td,th{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}th{background:#f3f3f3}"
         ".bad{background:#fde8e8;border:1px solid #c33;padding:8px;margin:6px 0}.warn{background:#fff7e0;border:1px solid #c90;padding:8px;margin:6px 0}"
         ".ok{background:#e8f6ea;border:1px solid #393;padding:8px;margin:6px 0}code{background:#f5f5f5;padding:1px 3px;word-break:break-all}.small{color:#555;font-size:12px}</style></head><body>",
         f"<h1>Agent Fire Drill safety report</h1><p class=small>Bundle <code>{E(model['bundle'])}</code> · manifest digest <code>{E(model['integrity'].get('digest'))}</code> · "
         f"{E(model['integrity'].get('files'))} files · integrity {'OK' if model['integrity']['ok'] else 'FAILED'}</p>"]
    for cls, txt in _banner(model): h.append(f"<div class={cls}>{E(txt)}</div>")
    if model.get("refused"):
        h.append("<p>Integrity problems: " + E("; ".join(map(str, model["integrity"]["problems"]))) + "</p></body></html>"); return "".join(h)
    w, a = model["workflow"], model["agent"]
    h.append(f"<h2>Workflow</h2><table><tr><th>contract</th><td>{E(w['contract_name'])}</td></tr><tr><th>contract sha256</th><td><code>{E(w['contract_sha256'])}</code></td></tr>"
             f"<tr><th>services (pinned)</th><td><code>{E(_fmt(w['services']))}</code></td></tr><tr><th>protocol</th><td>{E(model['protocol'])}</td></tr>"
             f"<tr><th>contract approved by</th><td>{E(w['approved_by'])} ({E(w['approved_utc'])})</td></tr></table>")
    h.append(f"<h2>Agent / tool under test</h2><table><tr><th>kind</th><td>{E(a['kind'])}</td></tr>")
    for k in ("name", "image", "result_mechanism", "spec_sha256", "approved_by", "policies", "scripted_policy_version"):
        if k in a: h.append(f"<tr><th>{E(k)}</th><td><code>{E(_fmt(a[k]))}</code></td></tr>")
    if "command" in a: h.append(f"<tr><th>command</th><td><code>{E(_fmt(a['command']))}</code></td></tr>")
    if "spec_note_verbatim" in a: h.append(f"<tr><th>provenance / description (from the approved spec, verbatim)</th><td>{E(a['spec_note_verbatim'])}</td></tr>")
    if a.get("workspace"): h.append(f"<tr><th>workspace (read-only, hash-bound)</th><td>tree <code>{E(a['workspace']['tree_sha256'])}</code> mounted at <code>{E(a['workspace']['mount'])}</code><br>"
                                    + "<br>".join(f"<code>{E(p)}</code> {E(b)} bytes sha256 <code>{E(s)}</code>" for p, b, s in a["workspace"]["files"]) + "</td></tr>")
    h.append("</table><p class=small>Behaviour in this report is that of the candidate described above. Wrappers, if any, are part of the candidate and are named in its description. "
             "Application protections are reported separately (fault fidelity and application-protection columns).</p>")
    h.append("<h2>Harness validity</h2><table>" + "".join(f"<tr><th>{E(k)}</th><td>{'PASS' if x else 'FAIL'}</td></tr>" for k, x in model["harness"]["checks"].items())
             + f"<tr><th>positive controls (verbatim)</th><td><code>{E(_fmt(model['positive_controls']['verbatim']))}</code><br><span class=small>{E(model['positive_controls']['note'])}</span></td></tr>"
             + f"<tr><th>evidence completeness</th><td><code>{E(_fmt(model['evidence_completeness']))}</code></td></tr></table>")
    if model["harness"]["cells"]:
        h.append("<h3>Coverage per cell (verbatim from verdict.json)</h3><table><tr><th>cell</th><th>planned</th><th>scored</th></tr>"
                 + "".join(f"<tr><td>{E(k)}</td><td>{E(_fmt(x.get('planned')))}</td><td>{E(_fmt(x.get('scored')))}</td></tr>" for k, x in model["harness"]["cells"].items()) + "</table>")
    for p, c in model["candidates"].items():
        h.append(f"<h2>Candidate <code>{E(p)}</code></h2><h3>Verdict (verbatim from verdict.json)</h3><pre style='white-space:pre-wrap'>{E(json.dumps(c['verdict_verbatim'], indent=1, ensure_ascii=False))}</pre>")
        h.append("<h3>Per condition: agent-reported vs independently verified (counts computed from runs.jsonl)</h3><table><tr><th>condition</th>" + "".join(f"<th>{E(t)}</th>" for _, t in COND_COLS) + "</tr>")
        for cond, row in c["conditions"].items(): h.append(f"<tr><th>{E(cond)}</th>" + "".join(f"<td>{E(_fmt(row[k]))}</td>" for k, _ in COND_COLS) + "</tr>")
        h.append("</table>")
    if model["applicability_verbatim"]: h.append("<h2>Applicability (verbatim)</h2><ul>" + "".join(f"<li>{E(x)}</li>" for x in model["applicability_verbatim"]) + "</ul>")
    h.append("<h2>All runs</h2><table><tr><th>run</th><th>role</th><th>policy</th><th>condition</th><th>category</th><th>agent reported success</th><th>exit</th><th>target effects</th><th>duplicates</th><th>other-object</th><th>fidelity</th><th>proxy on target</th></tr>"
             + "".join(f"<tr><td>{E(r['run_id'])}</td><td>{E(r['role'])}{' ' + E(r['control']) if r.get('control') else ''}</td><td>{E(r['policy'])}</td><td>{E(r['condition'])}</td><td>{E(r['category'])}</td>"
                       f"<td>{E(_fmt(r['reported']))}</td><td>{E(_fmt(r['exit_code']))}</td><td>{E(_fmt(r['target_effects']))}</td><td>{E(_fmt(r['duplicates']))}</td><td>{E(_fmt(r['other_object_effects']))}</td>"
                       f"<td>{E(_fmt(r['fidelity']))}</td><td>{E(' | '.join(r['proxy_on_target']))}</td></tr>" for r in model["runs"]) + "</table>")
    h.append("<h2>Verify this evidence yourself</h2><ul>" + "".join(f"<li><code>{E(x)}</code></li>" for x in model["verify"]) + "</ul></body></html>")
    return "".join(h)

def render_md(model):
    L = [f"# Agent Fire Drill safety report", "", f"Bundle {C(model['bundle'])} · manifest digest {C(model['integrity'].get('digest'))} · integrity {'OK' if model['integrity']['ok'] else 'FAILED'}", ""]
    L += [f"> **{M(t)}**" for _, t in _banner(model)] + [""]
    if model.get("refused"): return "\n".join(L + ["Integrity problems: " + M("; ".join(map(str, model["integrity"]["problems"])))]) + "\n"
    w, a = model["workflow"], model["agent"]
    L += ["## Workflow", "", f"- contract: {M(w['contract_name'])}", f"- contract sha256: {C(w['contract_sha256'])}", f"- services: {C(_fmt(w['services']))}",
          f"- protocol: {M(model['protocol'])}", f"- approved by: {M(w['approved_by'])} ({M(w['approved_utc'])})", "", "## Agent / tool under test", "", f"- kind: {M(a['kind'])}"]
    for k in ("name", "image", "result_mechanism", "spec_sha256", "approved_by", "policies", "scripted_policy_version", "command"):
        if k in a: L.append(f"- {k}: {C(_fmt(a[k]))}")
    if "spec_note_verbatim" in a: L.append(f"- provenance / description (verbatim): {M(a['spec_note_verbatim'])}")
    if a.get("workspace"): L.append(f"- workspace (read-only, hash-bound): tree {C(a['workspace']['tree_sha256'])}; " + "; ".join(f"{C(p)} {b} bytes sha256 {C(s)}" for p, b, s in a["workspace"]["files"]))
    L += ["", "## Harness validity", ""] + [f"- {M(k)}: {'PASS' if x else 'FAIL'}" for k, x in model["harness"]["checks"].items()]
    L += [f"- evidence completeness: {C(_fmt(model['evidence_completeness']))}", ""]
    if model["harness"]["cells"]:
        L += ["Coverage per cell (verbatim): " + "; ".join(f"{M(k)} {x.get('scored')}/{x.get('planned')} scored" for k, x in model["harness"]["cells"].items()), ""]
    for p, c in model["candidates"].items():
        L += [f"## Candidate {C(p)}", "", "Verdict (verbatim):", "", f"{C(_fmt(c['verdict_verbatim']))}", "", "Per condition (computed from runs.jsonl):", "",
              "| condition | " + " | ".join(M(t) for _, t in COND_COLS) + " |", "|" + "---|" * (len(COND_COLS) + 1)]
        for cond, row in c["conditions"].items(): L.append(f"| {M(cond)} | " + " | ".join(M(_fmt(row[k])) for k, _ in COND_COLS) + " |")
        L.append("")
    L += ["## Verify", ""] + [f"- {C(x)}" for x in model["verify"]]
    return "\n".join(L) + "\n"

def write(bundle_dir, out_dir, verifier=None):
    model = build(bundle_dir, verifier)
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    (out / "report.html").write_text(render_html(model)); (out / "report.md").write_text(render_md(model))
    return model, out
