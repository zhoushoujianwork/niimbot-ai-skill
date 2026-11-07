#!/usr/bin/env python3
"""Reproducible allowlist-only Agent Skill ZIPs. No network or publication action."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

SLUG = "niimbot-label"
VERSION = "1.0.0"
SKILLHUB_METADATA = {
    "slug": SLUG,
    "displayName": "NIIMBOT AI Printer",
    "version": VERSION,
    "author": "NIIMBOT AI Skill contributors",
}
# Every path is individually reviewed. No glob, walk, git inventory, or adjacent files.
ALLOWLIST = (
    "SKILL.md",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "requirements.txt",
    "requirements-dev.txt",
    "agents/openai.yaml",
    "examples/device.json",
    "assets/label-30x20mm.png",
    "assets/preview.png",
    "references/stock-catalog.md",
    "tests/test_stock_catalog.py",
    "references/usb-protocol.md",
    "references/ble-protocol.md",
    "references/ble-bridge.md",
    "references/miniapp-label.md",
    "scripts/niimbot_usb.py",
    "scripts/niimbot_ble.py",
    "scripts/render_miniapp_label.py",
    "scripts/ble_bridge.swift",
    "scripts/build_ble_bridge.py",
    "scripts/launch_ble_bridge.py",
    "scripts/package_skill.py",
    "tests/test_niimbot_usb.py",
    "tests/test_niimbot_ble.py",
    "tests/test_miniapp_label.py",
    "tests/test_ble_bridge_build.py",
    "tests/test_ble_launcher.py",
    "tests/test_package_skill.py",
    ".github/workflows/test.yml",
    ".gitignore",
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def collect_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source root must be a real directory, not a symlink")
    entries = {}
    for relative in sorted(ALLOWLIST):
        parts = PurePosixPath(relative).parts
        if not parts or relative.startswith("/") or any(p in (".", "..") for p in parts):
            raise ValueError("invalid allowlist path")
        candidate = root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("symlink in allowlisted path: " + relative)
        if not stat.S_ISREG(candidate.stat().st_mode):
            raise ValueError("not a regular file: " + relative)
        entries[relative] = candidate.read_bytes()
    return entries


def skillhub_overlay(canonical):
    text = canonical.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("canonical SKILL.md must start with YAML front matter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("canonical SKILL.md front matter is incomplete")
    keys = {line.partition(":")[0].strip() for line in lines[1:end]}
    if keys.intersection(SKILLHUB_METADATA):
        raise ValueError("canonical SKILL.md already contains overlay keys")
    additions = [key + ": " + json.dumps(value, ensure_ascii=False) + "\n"
                 for key, value in SKILLHUB_METADATA.items()]
    return ("".join(lines[:end]) + "".join(additions) + "".join(lines[end:])).encode("utf-8")


def archive(entries):
    target = io.BytesIO()
    # STORED avoids zlib-version differences; dates/modes/order are explicitly fixed.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as output:
        for relative, content in sorted(entries.items()):
            info = zipfile.ZipInfo(SLUG + "/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            executable = relative.startswith("scripts/") and relative.endswith(".py")
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            info.compress_type = zipfile.ZIP_STORED
            output.writestr(info, content)
    return target.getvalue()


def prepare(root):
    entries = collect_files(root)
    overlay = dict(entries)
    overlay["SKILL.md"] = skillhub_overlay(entries["SKILL.md"])
    variants = {"agent-skill": entries, "skillhub": overlay}
    archives, reports = {}, {}
    for variant, contents in variants.items():
        filename = f"{SLUG}-{VERSION}-{variant}.zip"
        data = archive(contents)
        archives[filename] = data
        reports[filename] = {
            "sha256": sha256(data), "bytes": len(data),
            "files": [{"path": SLUG + "/" + path, "sha256": sha256(content), "bytes": len(content)}
                      for path, content in sorted(contents.items())],
        }
    return archives, {"slug": SLUG, "version": VERSION, "archives": reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="hash/list both in-memory ZIPs; write nothing")
    mode.add_argument("--write", action="store_true", help="write local ZIPs and manifest to dist/ only")
    args = parser.parse_args(argv)
    root = Path(__file__).absolute().parents[1]
    archives, report = prepare(root)
    if args.write:
        output = root / "dist"
        if output.is_symlink():
            raise ValueError("dist must not be a symlink")
        output.mkdir(exist_ok=True)
        for filename, data in archives.items():
            destination = output / filename
            if destination.is_symlink():
                raise ValueError("output file must not be a symlink")
            destination.write_bytes(data)
        manifest = output / "manifest.json"
        if manifest.is_symlink():
            raise ValueError("manifest must not be a symlink")
        manifest.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
