# MindOS ARM64 (.mind) Official Package Repository

Official binary package repository for **MindOS Sunrise (ARM64 / aarch64)**.

## Overview
- **Architecture**: `aarch64` (ARM64)
- **Format**: `.mind` (Zstandard compressed archives with metadata and Debian maintainer script hooks)
- **Security**: Cryptographically signed indexes via Ed25519 (`packages.json.sig`, `system.json.sig`)
- **Distribution**: Chunk-based GitHub Releases (`v1.0-chunk-XX`)
- **Package Manager**: [mindpkg](tools/mindpkg)

## Usage with mindpkg

MindOS systems automatically configure the official ARM64 repository when running on an `aarch64` machine:

```bash
# Update repository catalog
sudo mindpkg update

# Install a package
sudo mindpkg install <package_name>

# Search packages
mindpkg search <query>
```

Repository index URL:
`https://raw.githubusercontent.com/MindOS-Official/packages-arm64/main`
