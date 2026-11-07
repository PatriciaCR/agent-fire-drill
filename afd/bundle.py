"""Evidence bundles: a directory of plain files plus MANIFEST.sha256 (sha256sum format, one line per file, sorted) and
BUNDLE_ID.txt. The bundle digest is the sha256 of the manifest. Anyone can verify a bundle with `sha256sum -c MANIFEST.sha256`
or `python3 -m afd.bundle verify <dir|tar.gz>`; verify() also rejects files that are not listed."""
import hashlib, io, json, os, sys, tarfile, tempfile
from pathlib import Path

MANIFEST = "MANIFEST.sha256"

def sha256_bytes(b): return hashlib.sha256(b).hexdigest()
def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def _files(d):
    d = Path(d)
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and p.name != MANIFEST)

def seal(d):
    """Write MANIFEST.sha256 for every file under d; return the bundle digest."""
    d = Path(d)
    lines = [f"{sha256_file(d / f)}  {f}" for f in _files(d)]
    (d / MANIFEST).write_text("\n".join(lines) + "\n")
    return sha256_bytes((d / MANIFEST).read_bytes())

def verify(d):
    """Return {ok, digest, files, problems}. Accepts a bundle directory or a .tar.gz of one."""
    if str(d).endswith((".tar.gz", ".tgz")):
        with tempfile.TemporaryDirectory() as t:
            with tarfile.open(d, "r:gz") as tf:
                for m in tf.getmembers():
                    if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts:
                        return {"ok": False, "problems": [f"unsafe archive member: {m.name}"]}
                tf.extractall(t, filter="data")
            subs = [p for p in Path(t).iterdir() if p.is_dir()]
            return verify(subs[0] if len(subs) == 1 and not (Path(t) / MANIFEST).exists() else t)
    d = Path(d); problems = []
    if not (d / MANIFEST).exists(): return {"ok": False, "problems": ["no manifest"]}
    listed = {}
    for line in (d / MANIFEST).read_text().splitlines():
        if not line.strip(): continue
        h, f = line.split("  ", 1); listed[f] = h
    present = set(_files(d))
    for f in sorted(set(listed) - present): problems.append(f"missing: {f}")
    for f in sorted(present - set(listed)): problems.append(f"unlisted: {f}")
    for f in sorted(set(listed) & present):
        if sha256_file(d / f) != listed[f]: problems.append(f"modified: {f}")
    return {"ok": not problems, "digest": sha256_bytes((d / MANIFEST).read_bytes()), "files": len(listed), "problems": problems}

def archive(d, out):
    """Deterministic-ish tar.gz of the bundle directory (sorted members, fixed mtime/owner)."""
    d = Path(d)
    with tarfile.open(out, "w:gz") as tf:
        for f in [MANIFEST] + _files(d):
            data = (d / f).read_bytes(); ti = tarfile.TarInfo(f"{d.name}/{f}"); ti.size = len(data); ti.mtime = 0; ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    return out

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "verify":
        r = verify(sys.argv[2]); print(json.dumps(r, indent=1)); sys.exit(0 if r["ok"] else 1)
    sys.exit("usage: python3 -m afd.bundle verify <bundle dir | bundle.tar.gz>")
