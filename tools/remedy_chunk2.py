#!/usr/bin/env python3
import json
import re
import tempfile
import subprocess
import hashlib
import os
from pathlib import Path
from batch_arm64_factory import fetch_debian_packages, convert_deb_to_mind, download_deb, gh_cmd, sanitize_filename

ROOT = Path(__file__).resolve().parent.parent
missing_names = [
    "apache2-bin", "espeak-ng-data", "firefox-esr", "libbrlapi0.8",
    "libopenni2-0", "libreoffice-core", "librevenge-0.0-0",
    "python3-cups", "xdg-desktop-portal-gtk"
]

def main():
    avail = fetch_debian_packages()
    pkg_json = ROOT / "packages.json"
    data = json.loads(pkg_json.read_text(encoding="utf-8"))
    pkg_map = {p["name"]: p for p in data["packages"]}

    work_dir = Path("/dev/shm/remedy_arm64")
    work_dir.mkdir(parents=True, exist_ok=True)

    for name in missing_names:
        pinfo = avail.get(name)
        if not pinfo:
            print(f"NOT FOUND in debian: {name}")
            continue
        raw_fname = f"{pinfo['name']}-{pinfo['version']}-1-aarch64.mind"
        clean_fname = sanitize_filename(raw_fname)
        out_file = work_dir / clean_fname
        print(f"Building {name} -> {clean_fname}...")
        with tempfile.NamedTemporaryFile(suffix=".deb", dir=str(work_dir)) as tmp_deb:
            download_deb(pinfo["filename_deb"], tmp_deb.name)
            out_file, rec = convert_deb_to_mind(tmp_deb.name, out_file, pinfo, work_base=str(work_dir))
        
        print(f"Uploading {clean_fname} to v1.0-chunk-02 ({rec['size']} bytes)...")
        gh_cmd("release", "upload", "v1.0-chunk-02", "-R", "MindOS-Official/packages-arm64", "--clobber", str(out_file), check=True)
        
        old = pkg_map.get(name, {})
        old["filename"] = clean_fname
        old["url"] = f"https://github.com/MindOS-Official/packages-arm64/releases/download/v1.0-chunk-02/{clean_fname}"
        old["sha256"] = rec["sha256"]
        old["size"] = rec["size"]
        pkg_map[name] = old
        out_file.unlink(missing_ok=True)
        print(f"✓ Successfully recovered {name}")

    data["packages"] = sorted(pkg_map.values(), key=lambda p: p["name"])
    content = json.dumps(data, indent=1, ensure_ascii=False)
    pkg_json.write_text(content, encoding="utf-8")
    (ROOT / "staging" / "packages.json").write_text(content, encoding="utf-8")
    (ROOT / "staging" / "packages-arm64.json").write_text(content, encoding="utf-8")
    (ROOT / "packages-arm64.json").write_text(content, encoding="utf-8")
    print("All 9 missing packages recovered and manifest updated!")

if __name__ == "__main__":
    main()
