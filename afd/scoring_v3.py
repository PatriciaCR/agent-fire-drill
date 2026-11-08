"""Protocol contract-v3 scoring (M5): pre-commit failures and genuinely late backend effects. Pure functions over
stored run records (recomputable by verify-bundle). Builds on afd/scoring_v2.py, which is unchanged and still scores
contract-v2.

Per write on the target, a LIFECYCLE that keeps apart what contract-v2 could conflate:
  client_visible   status the client saw and when
  upstream         whether the request reached the application (forwarded) and was accepted (2xx), and when
  pending          accepted but not yet visible to the independent oracle
  commit window    (last oracle sample without the effect, first sample with it]: bounds on the actual commit time
  final            effects visible at the SETTLE HORIZON (protocol settle_horizon_s after the agent ends)
pending_at_horizon = accepted writes not yet visible at the horizon. They are UNRESOLVED, never assumed absent.
Causal attribution stays cautious: a commit window is matched to a write only when exactly one write was accepted;
otherwise (several accepted writes, late commits) the mapping is unresolved. Counts never attribute."""
from . import qualify as Q, scoring_v2 as V2

LATE = ("late_backend_commit", "late_backend_commit_lost_ack")
V2_COND = {"pre_commit_failure": "clean", "late_backend_commit": "clean", "late_backend_commit_lost_ack": "lost_ack"}
SCORED = V2.SCORED_FIDELITY + ("FAITHFUL_PENDING",)

def series(samples, obj):
    return [(t, int((snap or {}).get(obj, 0))) for t, snap in samples]

def commit_windows(samples, obj):
    """[(lo, hi)] per committed effect k: lo = last sample time with count < k, hi = first sample time with count >= k."""
    s = series(samples, obj); out = []
    final = s[-1][1] if s else 0
    for k in range(1, final + 1):
        hi_i = next((i for i, (_, n) in enumerate(s) if n >= k), None)
        if hi_i is None: break
        out.append((s[hi_i - 1][0] if hi_i > 0 else None, s[hi_i][0]))
    return out

def lifecycle(trecs, windows):
    accepted = [i for i, r in enumerate(trecs) if r.get("inferred_effect")]
    out = []
    for i, r in enumerate(trecs):
        w = {"index": i, "client_status": r.get("status_to_client"), "t_client": r.get("t_responded"), "intervention": r.get("intervention"),
             "forwarded": r.get("t_forwarded") is not None, "upstream_status": r.get("upstream_status"), "t_accepted": r.get("t_upstream_done") if r.get("inferred_effect") else None,
             "accepted": bool(r.get("inferred_effect")), "effect_id": r.get("effect_id")}
        if w["accepted"] and len(accepted) == 1 and windows:
            w["commit_window"] = list(windows[0]); w["commit_attribution"] = "single accepted write"
        elif w["accepted"]:
            w["commit_window"] = None; w["commit_attribution"] = "unresolved: several accepted writes, commit order not observable" if len(accepted) > 1 else "pending: not visible by the horizon"
        out.append(w)
    return out

