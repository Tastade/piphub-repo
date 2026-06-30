#!/usr/bin/env python3
"""Строит PEP 503 Simple Repository Index из папки packages/"""

import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone

REPO_ROOT    = Path(__file__).parent.parent
PACKAGES_DIR = REPO_ROOT / "packages"
SIMPLE_DIR   = REPO_ROOT / "simple"
META_FILE    = REPO_ROOT / "mirror_meta.json"

SIMPLE_DIR.mkdir(exist_ok=True)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def pkg_name_from_filename(filename):
    if filename.endswith(".whl"):
        parts = filename.split("-")
        if len(parts) >= 2:
            return parts[0]
    for ext in (".tar.gz", ".zip", ".tar.bz2"):
        if filename.endswith(ext):
            base = filename[:-len(ext)]
            idx = base.rfind("-")
            if idx > 0:
                return base[:idx]
    return None

packages = {}

for item in sorted(PACKAGES_DIR.rglob("*")):
    if not item.is_file():
        continue
    if not any(item.name.endswith(e) for e in (".whl", ".tar.gz", ".zip", ".tar.bz2")):
        continue
    raw_name = pkg_name_from_filename(item.name)
    if not raw_name:
        continue
    norm = normalize(raw_name)
    digest = sha256_file(item)
    rel_path = item.relative_to(REPO_ROOT)
    href = "../../" + str(rel_path).replace("\\", "/")
    packages.setdefault(norm, []).append({
        "filename": item.name,
        "href": href,
        "sha256": digest,
        "size": item.stat().st_size,
    })

print(f"Найдено {len(packages)} пакетов.")

for norm, files in packages.items():
    pkg_dir = SIMPLE_DIR / norm
    pkg_dir.mkdir(exist_ok=True)
    links = "\n".join(
        f'    <a href="{f["href"]}#sha256={f["sha256"]}">{f["filename"]}</a><br>'
        for f in sorted(files, key=lambda x: x["filename"])
    )
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Links for {norm}</title></head>
<body>
  <h1>Links for {norm}</h1>
{links}
</body>
</html>
"""
    (pkg_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"  ✓ /simple/{norm}/")

meta = {
    "generated": datetime.now(timezone.utc).isoformat(),
    "packages": {
        norm: [{"filename": f["filename"], "sha256": f["sha256"], "size": f["size"]}
               for f in files]
        for norm, files in packages.items()
    }
}
META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"mirror_meta.json записан.")
