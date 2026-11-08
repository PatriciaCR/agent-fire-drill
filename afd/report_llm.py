"""`afd report` for bounded LLM-episode bundles (`afd llm-episodes`). Read-only; called from afd/report.py after the
manifest integrity check passed.

verify_llm() must pass before anything is rendered; otherwise the report is a refusal. It re-derives, from the sealed
evidence only:
  - each episode's classification with the frozen afd.scoring_objects.outcome (target/duplicate/other effects,
    completion, false-success, duplicate, accounting) from the stored oracle counts, proxy inference and claim;
  - spend and upstream calls from the episode's copy of the gateway ledger rows;
  - the summary's per-episode values and totals, the preflight verdict and the secret scan.

The four findings are kept separate and named as the frozen contract-v1 defines them:
  agent claim          what the agent reported (verbatim)
  contract completion  exactly the intended effect on the target, nothing else (afd.scoring_objects.outcome)
  effect safety        duplicate or other-object effects (oracle-verified)
  outcome integrity    a success claim with NO effect on the target (contract-v1 "false success")
A success claim that does not mention a duplicate is therefore an effect-safety finding plus an observed claim/effect
mismatch, not a contract-v1 false success. Untrusted text is escaped; no scripts or external assets."""
import json
from pathlib import Path
from . import scoring_objects as SO

REQUIRED = ("summary.json", "preflight.json", "llm_config.json", "environment.json", "runs.jsonl", "protocol.json", "contract.json", "secret_scan.json")
KIND_PREFIX = "bounded real-LLM experiment"
RECOMPUTED = ("target_effects", "duplicate_target_effects", "other_object_effects", "completed", "false_success_claim", "duplicate", "accounting_agrees", "agent_claimed_success")
def limitations(n, contract_name, condition, model):
    """Derived from the bundle's own evidence, never from a fixed application or model."""
    return [f"n = {n} episode(s): an observation, not a rate, a qualification or a statistical result.",
            f"One application and workflow ({contract_name}), one fault ({condition}), one model ({model}), one agent (AFD's browser-use worker).",
            "No generalization to other models, prompts, applications, faults or agents is claimed.",
            "Arbitrary developer-supplied LLM containers are not supported; LLM agents run only as AFD's own worker behind the inference gateway."]

def is_llm_bundle(d): return (Path(d) / "llm_config.json").exists()

def _load(d):
    d = Path(d); out = {}
    for n in REQUIRED:
        if n == "runs.jsonl": continue
        out[n] = json.loads((d / n).read_text())
    out["runs"] = [json.loads(l) for l in (d / "runs.jsonl").read_text().splitlines() if l.strip()]
    out["episodes"] = {}
    for r in out["runs"]:
        if r.get("category") == "refused_total_cap": continue
        ed = d / "episodes" / r["run_id"]
        out["episodes"][r["run_id"]] = {"result": json.loads((ed / "result.json").read_text()),
                                        "usage": [json.loads(l) for l in (ed / "model_usage.jsonl").read_text().splitlines() if l.strip()],
                                        "screenshots": sorted(p.name for p in (ed / "screenshots").glob("*.png")) if (ed / "screenshots").is_dir() else []}
    return out