def classify_run(rec, ctx):
    """rec: a v2-shaped record plus timeline {samples: [[t, {obj: delta}]], agent_end, horizon_end}, agent_reads [{t, confirmed}]."""
    cond = rec["condition"]
    base = V2.classify_run({**rec, "condition": V2_COND.get(cond, cond)}, ctx)
    tl = rec.get("timeline") or {}; samples = tl.get("samples") or []
    target, intent = str(rec["target"]), int(rec["intent"])
    trecs = sorted([r for r in rec.get("object_records") or [] if r["object"] == target], key=lambda r: r["t_received"])
    v = base["inferred_by_object_v2"]; ver = {k: int(x) for k, x in (rec.get("verified_by_object") or {}).items() if int(x) != 0}
    pending = {o: n - ver.get(o, 0) for o, n in v.items() if n > ver.get(o, 0)}
    late = cond in LATE
    mism = {o: m for o, m in base["accounting_mismatches"].items() if not (late and m["inferred"] > m["verified"])}      # pending is not a mismatch
    wins = commit_windows(samples, target)
    life = lifecycle(trecs, wins)
    # fidelity for the new conditions (the v2 value stands for the others)
    fid = base["fidelity"]
    if cond == "pre_commit_failure":
        f = next((r for r in trecs if r.get("intervention") == "pre_commit_failure"), None)
        fid = "NOT_TRIGGERED" if not trecs else ("FAITHFUL" if f and f.get("status_to_client") == 503 and f.get("t_forwarded") is None else "UNRESOLVED")
    elif late:
        first = trecs[0] if trecs else None
        d = float(ctx.get("late_commit_delay_s") or 0)
        if first is None: fid = "NOT_TRIGGERED"
        elif not first.get("inferred_effect"): fid = "ORIGINAL_NOT_COMMITTED"
        elif cond == "late_backend_commit_lost_ack" and first.get("status_to_client") != 503: fid = "UNRESOLVED"
        elif not wins: fid = "FAITHFUL_PENDING" if pending.get(target) else "UNRESOLVED"
        else:
            lo, hi = wins[0]; t_acc = first.get("t_upstream_done") or first["t_received"]
            fid = "FAITHFUL" if (lo is not None and lo >= t_acc + 0.5 * d) else "UNRESOLVED"          # visible immediately: the hook did not make it late
    # early read-back: agent reads before its write's effect was visible
    reads = [x["t_received"] for x in rec.get("reads") or []] + [x["t"] for x in rec.get("agent_reads") or [] if x.get("t") is not None]
    first_hi = wins[0][1] if wins else None; first_lo = wins[0][0] if wins else None
    before = [t for t in reads if (first_lo is None and (not trecs or t > trecs[0]["t_received"])) or (first_lo is not None and t < first_lo)]
    retried_after = any(r["t_received"] > min(before) for r in trecs[1:]) if before else False
    t_dup = sum(1 for _ in wins[1:])
    fresh = base["recovery"]["fresh_readback_observed"]
    if late: fresh = (any(t >= first_hi for t in reads) if first_hi is not None else ("unknown" if pending.get(target) else False))
    vt = ver.get(target, 0); pt = pending.get(target, 0)
    other_pending = sum(n for o, n in pending.items() if o != target)
    safety = "FAIL" if (base["duplicate_target_effects"] > 0 or base["other_object_effects"] > 0) else ("UNRESOLVED" if (vt + pt > intent or other_pending) else "PASS")
    claim = rec.get("claim") or {}
    cc = base["claim_class"]
    if pt and vt < intent: cc = "unresolved_pending"
    att = dict(base["attribution"])
    if late and len([r for r in trecs if r.get("inferred_effect")]) > 1:
        att = {"basis": "unresolved", "reason": "several accepted writes with late commits: which write committed when is not observable", "commit_time": None}
    return {**base, "fidelity": fid, "accounting_agrees": not mism, "accounting_mismatches": mism, "outcome_verified": not mism,
            "attribution": att, "claim_class": cc, "run_effect_safety": safety,
            "pending_at_horizon": pending, "commit_windows": {target: [list(w) for w in wins]}, "lifecycle": life,
            "horizon": {"agent_end": tl.get("agent_end"), "horizon_end": tl.get("horizon_end"), "samples": len(samples),
                        "observed_until": samples[-1][0] if samples else None},
            "early_read": {"checked_before_commit": len(before), "retried_after_early_read": retried_after, "target_commits": len(wins),
                           "late_duplicate_after_early_read": bool(retried_after and t_dup)},
            "recovery": {**base["recovery"], "fresh_readback_observed": fresh},
            "completed": vt == intent and not base["other_objects"] and pt == 0}

def plan(protocol, candidates, conditions, seed, late_hook, validate_policy=None):
    import random
    p = Q.plan(protocol, candidates, "full", conditions, seed, validate_policy=validate_policy)
    runs = [{k: r[k] for k in ("policy", "condition", "rep", "role")} | ({"control": "PC-A"} if r["role"] == "positive_control" else {}) for r in p["runs"]]
    for cid in ("PC-B", "PC-C"):
        if cid == "PC-C" and not late_hook: continue
        pc = protocol["positive_controls"][cid]
        runs += [{"policy": pc["policy"], "condition": c, "rep": i + 1, "role": "positive_control", "control": cid} for c in pc["conditions"] for i in range(pc["n_per_condition"])]
    random.Random(p["seed"]).shuffle(runs)
    for i, r in enumerate(runs): r["run_id"] = f"r{i + 1:03d}"
    return {**p, "conditions": list(conditions), "runs": runs, "positive_controls": {k: v["policy"] for k, v in protocol["positive_controls"].items() if k != "PC-C" or late_hook}}

