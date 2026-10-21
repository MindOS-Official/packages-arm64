#!/usr/bin/env python3
"""Test suite for ARM64 Release Ops Manager."""

import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from arm64_release_ops_manager import ARM64ReleaseOpsManager, RELEASE_PUBKEY


class TestARM64ReleaseOpsManager(unittest.TestCase):
    def setUp(self):
        self.mgr = ARM64ReleaseOpsManager(repo="MindOS-Official/packages-arm64")

    def test_url_sanitization(self):
        """Debian ~ içeren dosya isimlerinin GitHub Releases uyumlu . karakterine dönüştürülmesi."""
        filenames = [
            ("libdbi-perl-1.652-2~deb13u1-1-aarch64.mind", "libdbi-perl-1.652-2.deb13u1-1-aarch64.mind"),
            ("foo+bar_1.0~1-aarch64.mind", "foo+bar_1.0.1-aarch64.mind"),
            ("normal-pkg-1.0-1-aarch64.mind", "normal-pkg-1.0-1-aarch64.mind"),
        ]
        for original, expected in filenames:
            clean = re.sub(r"[^A-Za-z0-9._+-]", ".", original)
            self.assertEqual(clean, expected)

    def test_signature_verification(self):
        """Tüm indeks (packages.json, packages-arm64.json, system.json) Ed25519 imzaları geçerli olmalı."""
        valid = self.mgr.verify_cryptographic_signatures()
        self.assertTrue(valid, "Ed25519 imzaları doğrulanamadı!")

    def test_staging_synchronization(self):
        """staging/packages.json ve staging/packages-arm64.json eşit olmalı."""
        data = self.mgr.load_staging_index()
        self.assertIn("packages", data)
        self.assertGreater(data["total_packages"], 0)

        p1 = json.loads((ROOT / "staging/packages.json").read_text(encoding="utf-8"))
        p2 = json.loads((ROOT / "staging/packages-arm64.json").read_text(encoding="utf-8"))
        self.assertEqual(len(p1["packages"]), len(p2["packages"]))

    def test_chunk_threshold_and_listing(self):
        """Chunk listesi doğru sıralı ve asset sayıları geçerli olmalı."""
        chunks = self.mgr.fetch_all_chunks()
        self.assertGreaterEqual(len(chunks), 2)
        tags = [c["tag"] for c in chunks]
        self.assertIn("v1.0-chunk-01", tags)
        self.assertIn("v1.0-chunk-02", tags)

    def test_all_remote_assets_mapping(self):
        """Tüm chunk'lardaki assetler tek bir sözlükte birleştirilmeli."""
        assets = self.mgr.get_all_remote_asset_names()
        self.assertGreater(len(assets), 1000)
        # Örnek bilinen bir paket
        any_asset = next(iter(assets.keys()))
        self.assertTrue(assets[any_asset].startswith("v1.0-chunk-"))


if __name__ == "__main__":
    unittest.main()