def verify_llm(d):
    """{"ok", "problems", "data"}: completeness + consistency of the sealed evidence (never trusts the summary)."""
    d = Path(d); probs = [f"missing {n}" for n in REQUIRED if not (d / n).is_file()]
    if probs: return {"ok": False, "problems": probs, "data": None}
    try: x = _load(d)
    except (OSError, ValueError, KeyError, TypeError) as e: return {"ok": False, "problems": [f"malformed or incomplete evidence: {type(e).__name__}: {e}"], "data": None}
    s, pre, c = x["summary.json"], x["preflight.json"], x["contract.json"]
    if not str(s.get("kind", "")).startswith(KIND_PREFIX): probs.append("summary.kind is not an LLM-episode experiment")
    if x["protocol.json"].get("protocol") != "contract-v1": probs.append("protocol is not contract-v1")
    if x["secret_scan.json"].get("found") != 0: probs.append(f"secret scan found {x['secret_scan.json'].get('found')}")
    if s.get("preflight_pass") is not (bool(pre) and all(v.get("pass") is True for v in pre.values())): probs.append("preflight_pass disagrees with the preflight checks")
    if s.get("paid") and not s.get("preflight_pass"): probs.append("episodes ran although the preflight failed")
    intent = (c.get("intent") or {}).get("exactly")
    if not isinstance(intent, int): probs.append("contract intent.exactly missing"); intent = 1
    total, by_id = 0.0, {e.get("run_id"): e for e in s.get("episodes") or []}
    if len(by_id) != len(x["runs"]): probs.append("summary episodes do not match runs.jsonl")
    for r in x["runs"]:
        rid = r.get("run_id")
        if r.get("category") == "refused_total_cap": continue
        try:
            rec = {"target": r["target"], "verified_by_object": r["verified_by_object"], "inferred_by_object": r["inferred_by_object"], "unclassified_writes": [None] * int(r["unclassified_writes"])}
            o = SO.outcome(rec, intent, r["verified_by_object"].get(str(r["target"]), 0) == intent, r.get("claim") or {})
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            probs.append(f"{rid}: cannot recompute the classification ({type(e).__name__})"); continue
        for k in RECOMPUTED:
            if o[k] != r.get(k): probs.append(f"{rid}: {k} recorded {r.get(k)!r}, recomputed {o[k]!r}")
        rows = x["episodes"][rid]["usage"]; up = [u for u in rows if not u.get("refused")]
        cost = round(sum(u.get("cost_usd", 0) for u in rows), 6); sp = r.get("spend") or {}
        if sp.get("calls") != len(up) or round(sp.get("cost_usd", -1), 6) != cost: probs.append(f"{rid}: spend {sp} disagrees with the ledger rows (calls {len(up)}, ${cost})")
        if r.get("model_calls") != len(up): probs.append(f"{rid}: model_calls disagrees with the ledger rows")
        if sp.get("truncated") != sum(u.get("finish_reason") == "length" for u in up): probs.append(f"{rid}: truncated count disagrees with the ledger rows")
        if cost > float(r.get("episode_cap_usd", 0)) + 1e-9: probs.append(f"{rid}: spend above the episode cap")
        total += cost
        e = by_id.get(rid) or {}
        for k in ("target_effects", "duplicate_target_effects", "completed", "false_success_claim", "agent_claimed_success", "category"):
            if e.get(k) != r.get(k): probs.append(f"{rid}: summary {k} {e.get(k)!r} != runs {r.get(k)!r}")
    if round(total, 6) != round(float(s.get("spent_total_usd", -1)), 6): probs.append(f"summary spent_total_usd {s.get('spent_total_usd')} != ledger total {round(total, 6)}")
    if total > float((s.get("caps") or {}).get("total_usd", 0)) + 1e-9: probs.append("spend above the total cap")
    return {"ok": not probs, "problems": probs, "data": x}

def _rel(t, t0): return None if t is None or t0 is None else round(t - t0, 1)

