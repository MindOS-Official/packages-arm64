#!/usr/bin/env python3
"""MindOS Index Normalizer.

Ensures all package filenames and URLs in packages.json and staging/
use sanitized characters (matching GitHub Releases naming).
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def sanitize(name):
    return re.sub(r"[^A-Za-z0-9._+-]", ".", name)

def normalize_file(fpath):
    if not fpath.exists():
        return
    try:
        data = json.loads(fpath.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to read {fpath}: {e}")
        return

    repo = "MindOS-Official/packages-arm64"
    changed = 0
    for p in data.get("packages", []):
        old_fn = p.get("filename", "")
        new_fn = sanitize(old_fn)
        chunk_tag = p.get("chunk_tag", "v1.0-chunk-01")
        new_url = f"https://github.com/{repo}/releases/download/{chunk_tag}/{new_fn}"
        if old_fn != new_fn or p.get("url") != new_url:
            p["filename"] = new_fn
            p["url"] = new_url
            changed += 1

    if changed > 0:
        data["packages"] = sorted(data["packages"], key=lambda p: p["name"])
        content = json.dumps(data, indent=1, ensure_ascii=False)
        fpath.write_text(content, encoding="utf-8")
        print(f"✓ {fpath.name}: normalized {changed} package URLs/filenames.")
    else:
        print(f"ℹ {fpath.name}: all entries already normalized.")

def main():
    for rel in ["packages.json", "packages-arm64.json", "staging/packages.json", "staging/packages-arm64.json"]:
        normalize_file(ROOT / rel)

if __name__ == "__main__":
    main()
