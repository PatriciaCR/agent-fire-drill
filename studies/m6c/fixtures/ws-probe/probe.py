"""Workspace probe (M6c fixture): runs FROM the mounted workspace; reports what it can read and whether it can write."""
import hashlib, json, os
out = {"cwd_listing": sorted(os.listdir("/afd/workspace"))}
out["self_sha256"] = hashlib.sha256(open("/afd/workspace/probe.py", "rb").read()).hexdigest()
for target in ("/afd/workspace/probe.py", "/afd/workspace/new-file"):
    try: open(target, "a").close(); out[f"write:{target}"] = "WRITABLE"
    except OSError as e: out[f"write:{target}"] = f"denied:{e.errno}"
try: os.chmod("/afd/workspace/probe.py", 0o777); out["chmod"] = "ALLOWED"
except OSError as e: out["chmod"] = f"denied:{e.errno}"
mounts = [l.split()[4] + " " + l.split()[5] for l in open("/proc/self/mountinfo") if l.split()[4].startswith(("/afd", "/opt", "/Users", "/home", "/var/lib"))]
out["mounts"] = mounts
print("AFD_RESULT_JSON=" + json.dumps({"claimed_success": True, "summary": json.dumps(out)}))