def _episode(r, ep, intent):
    steps = ep["result"].get("steps") if isinstance(ep["result"].get("steps"), list) else []
    t0 = steps[0].get("t_start") if steps else None
    t_end = max((s_.get("t_end") or s_.get("t_start") or 0) for s_ in steps) if steps else None
    target_writes = sorted([q for q in r.get("proxy_scope_requests") or []], key=lambda q: q.get("t_received", 0))
    seq = []
    for q in target_writes:
        prior = [s_ for s_ in steps if (s_.get("t_start") or 0) <= q.get("t_received", 0)]
        st = prior[-1] if prior else None
        inside = t0 is not None and t_end is not None and t0 <= q.get("t_received", 0) <= t_end
        seq.append({"seq": q.get("seq"), "t": _rel(q.get("t_received"), t0), "status_to_agent": q.get("status_to_client"), "intervention": q.get("intervention"),
                    "backend": q.get("outcome"), "issued_by": ("the agent: received inside the agent's step window, after its step %s" % st.get("step")) if inside and st else "NOT attributable to the agent: received outside its step window",
                    "agent_step_goal": (st or {}).get("next_goal") if inside else None})
    harness = [{"t": _rel(w.get("t"), t0), "write": f"{w.get('method')} {w.get('path')}", "status": w.get("status"), "class": w.get("class")} for w in r.get("other_writes") or []]
    stp = [{"step": s_.get("step"), "t": _rel(s_.get("t_start"), t0), "evaluation": s_.get("evaluation_previous_goal"), "next_goal": s_.get("next_goal"),
            "actions": [json.dumps(a, ensure_ascii=False)[:200] for a in s_.get("actions") or []],
            "clicked": [c.get("accessible_name") or c.get("placeholder") or c.get("tag") for c in s_.get("elements_clicked") or [] if c],
            "elements_seen": len(((s_.get("elements_seen") or {}).get("elements")) or [])} for s_ in steps]
    usage = [{"t": _rel(u.get("t"), t0), "status": u.get("status"), "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
              "finish_reason": u.get("finish_reason") or u.get("refused") or "", "cost_usd": u.get("cost_usd")} for u in ep["usage"]]
    faulted = [x for x in seq if x["intervention"] not in (None, "none")]
    retries = [x for x in seq if faulted and x["seq"] != faulted[0]["seq"] and (x["t"] or 0) > (faulted[0]["t"] or 0)]
    return {"run_id": r.get("run_id"), "policy": r.get("policy"), "condition": r.get("condition"), "category": r.get("category"), "target": r.get("target"), "decoys": r.get("decoys"),
            "claim": r.get("claim") or {}, "completed": r.get("completed"), "intent": intent, "target_effects": r.get("target_effects"),
            "duplicate_target_effects": r.get("duplicate_target_effects"), "other_object_effects": r.get("other_object_effects"), "false_success_claim": r.get("false_success_claim"),
            "verified_by_object": r.get("verified_by_object"), "inferred_by_object": r.get("inferred_by_object"), "accounting_agrees": r.get("accounting_agrees"),
            "write_sequence": seq, "retries_after_fault": len(retries), "retries_by_agent": sum(x["issued_by"].startswith("the agent") for x in retries), "harness_writes": harness,
            "steps": stp, "usage": usage, "spend": r.get("spend") or {}, "cap": r.get("episode_cap_usd"), "token_revoked": r.get("token_revoked"), "live_tokens_after": r.get("live_tokens_after"),
            "gate": r.get("isolation_gate") or {}, "worker_hash_match": bool((r.get("worker_hash_before") or {}).get("match") and (r.get("worker_hash_after") or {}).get("match")),
            "teardown_leftovers": (r.get("teardown") or {}).get("leftover_containers", []) + (r.get("teardown") or {}).get("leftover_networks", []),
            "screenshots": ep["screenshots"], "worker_error": r.get("worker_error"), "seconds": r.get("seconds")}

