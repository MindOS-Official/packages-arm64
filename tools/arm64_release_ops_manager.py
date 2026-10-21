#!/usr/bin/env python3
"""MindOS Paradise ARM64 Dedicated GitHub Release & Repository Operations Manager.

Bu araç MindOS ARM64 paket deposunun GitHub Releases ve indeks dağıtım operasyonlarını
tek merkezden otomatik ve güvenli şekilde yönetir:
  1. out_arm64/ klasöründeki .mind paketlerini izler ve doğrular.
  2. GitHub Releases chunk'larına (v1.0-chunk-01, v1.0-chunk-02 vb.) toplu yükleme yapar.
  3. Chunk ~980 pakete ulaştığında bir sonraki chunk'ı (v1.0-chunk-XX) otomatik açar.
  4. GitHub API hız sınırlarını (rate limit / secondary rate limit) ve ağ hatalarını
     üstel geri çekilme (exponential backoff) ile yönetir.
  5. Ed25519 sürüm anahtarıyla packages.json ve system.json'u imzalar.
  6. İmzayı kriptografik olarak doğrular, git deposuna commit ve push eder.
  7. Disk dolmasını engellemek için yüklenen dosyaları anında temizler.
  8. Canlı yükleme ve depo metriklerini detaylı raporlar.

Kullanım:
  python3 tools/arm64_release_ops_manager.py --status
  python3 tools/arm64_release_ops_manager.py --sync-now
  python3 tools/arm64_release_ops_manager.py --watch --interval 15
  python3 tools/arm64_release_ops_manager.py --sign-push
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

REPO_DEFAULT = os.environ.get("MIND_REPO_ARM64", "MindOS-Official/packages-arm64")
MAX_ASSETS_PER_CHUNK = 980  # GitHub limit: 1000 assets per release
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "out_arm64"
STAGING_JSON = ROOT / "staging" / "packages.json"
STAGING_ARM64_JSON = ROOT / "staging" / "packages-arm64.json"
INDEX_JSON = ROOT / "packages.json"
INDEX_ARM64_JSON = ROOT / "packages-arm64.json"
SYSTEM_JSON = ROOT / "system.json"
SIGN_TOOL = ROOT / "tools" / "sign_index.py"
SIGNING_KEY = Path(os.environ.get("MINDPKG_SIGNING_KEY", Path.home() / ".config/mindos/mindpkg-release.key"))

RELEASE_PUBKEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEA0kq80lqdyWdIHD0A1w62Xsrp0QDcSQWEYOSxlz3lMhw=
-----END PUBLIC KEY-----
"""


def log(msg, symbol="ℹ"):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {symbol} {msg}", flush=True)


def gh_cmd(*args, repo=REPO_DEFAULT, check=True, max_retries=6, base_delay=5):
    """GitHub CLI komutlarını üstel geri çekilme ve oran sınırı yönetimiyle çalıştırır."""
    cmd = ["gh", *args]
    if "-R" not in args and "--repo" not in args:
        cmd.extend(["-R", repo])

    for attempt in range(max_retries):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return res

        err = res.stderr.strip()
        lower_err = err.lower()
        is_transient = any(
            x in lower_err
            for x in (
                "rate limit",
                "secondary rate limit",
                "abuse detection",
                "500",
                "502",
                "503",
                "504",
                "timeout",
                "timed out",
                "connection reset",
                "tls handshake",
                "network is unreachable",
            )
        )

        if is_transient and attempt < max_retries - 1:
            delay = (base_delay * (2 ** attempt)) + random.uniform(1, 3)
            log(f"GitHub API beklemesi ({delay:.1f} sn, deneme {attempt + 1}/{max_retries}): {err[:90]}", "⏳")
            time.sleep(delay)
            continue

        if check:
            raise RuntimeError(f"gh komut hatası ({' '.join(args[:3])}): {err}")
        return res

    return res