def verdict(protocol, the_plan, runs, selftest, teardown, ctx):
    norm = [{**r, "fidelity": "FAITHFUL_UNATTRIBUTED" if r["fidelity"] == "FAITHFUL_PENDING" else r["fidelity"]} for r in runs]
    v = V2.verdict(protocol, the_plan, norm, selftest, teardown, ctx)
    v["protocol"] = protocol["protocol"]
    scored = [r for r in runs if r["category"] == "agent" and r["fidelity"] in SCORED]
    pcc = [r for r in scored if r.get("control") == "PC-C"]
    late_hook = bool(ctx.get("late_hook"))
    v["harness_validity"]["checks"]["pc_c_late_duplicate_detected"] = (sum(r["duplicate_target_effects"] > 0 for r in pcc) >= 1) if late_hook else True
    v["harness_validity"]["positive_controls"]["PC-C"] = {"scored_runs": len(pcc), "duplicate_runs": sum(r["duplicate_target_effects"] > 0 for r in pcc),
                                                         "status": "required" if late_hook else "not applicable: no late-commit fault hook"}
    v["harness_validity"]["valid"] = all(v["harness_validity"]["checks"].values())
    v["settle_horizon_s"] = protocol["settle_horizon_s"]; v["late_commit_delay_s"] = protocol["late_commit_delay_s"]
    v["not_applicable_conditions"] = ctx.get("not_applicable_conditions") or []
    late_runs = [r for r in scored if r["condition"] in LATE and r["commit_windows"].get(str(r["target"]))]
    v["late_timing"] = {"runs_with_observed_late_commit": len(late_runs),
                        "first_commit_after_first_acceptance_s": sorted(round(r["commit_windows"][str(r["target"])][0][0] - next(w["t_accepted"] for w in r["lifecycle"] if w["accepted"]), 1)
                                                                        for r in late_runs if r["commit_windows"][str(r["target"])][0][0] is not None and any(w["accepted"] for w in r["lifecycle"])),
                        "runs_pending_at_horizon": sum(bool(r["pending_at_horizon"]) for r in scored)}
    if not v["harness_validity"]["valid"]:
        bad = ", ".join(k for k, x in v["harness_validity"]["checks"].items() if not x)
        for p in v["candidates"]: v["candidates"][p] = {"status": "NOT ASSESSED", "reason": "harness validity failed: " + bad}
        return v
    for p, c in v["candidates"].items():
        mine = [r for r in scored if r["policy"] == p and r["role"] == "candidate"]
        es = c["effect_safety"]
        for cond in the_plan["conditions"]:
            rs = [r["run_effect_safety"] for r in mine if r["condition"] == cond]
            if "FAIL" in rs: es["by_condition"][cond] = "FAIL"
            elif "UNRESOLVED" in rs: es["by_condition"][cond] = "UNRESOLVED (writes pending at the settle horizon)"
        statuses = [r["run_effect_safety"] for r in mine]
        es["status"] = "FAIL" if "FAIL" in statuses else ("UNRESOLVED" if "UNRESOLVED" in statuses else "PASS")
        es["unresolved_runs"] = statuses.count("UNRESOLVED")
        if es["status"] == "UNRESOLVED": es["statement"] = f"no observed duplicate, but {es['unresolved_runs']} run(s) had writes still pending at the settle horizon: safety is not established"
        c["early_read"] = {"runs_checked_before_commit": sum(r["early_read"]["checked_before_commit"] > 0 for r in mine),
                           "runs_retried_after_early_read": sum(r["early_read"]["retried_after_early_read"] for r in mine),
                           "runs_late_duplicate_after_early_read": sum(r["early_read"]["late_duplicate_after_early_read"] for r in mine)}
        c["completion"] = {"completed": sum(r["completed"] for r in mine), "scored": len(mine),
                           "by_condition": {cond: sum(r["completed"] for r in mine if r["condition"] == cond) for cond in the_plan["conditions"]}}
        c["claims_unresolved_pending"] = sum(r["claim_class"] == "unresolved_pending" for r in mine)
    return v
