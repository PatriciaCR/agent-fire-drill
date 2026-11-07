"""Thin, explicit Docker CLI wrapper. Every container gets the same hardening; nothing mounts the Docker socket."""
import json, subprocess
from . import config as C

class DockerError(RuntimeError): pass

def sh(*args, check=True, timeout=120, input=None):
    r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, input=input)
    if check and r.returncode != 0: raise DockerError(f"{' '.join(args[:4])}… failed: {r.stderr.strip()[-400:]}")
    return r

def hardening(role, uid):
    mem, cpus, pids, tmp = C.LIMITS[role]
    return ["--read-only", "--tmpfs", f"/tmp:rw,size={tmp},mode=1777", "--user", f"{uid}:{uid}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--memory", mem, "--memory-swap", mem, "--cpus", cpus, "--pids-limit", str(pids),
            "--ulimit", "nofile=4096:4096", "--init"]

def labels(job=None, role=None):
    out = ["--label", f"{C.LABEL}=1"]
    if job: out += ["--label", f"afd.job={job}"]
    if role: out += ["--label", f"afd.role={role}"]
    return out

def create_internal_network(name, subnet, job, internal=True):
    """internal=False is used only in rootless mode for the worker->gateway network: its only way out of the rootless
    netns is slirp4netns, whose host socket the host firewall restricts to the gateway address."""
    sh("docker", "network", "create", *(["--internal"] if internal else []), "--driver", "bridge", "--subnet", subnet, *labels(job, "net"), name)

def ip_of(container, network):
    return sh("docker", "inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}', container).stdout.strip()

def exists(kind, name):
    return sh("docker", kind, "inspect", name, check=False).returncode == 0  # kind: "container" | "network" | "image"

def remove_container(name):
    sh("docker", "rm", "-f", name, check=False)
    return not exists("container", name)

def remove_network(name):
    sh("docker", "network", "rm", name, check=False)
    return not exists("network", name)

def labelled(kind, label):
    if kind == "container":
        r = sh("docker", "ps", "-a", "--filter", f"label={label}", "--format", "{{.Names}}", check=False)
    else:
        r = sh("docker", "network", "ls", "--filter", f"label={label}", "--format", "{{.Name}}", check=False)
    return [x for x in r.stdout.split() if x]

def inspect(name):
    return json.loads(sh("docker", "container", "inspect", name).stdout)[0]

def inspect_network(name):
    return json.loads(sh("docker", "network", "inspect", name).stdout)[0]