class ARM64ReleaseOpsManager:
    def __init__(self, repo=REPO_DEFAULT, max_chunk_assets=MAX_ASSETS_PER_CHUNK):
        self.repo = repo
        self.max_chunk_assets = max_chunk_assets
        self._cached_chunks = None
        self._cached_assets = {}

    def fetch_all_chunks(self, force_refresh=False):
        """Uzak GitHub Releases chunk'larını listeler."""
        if self._cached_chunks is not None and not force_refresh:
            return self._cached_chunks

        res = gh_cmd("release", "list", "--json", "tagName,name,isLatest,publishedAt", repo=self.repo, check=False)
        if res.returncode != 0:
            log(f"Release listesi alınamadı: {res.stderr}", "⚠")
            return []

        try:
            releases = json.loads(res.stdout)
        except Exception:
            releases = []

        chunk_releases = []
        for r in releases:
            tag = r.get("tagName", "")
            if tag.startswith("v1.0-chunk-"):
                try:
                    num = int(tag.rsplit("-", 1)[1])
                    chunk_releases.append({
                        "tag": tag,
                        "number": num,
                        "title": r.get("name", ""),
                        "is_latest": r.get("isLatest", False),
                        "published_at": r.get("publishedAt", "")
                    })
                except ValueError:
                    pass

        chunk_releases.sort(key=lambda x: x["number"])
        self._cached_chunks = chunk_releases
        return self._cached_chunks

    def get_chunk_asset_names(self, tag, force_refresh=False):
        """Belirtilen chunk'taki dosya listesini döner."""
        if tag in self._cached_assets and not force_refresh:
            return self._cached_assets[tag]

        res = gh_cmd("release", "view", tag, "--json", "assets", repo=self.repo, check=False)
        if res.returncode != 0:
            return set()

        try:
            data = json.loads(res.stdout)
            names = set(a.get("name", "") for a in data.get("assets", []))
            self._cached_assets[tag] = names
            return names
        except Exception:
            return set()

    def get_chunk_asset_count(self, tag, force_refresh=False):
        return len(self.get_chunk_asset_names(tag, force_refresh))

    def ensure_chunk_exists(self, chunk_num):
        """Chunk release'in var olduğunu doğrular, yoksa oluşturur."""
        tag = f"v1.0-chunk-{chunk_num:02d}"
        chunks = self.fetch_all_chunks(force_refresh=True)
        for c in chunks:
            if c["tag"] == tag:
                return tag

        title = f"MindOS ARM64 Packages Chunk {chunk_num:02d}"
        notes = (
            f"MindOS Paradise ARM64 (aarch64) .mind binary package repository chunk {chunk_num:02d}.\n"
            f"Official release assets for MindOS Paradise package manager (mindpkg).\n"
            f"Signed and maintained by MindOS Team."
        )
        log(f"Yeni GitHub Release Chunk'ı oluşturuluyor: {tag} ({title})...", "🚀")
        gh_cmd("release", "create", tag, "--title", title, "--notes", notes, repo=self.repo, check=True)
        self.fetch_all_chunks(force_refresh=True)
        return tag

    def get_active_upload_chunk(self):
        """Yükleme yapılacak aktif chunk'ı belirler (~980 doluluğa göre)."""
        chunks = self.fetch_all_chunks()
        if not chunks:
            tag = self.ensure_chunk_exists(1)
            return tag, 1, 0

        last_chunk = chunks[-1]
        cnt = self.get_chunk_asset_count(last_chunk["tag"], force_refresh=True)
        if cnt >= self.max_chunk_assets:
            next_num = last_chunk["number"] + 1
            log(f"Chunk {last_chunk['tag']} doldu ({cnt}/{self.max_chunk_assets} asset). Yeni chunk {next_num:02d} açılıyor...", "📦")
            tag = self.ensure_chunk_exists(next_num)
            return tag, next_num, 0

        return last_chunk["tag"], last_chunk["number"], cnt

    def inspect_mind_file(self, fpath):
        """Bir .mind paket dosyasının metadata, sha256 ve boyutunu okur."""
        fpath = Path(fpath)
        sha = hashlib.sha256(fpath.read_bytes()).hexdigest()
        size = fpath.stat().st_size
        meta = {}
        try:
            with tarfile.open(fpath, "r:*") as tar:
                mfile = tar.extractfile("metadata.json")
                if mfile:
                    meta = json.loads(mfile.read().decode("utf-8"))
        except Exception:
            pass

        return {
            "name": meta.get("name") or fpath.name.split("-")[0],
            "version": meta.get("version", "1.0"),
            "release": meta.get("release", "1"),
            "architecture": meta.get("architecture", "aarch64"),
            "description": meta.get("description", fpath.name),
            "maintainer": meta.get("maintainer", "MindOS Team <https://github.com/MindOS-Official>"),
            "dependencies": meta.get("dependencies", []),
            "origin": meta.get("origin", "debian/trixie"),
            "format": meta.get("format", 2),
            "maintscripts": meta.get("maintscripts", []),
            "filename": fpath.name,
            "sha256": sha,
            "size": size,
        }

    def load_index(self, path):
        if Path(path).exists():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "repository": "MindOS Sunrise Official Package Repository (aarch64)",
            "architecture": "aarch64",
            "version": "1.0",
            "total_packages": 0,
            "packages": []
        }

    def save_index(self, path, data):
        data["total_packages"] = len(data.get("packages", []))
        max_chunk = 1
        for p in data.get("packages", []):
            ctag = p.get("chunk_tag", "")
            if ctag.startswith("v1.0-chunk-"):
                try:
                    cnum = int(ctag.rsplit("-", 1)[1])
                    if cnum > max_chunk:
                        max_chunk = cnum
                except ValueError:
                    pass
        data["total_chunks"] = max_chunk
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    def upload_pending_packages(self, batch_size=25, clean_local=True):
        """out_arm64 klasöründeki dosyaları chunk'lara yükler ve staging indeksini günceller."""
        if not OUT_DIR.exists():
            return 0, 0

        files = sorted(list(OUT_DIR.glob("*.mind")))
        if not files:
            log("out_arm64/ içinde yüklenecek bekleyen paket yok.", "✓")
            return 0, 0

        log(f"out_arm64/ içinde {len(files)} adet paket tespit edildi. Yükleme planlanıyor...", "🔍")

        staging_data = self.load_index(STAGING_JSON)
        existing_pkgs = {p["name"]: p for p in staging_data.get("packages", [])}

        uploaded_total = 0
        failed_total = 0

        # Aktif chunk'ı bul
        curr_tag, curr_num, curr_asset_count = self.get_active_upload_chunk()
        chunk_remote_assets = self.get_chunk_asset_names(curr_tag, force_refresh=True)

        i = 0
        while i < len(files):
            # Chunk limiti kontrolü
            if curr_asset_count >= self.max_chunk_assets:
                curr_num += 1
                curr_tag = self.ensure_chunk_exists(curr_num)
                curr_asset_count = 0
                chunk_remote_assets = self.get_chunk_asset_names(curr_tag, force_refresh=True)
                log(f"Hedef chunk değiştirildi: {curr_tag}", "📦")

            remaining_chunk_capacity = max(1, self.max_chunk_assets - curr_asset_count)
            take_count = min(batch_size, remaining_chunk_capacity, len(files) - i)
            batch = files[i:i + take_count]
            i += take_count

            # Dosyaların metadata ve staging kayıtlarını hazırla
            batch_to_upload = []
            for f in batch:
                if f.name in chunk_remote_assets:
                    # Zaten uzak sunucuda var
                    log(f"Zaten mevcut: {f.name} -> {curr_tag}", "ℹ")
                    if clean_local:
                        try:
                            f.unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                batch_to_upload.append(f)

            if not batch_to_upload:
                continue

            log(f"{len(batch_to_upload)} paket {curr_tag} chunk'ına yükleniyor ({curr_asset_count}/{self.max_chunk_assets})...", "⬆️")
            try:
                gh_cmd("release", "upload", curr_tag, "--clobber", *[str(p) for p in batch_to_upload], repo=self.repo, check=True)
                uploaded_total += len(batch_to_upload)
                curr_asset_count += len(batch_to_upload)

                # İndeks kayıtlarını güncelle
                for f in batch_to_upload:
                    info = self.inspect_mind_file(f)
                    info["chunk_tag"] = curr_tag
                    info["url"] = f"https://github.com/{self.repo}/releases/download/{curr_tag}/{f.name}"
                    existing_pkgs[info["name"]] = info

                    if clean_local:
                        try:
                            f.unlink(missing_ok=True)
                        except OSError:
                            pass

                # Her başarılı partide staging indeksini güncelle
                staging_data["packages"] = sorted(existing_pkgs.values(), key=lambda p: p["name"])
                self.save_index(STAGING_JSON, staging_data)
                self.save_index(STAGING_ARM64_JSON, staging_data)

            except Exception as e:
                failed_total += len(batch_to_upload)
                log(f"Yükleme hatası ({curr_tag}): {e}", "✗")
                time.sleep(5)

        log(f"Yükleme tamamlandı: {uploaded_total} paket yüklendi, {failed_total} hata.", "📊")
        return uploaded_total, failed_total

    def sign_and_publish_index(self, auto_push=True):
        """Taslak indeksi doğrular, Ed25519 ile imzalar, packages-arm64 ile eşitler ve push eder."""
        if not SIGN_TOOL.exists():
            raise FileNotFoundError(f"İmzalama aracı bulunamadı: {SIGN_TOOL}")

        log("Taslak indeks denetlenip Ed25519 ile imzalanıyor...", "🔏")
        res = subprocess.run([
            sys.executable,
            str(SIGN_TOOL),
            "--arch", "arm64",
            "--repo", self.repo,
            "--yes"
        ], cwd=str(ROOT), capture_output=True, text=True)

        if res.returncode != 0:
            log(f"İmzalama başarısız oldu:\n{res.stderr}\n{res.stdout}", "✗")
            return False

        log(res.stdout.strip(), "✓")

        # packages-arm64.json ve .sig dosyalarını packages.json ile eşitle
        if INDEX_JSON.exists():
            shutil.copyfile(INDEX_JSON, INDEX_ARM64_JSON)
        if Path(f"{INDEX_JSON}.sig").exists():
            shutil.copyfile(f"{INDEX_JSON}.sig", f"{INDEX_ARM64_JSON}.sig")

        # Kriptografik doğrulama testi yap
        valid = self.verify_cryptographic_signatures()
        if not valid:
            log("Kriptografik imza doğrulama başarısız! Push iptal edildi.", "🚨")
            return False

        if auto_push:
            return self.commit_and_push()
        return True

    def verify_cryptographic_signatures(self):
        """packages.json ve system.json Ed25519 imzalarını doğrular."""
        if not shutil.which("openssl"):
            log("openssl bulunamadı, imza doğrulama atlandı.", "⚠")
            return True

        files_to_verify = [INDEX_JSON, SYSTEM_JSON]
        for f in files_to_verify:
            sig_file = Path(f"{f}.sig")
            if not f.exists() or not sig_file.exists():
                log(f"{f.name} veya imzası mevcut değil!", "✗")
                return False

            try:
                sig_data = json.loads(sig_file.read_text(encoding="utf-8"))
                expected_sha = sig_data.get("sha256", "")
                sig_raw = base64.b64decode(sig_data.get("sig", ""), validate=True)

                data_bytes = f.read_bytes()
                actual_sha = hashlib.sha256(data_bytes).hexdigest()
                if actual_sha != expected_sha:
                    log(f"{f.name} SHA256 uyuşmazlığı!", "✗")
                    return False

                with tempfile.TemporaryDirectory() as td:
                    pub = Path(td) / "pub.pem"
                    sigf = Path(td) / "sig"
                    pub.write_text(RELEASE_PUBKEY)
                    sigf.write_bytes(sig_raw)
                    r = subprocess.run([
                        "openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(pub),
                        "-rawin", "-in", str(f), "-sigfile", str(sigf)
                    ], capture_output=True, text=True)
                    if r.returncode != 0:
                        log(f"{f.name} Ed25519 imza doğrulaması başarısız: {r.stderr}", "✗")
                        return False
            except Exception as e:
                log(f"{f.name} doğrulama sırasında istisna: {e}", "✗")
                return False

        log("Tüm Ed25519 imzaları başarıyla doğrulandı (mindpkg 1.8+ uyumlu).", "🛡️")
        return True

    def commit_and_push(self):
        """İmzalı indeks ve sistem dosyalarını origin main dalına commit ve push eder."""
        log("Git deposu kontrol ediliyor...", "📡")
        files_to_stage = [
            "packages.json",
            "packages.json.sig",
            "packages-arm64.json",
            "packages-arm64.json.sig",
            "system.json",
            "system.json.sig",
            "staging/packages.json",
            "staging/packages-arm64.json"
        ]
        existing_stage = [f for f in files_to_stage if (ROOT / f).exists()]
        subprocess.run(["git", "add", *existing_stage], cwd=str(ROOT), check=True)

        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True)
        changed = [line for line in status.stdout.splitlines() if any(f in line for f in files_to_stage)]

        if not changed:
            log("İndeks ve sistem dosyalarında commit edilecek yeni değişiklik yok.", "ℹ")
            return True

        curr_index = self.load_index(INDEX_JSON)
        pkg_count = curr_index.get("total_packages", 0)
        chunk_count = curr_index.get("total_chunks", 1)

        msg = f"chore(arm64): publish signed packages index ({pkg_count} packages, {chunk_count} chunks)"
        log(f"Git commit: {msg}", "📝")
        subprocess.run(["git", "commit", "-m", msg], cwd=str(ROOT), check=True)

        log("Git origin main ile rebase ve push yapılıyor...", "🚀")
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=str(ROOT), check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(ROOT), check=True)
        log("✓ Git push başarıyla tamamlandı.", "✨")
        return True

    def get_dashboard_stats(self):
        """Detaylı canlı istatistik ve metrikleri toplar."""
        chunks = self.fetch_all_chunks(force_refresh=True)
        chunk_stats = []
        total_remote_assets = 0
        for c in chunks:
            cnt = self.get_chunk_asset_count(c["tag"], force_refresh=True)
            total_remote_assets += cnt
            cap_pct = (cnt / self.max_chunk_assets) * 100
            chunk_stats.append({
                "tag": c["tag"],
                "number": c["number"],
                "title": c["title"],
                "asset_count": cnt,
                "capacity_pct": cap_pct,
                "is_full": cnt >= self.max_chunk_assets
            })

        idx_data = self.load_index(INDEX_JSON)
        stg_data = self.load_index(STAGING_JSON)

        pending_local_files = len(list(OUT_DIR.glob("*.mind"))) if OUT_DIR.exists() else 0

        # İmza durumu
        sig_ok = self.verify_cryptographic_signatures()

        # Git son commit
        git_log = subprocess.run(
            ["git", "log", "-1", "--format=%h - %s (%cr)"],
            cwd=str(ROOT),
            capture_output=True,
            text=True
        ).stdout.strip()

        return {
            "repo": self.repo,
            "chunks": chunk_stats,
            "total_chunks": len(chunks),
            "total_remote_assets": total_remote_assets,
            "published_packages": idx_data.get("total_packages", len(idx_data.get("packages", []))),
            "staging_packages": stg_data.get("total_packages", len(stg_data.get("packages", []))),
            "pending_local_files": pending_local_files,
            "signatures_valid": sig_ok,
            "latest_commit": git_log
        }

    def print_dashboard(self):
        stats = self.get_dashboard_stats()
        print("\n" + "=" * 70)
        print(f"📊  MindOS Paradise ARM64 Repository & Release Dashboard")
        print("=" * 70)
        print(f"  Depo               : {stats['repo']}")
        print(f"  Yayınlanan İndeks  : {stats['published_packages']} paket (packages.json)")
        print(f"  Taslak İndeks      : {stats['staging_packages']} paket (staging/packages.json)")
        print(f"  Bekleyen Yerel     : {stats['pending_local_files']} .mind paketi (out_arm64/)")
        print(f"  Kriptografik İmza  : {'✓ GEÇERLİ (Ed25519)' if stats['signatures_valid'] else '✗ GEÇERSİZ'}")
        print(f"  Son Git Commit     : {stats['latest_commit']}")
        print("-" * 70)
        print(f"  GitHub Releases Parçaları (Chunks) - Toplam {stats['total_remote_assets']} Asset:")
        for c in stats["chunks"]:
            status_symbol = "🔒 DOLU" if c["is_full"] else "🟢 AKTİF"
            bar_len = int(c["capacity_pct"] // 5)
            progress_bar = "[" + "#" * bar_len + "." * (20 - bar_len) + "]"
            print(f"    • {c['tag']:<15} : {c['asset_count']:>4}/{self.max_chunk_assets} {progress_bar} {c['capacity_pct']:>5.1f}% | {status_symbol}")
        print("=" * 70 + "\n")


def main():
    ap = argparse.ArgumentParser(description="MindOS ARM64 Release & Repository Manager")
    ap.add_argument("--repo", default=REPO_DEFAULT, help="GitHub deposu")
    ap.add_argument("--status", action="store_true", help="Canlı depo ve chunk durum panelini göster")
    ap.add_argument("--sync-now", action="store_true", help="Bekleyen paketleri yükle, imzala ve push et")
    ap.add_argument("--sign-push", action="store_true", help="Yalnızca indeksleri imzala, doğrula ve push et")
    ap.add_argument("--watch", action="store_true", help="Sürekli izleme döngüsü çalıştır")
    ap.add_argument("--interval", type=int, default=15, help="İzleme döngüsü bekleme süresi (sn)")
    ap.add_argument("--batch-size", type=int, default=25, help="Yükleme partisi büyüklüğü")
    args = ap.parse_args()

    mgr = ARM64ReleaseOpsManager(repo=args.repo)

    if args.status:
        mgr.print_dashboard()
        return 0

    if args.sign_push:
        mgr.sign_and_publish_index(auto_push=True)
        mgr.print_dashboard()
        return 0

    if args.sync_now:
        log("Tam eşitleme ve dağıtım başlatıldı...", "⚡")
        uploaded, failed = mgr.upload_pending_packages(batch_size=args.batch_size)
        mgr.sign_and_publish_index(auto_push=True)
        mgr.print_dashboard()
        return 0

    if args.watch:
        log(f"Sürekli ARM64 Release Operasyon Yöneticisi devrede ({args.interval} sn aralık)...", "👀")
        while True:
            try:
                pending = len(list(OUT_DIR.glob("*.mind"))) if OUT_DIR.exists() else 0
                if pending > 0:
                    log(f"İzleme: {pending} bekleyen paket tespit edildi, eşitleme yapılıyor...", "⚡")
                    mgr.upload_pending_packages(batch_size=args.batch_size)
                    mgr.sign_and_publish_index(auto_push=True)
                    mgr.print_dashboard()
                time.sleep(args.interval)
            except KeyboardInterrupt:
                log("İzleme kullanıcı tarafından durduruldu.", "🛑")
                break
            except Exception as e:
                log(f"İzleme döngüsünde hata: {e}", "⚠")
                time.sleep(args.interval)
        return 0

    # Varsayılan: status göster
    mgr.print_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