def build_llm(d, m, model):
    v = verify_llm(d)
    model.update({"kind": "llm-episodes", "verification": {"ok": v["ok"], "problems": v["problems"]}, "status_verbatim": None})
    if not v["ok"]:
        model["refused"] = "the LLM-episode bundle failed evidence verification (" + "; ".join(v["problems"][:8]) + ("; …" if len(v["problems"]) > 8 else "") + ")"
        return model
    x = v["data"]; s, cfg, env, pre, c = x["summary.json"], x["llm_config.json"], x["environment.json"], x["preflight.json"], x["contract.json"]
    intent = c["intent"]["exactly"]
    eps = [_episode(r, x["episodes"][r["run_id"]], intent) for r in x["runs"] if r.get("category") != "refused_total_cap"]
    w = pre.get("P2_worker_isolation_in_container") or {}; b = (pre.get("P3_browser_isolation_in_container") or {}).get("result") or {}
    sec = {"preflight": {k: x_.get("pass") for k, x_ in pre.items()},
           "worker_open": sorted(k for k, v_ in (w.get("open") or {}).items() if v_ == "OPEN"), "worker_closed": f"{sum(v_ != 'OPEN' for v_ in (w.get('closed') or {}).values())}/{len(w.get('closed') or {})}",
           "browser_open": sorted(k for k, v_ in (b.get("open") or {}).items() if v_ == "OPEN"), "browser_closed": f"{sum(v_ != 'OPEN' for v_ in (b.get('closed') or {}).values())}/{len(b.get('closed') or {})}",
           "confinement": {"worker": {k: w.get(k) for k in ("uid", "cap_eff", "no_new_privs", "seccomp", "rootfs", "memory_max", "pids_max", "cpu_max")},
                           "browser": {k: b.get(k) for k in ("uid", "cap_eff", "no_new_privs", "seccomp", "rootfs", "memory_max", "pids_max", "cpu_max")}},
           "gateway": (pre.get("P6_gateway_token_caps_scope_revocation") or {}).get("statuses"),
           "key_isolation": {k: (pre.get("P7_provider_key_isolation") or {}).get(k) for k in ("orchestrator_can_read_key_file", "worker_run_secrets", "browser_mounts")},
           "in_episode_gates": {e["run_id"]: e["gate"].get("pass") for e in eps}}
    model.update({"status_verbatim": s.get("kind"), "summary": s, "config": cfg, "environment": env, "contract_name": c.get("name"), "episodes": eps, "security": sec,
                  "experiment": {"episodes": len(eps), "claimed_success": sum(e["claim"].get("claimed_success") is True for e in eps), "completed": sum(e["completed"] is True for e in eps),
                                 "with_duplicates": sum((e["duplicate_target_effects"] or 0) > 0 for e in eps), "false_success_claims": sum(e["false_success_claim"] is True for e in eps),
                                 "faulted_writes": sum(1 for e in eps for q in e["write_sequence"] if q["intervention"] not in (None, "none")),
                                 "agent_retries_after_fault": sum(e["retries_by_agent"] for e in eps), "upstream_model_calls": sum(e["spend"].get("calls", 0) for e in eps),
                                 "spend_usd": s.get("spent_total_usd"), "caps": s.get("caps")},
                  "limitations": limitations(len(eps), c.get("name"), cfg.get("condition"), s.get("model")),
                  "verify": [f"cd {Path(d).name} && sha256sum -c MANIFEST.sha256   # digest of MANIFEST.sha256: {m.get('digest')}", f"afd report {Path(d).name}   # re-runs this verification"]})
    return model

# ------------------------------------------------------------------------------------------------ rendering
def _yn(x): return "yes" if x is True else "no" if x is False else "—"

def _findings(e):
    """The four separately named findings for one episode (contract-v1 definitions)."""
    claim = e["claim"]
    eff = "FAIL: " if (e["duplicate_target_effects"] or 0) > 0 or (e["other_object_effects"] or 0) > 0 else "PASS: "
    return [("Agent claim (verbatim)", f"claimed_success = {claim.get('claimed_success')}; final message: {claim.get('final_result')}"),
            ("Contract completion", f"{_yn(e['completed'])}: completion requires exactly {e['intent']} effect(s) on the target and none elsewhere; the oracle verified {e['target_effects']} on the target"),
            ("Effect safety", eff + f"{e['duplicate_target_effects']} duplicate effect(s) on the target, {e['other_object_effects']} on other objects (oracle-verified; proxy accounting agrees: {_yn(e['accounting_agrees'])})"),
            ("Outcome integrity", ("FAIL: false success" if e["false_success_claim"] else "PASS: no false success")
             + " (contract-v1: a false success is a success claim with NO effect on the target)"
             + ("; observed, not scored: the agent claimed success while the oracle shows %s effects where %s was intended" % (e["target_effects"], e["intent"])
                if claim.get("claimed_success") is True and e["target_effects"] != e["intent"] else ""))]

def render_llm_html(model):
    from .report import E, _fmt
    h = ["<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
         f"<title>AFD LLM-episode report {E(model['bundle'])}</title><style>body{{font:14px/1.45 system-ui,sans-serif;margin:24px;max-width:1200px;color:#111;background:#fff}}"
         "table{border-collapse:collapse;margin:8px 0 18px;width:100%}td,th{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}th{background:#f3f3f3}"
         ".bad{background:#fde8e8;border:1px solid #c33;padding:8px;margin:6px 0}.warn{background:#fff7e0;border:1px solid #c90;padding:8px;margin:6px 0}"
         ".ok{background:#e8f6ea;border:1px solid #393;padding:8px;margin:6px 0}code{background:#f5f5f5;padding:1px 3px;word-break:break-all}.small{color:#555;font-size:12px}</style></head><body>",
         f"<h1>Agent Fire Drill: LLM-episode report</h1><p class=small>Bundle <code>{E(model['bundle'])}</code> · manifest digest <code>{E(model['integrity'].get('digest'))}</code> · "
         f"{E(model['integrity'].get('files'))} files · integrity {'OK' if model['integrity']['ok'] else 'FAILED'} · evidence verification {'OK' if (model.get('verification') or {}).get('ok') else 'FAILED'}</p>"]
    if model.get("refused"):
        h.append(f"<div class=bad>REPORT REFUSED: {E(model['refused'])}</div></body></html>"); return "".join(h)
    x, s, cfg = model["experiment"], model["summary"], model["config"]
    h.append(f"<div class=warn>Bundle kind (verbatim): {E(model['status_verbatim'])}. Limitations are listed at the end.</div>")
    h.append("<h2>Experiment summary</h2><table>" + "".join(f"<tr><th>{E(k)}</th><td>{E(_fmt(v))}</td></tr>" for k, v in (
        ("episodes", x["episodes"]), ("agent claimed success", f"{x['claimed_success']} of {x['episodes']}"), ("contract completion", f"{x['completed']} of {x['episodes']}"),
        ("effect safety: episodes with a duplicate", f"{x['with_duplicates']} of {x['episodes']}"), ("outcome integrity: false-success claims", f"{x['false_success_claims']} of {x['episodes']}"),
        ("faulted writes (lost_ack)", x["faulted_writes"]), ("agent-issued retries after the fault", x["agent_retries_after_fault"]),
        ("upstream model calls", x["upstream_model_calls"]), ("spend (USD)", f"{x['spend_usd']} (caps {_fmt(x['caps'])})"))) + "</table>")
    h.append("<h2>Configuration and environment</h2><table>" + "".join(f"<tr><th>{E(k)}</th><td><code>{E(_fmt(v))}</code></td></tr>" for k, v in (
        ("mode", s.get("mode")), ("model", s.get("model")), ("fault", cfg.get("condition")), ("episodes (policy text)", [e.get("policy") for e in cfg.get("episodes") or []]),
        ("instruction (verbatim)", cfg.get("instruction")), ("max steps", cfg.get("max_steps")), ("contract", model["contract_name"]),
        ("contract sha256", model["environment"].get("contract_sha256")), ("config sha256", model["environment"].get("config_sha256")),
        ("services", model["environment"].get("services")), ("images", model["environment"].get("images")), ("worker sha256", model["environment"].get("worker_sha256")),
        ("code sha256", model["environment"].get("code_sha256")))) + "</table>")
    for e in model["episodes"]:
        h.append(f"<h2>Episode {E(e['run_id'])}</h2><p class=small>target <code>{E(e['target'])}</code> · decoys <code>{E(_fmt(e['decoys']))}</code> · policy text: {E(e['policy'])} · category {E(e['category'])} · {E(e['seconds'])} s</p>")
        h.append("<h3>Findings (contract-v1 definitions, kept separate)</h3><table>" + "".join(f"<tr><th>{E(k)}</th><td>{E(v)}</td></tr>" for k, v in _findings(e)) + "</table>")
        h.append("<h3>Writes to the target, as the proxy recorded them</h3><table><tr><th>#</th><th>t (s)</th><th>status to the agent</th><th>fault</th><th>backend</th><th>issued by</th><th>agent's stated goal before it</th></tr>"
                 + "".join(f"<tr><td>{E(q['seq'])}</td><td>{E(_fmt(q['t']))}</td><td>{E(q['status_to_agent'])}</td><td>{E(q['intervention'])}</td><td>{E(q['backend'])}</td><td>{E(q['issued_by'])}</td><td>{E(_fmt(q['agent_step_goal']))}</td></tr>" for q in e["write_sequence"]) + "</table>")
        h.append(f"<p>Independent database oracle: <code>{E(_fmt(e['verified_by_object']))}</code> · proxy inference: <code>{E(_fmt(e['inferred_by_object']))}</code> · intended: {E(e['intent'])}. "
                 f"Other writes during the scenario (harness, before the agent): <code>{E(_fmt(e['harness_writes']))}</code></p>")
        h.append("<h3>Agent steps</h3><table><tr><th>step</th><th>t (s)</th><th>agent's evaluation of the previous step</th><th>next goal</th><th>actions</th><th>clicked</th><th>elements seen</th></tr>"
                 + "".join(f"<tr><td>{E(st['step'])}</td><td>{E(_fmt(st['t']))}</td><td>{E(_fmt(st['evaluation']))}</td><td>{E(_fmt(st['next_goal']))}</td><td>{'<br>'.join(E(a) for a in st['actions'])}</td><td>{E(', '.join(map(str, st['clicked'])))}</td><td>{E(st['elements_seen'])}</td></tr>" for st in e["steps"]) + "</table>")
        h.append("<h3>Model usage (the episode's gateway ledger rows)</h3><table><tr><th>t (s)</th><th>status</th><th>prompt tokens</th><th>completion tokens</th><th>finish</th><th>cost (USD)</th></tr>"
                 + "".join(f"<tr><td>{E(_fmt(u['t']))}</td><td>{E(u['status'])}</td><td>{E(u['prompt_tokens'])}</td><td>{E(u['completion_tokens'])}</td><td>{E(u['finish_reason'])}</td><td>{E(u['cost_usd'])}</td></tr>" for u in e["usage"])
                 + f"</table><p>Spend <b>${E(e['spend'].get('cost_usd'))}</b> of the ${E(e['cap'])} cap · {E(e['spend'].get('calls'))} upstream calls · {E(e['spend'].get('truncated'))} truncated at the output cap · "
                 f"{E(e['spend'].get('budget_refusals'))} gateway refusals · token revoked: {E(_yn(e['token_revoked']))} · live tokens after: {E(_fmt(e['live_tokens_after']))}</p>")
        h.append(f"<p class=small>In-episode isolation gate: {E(_yn(e['gate'].get('pass')))} · worker hash before/after matches: {E(_yn(e['worker_hash_match']))} · teardown leftovers: {E(_fmt(e['teardown_leftovers']))} · "
                 f"screenshots in the bundle: {E(', '.join(e['screenshots']))}</p>")
    sc = model["security"]
    h.append("<h2>Security results (sealed preflight + in-episode gates)</h2><table>" + "".join(f"<tr><th>{E(k)}</th><td><code>{E(_fmt(v))}</code></td></tr>" for k, v in (
        ("preflight checks", sc["preflight"]), ("worker: open", sc["worker_open"]), ("worker: closed", sc["worker_closed"]), ("browser: open", sc["browser_open"]),
        ("browser: closed", sc["browser_closed"]), ("confinement and cgroup limits", sc["confinement"]), ("gateway authorization", sc["gateway"]),
        ("provider key isolation", sc["key_isolation"]), ("in-episode isolation gates", sc["in_episode_gates"]))) + "</table>")
    h.append("<h2>Limitations</h2><ul>" + "".join(f"<li>{E(l)}</li>" for l in model["limitations"]) + "</ul>")
    h.append("<h2>Evidence integrity</h2><p>Manifest digest <code>" + E(model["integrity"].get("digest")) + "</code>; verified before rendering (manifest, then every episode's classification recomputed with the frozen scorer and spend recomputed from the ledger rows).</p><ul>"
             + "".join(f"<li><code>{E(v)}</code></li>" for v in model["verify"]) + "</ul></body></html>")
    return "".join(h)

def render_llm_md(model):
    from .report import M, C, _fmt
    L = ["# Agent Fire Drill: LLM-episode report", "", f"Bundle {C(model['bundle'])} · manifest digest {C(model['integrity'].get('digest'))} · integrity {'OK' if model['integrity']['ok'] else 'FAILED'} · "
         f"evidence verification {'OK' if (model.get('verification') or {}).get('ok') else 'FAILED'}", ""]
    if model.get("refused"): return "\n".join(L + [f"> **REPORT REFUSED: {M(model['refused'])}**"]) + "\n"
    x, s, cfg = model["experiment"], model["summary"], model["config"]
    L += [f"> **Bundle kind (verbatim): {M(model['status_verbatim'])}.** Limitations are listed at the end.", "", "## Experiment summary", "", "| | |", "|---|---|",
          f"| episodes | {x['episodes']} |", f"| agent claimed success | {x['claimed_success']} of {x['episodes']} |", f"| contract completion | {x['completed']} of {x['episodes']} |",
          f"| effect safety: episodes with a duplicate | {x['with_duplicates']} of {x['episodes']} |", f"| outcome integrity: false-success claims | {x['false_success_claims']} of {x['episodes']} |",
          f"| faulted writes (lost_ack) | {x['faulted_writes']} |", f"| agent-issued retries after the fault | {x['agent_retries_after_fault']} |",
          f"| upstream model calls | {x['upstream_model_calls']} |", f"| spend (USD) | {M(x['spend_usd'])} (caps {M(_fmt(x['caps']))}) |", "",
          "## Configuration and environment", "", f"- mode {C(s.get('mode'))}, model {C(s.get('model'))}, fault {C(cfg.get('condition'))}, policy text {C([e.get('policy') for e in cfg.get('episodes') or []])}, max steps {C(cfg.get('max_steps'))}",
          f"- instruction (verbatim): {M(cfg.get('instruction'))}", f"- contract: {M(model['contract_name'])} ({C(model['environment'].get('contract_sha256'))})",
          f"- services: {C(_fmt(model['environment'].get('services')))}", f"- worker sha256 {C(model['environment'].get('worker_sha256'))}", ""]
    for e in model["episodes"]:
        L += [f"## Episode {M(e['run_id'])}", "", f"Target {C(e['target'])} · policy text: {M(e['policy'])} · category {M(e['category'])} · {M(e['seconds'])} s", ""]
        L += [f"- **{M(k)}:** {M(v)}" for k, v in _findings(e)] + ["", "Writes to the target (proxy records):", "", "| # | t (s) | status to the agent | fault | backend | issued by |", "|---|---|---|---|---|---|"]
        L += [f"| {M(q['seq'])} | {M(_fmt(q['t']))} | {M(q['status_to_agent'])} | {M(q['intervention'])} | {M(q['backend'])} | {M(q['issued_by'])} |" for q in e["write_sequence"]]
        L += ["", f"Oracle {C(_fmt(e['verified_by_object']))} · proxy inference {C(_fmt(e['inferred_by_object']))} · intended {e['intent']}", "", "Agent steps:", ""]
        L += [f"{st['step']}. (t {M(_fmt(st['t']))} s) evaluation: {M(_fmt(st['evaluation']))} — next goal: {M(_fmt(st['next_goal']))} — clicked: {M(', '.join(map(str, st['clicked'])) or '—')}" for st in e["steps"]]
        L += ["", f"Model usage: {e['spend'].get('calls')} upstream calls, {e['spend'].get('truncated')} truncated at the output cap, {e['spend'].get('budget_refusals')} gateway refusals; spend **${M(e['spend'].get('cost_usd'))}** of ${M(e['cap'])}; "
              f"token revoked {_yn(e['token_revoked'])}; in-episode isolation gate {_yn(e['gate'].get('pass'))}; worker hash unchanged {_yn(e['worker_hash_match'])}; teardown leftovers {M(_fmt(e['teardown_leftovers']))}", ""]
    sc = model["security"]
    L += ["## Security results", "", f"- preflight: {C(_fmt(sc['preflight']))}", f"- worker open {C(sc['worker_open'])}, closed {sc['worker_closed']}; browser open {C(sc['browser_open'])}, closed {sc['browser_closed']}",
          f"- confinement and limits: {C(_fmt(sc['confinement']))}", f"- gateway authorization: {C(_fmt(sc['gateway']))}; key isolation: {C(_fmt(sc['key_isolation']))}",
          f"- in-episode isolation gates: {C(_fmt(sc['in_episode_gates']))}", "", "## Limitations", ""] + [f"- {M(l)}" for l in model["limitations"]]
    L += ["", "## Evidence integrity", "", f"Manifest digest {C(model['integrity'].get('digest'))}; verified before rendering.", ""] + [f"- {C(v)}" for v in model["verify"]]
    return "\n".join(L) + "\n"
