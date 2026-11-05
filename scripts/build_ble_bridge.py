#!/usr/bin/env python3
"""Build a private, purpose-identified CoreBluetooth transport; never access BLE."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

OUTPUT = Path(os.environ.get("NIIMBOT_BLE_BUILD_DIR",
                             str(Path.home() / ".local/state/niimbot-label/ble-bridge"))).expanduser()
BUNDLE_ID = "org.niimbot-ai-skill.ble-bridge"
USAGE = "允许 NIIMBOT BLE Bridge 通过蓝牙直接连接您的 NIIMBOT 标签打印机并传输打印数据。"


def build():
    if not OUTPUT.is_absolute():
        raise ValueError("NIIMBOT_BLE_BUILD_DIR must be an absolute path")
    if sys.platform != "darwin":
        raise RuntimeError("This helper requires macOS CoreBluetooth and swiftc")
    compiler = shutil.which("swiftc")
    if not compiler:
        raise RuntimeError("swiftc unavailable; install/configure Xcode command line tools separately")
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(OUTPUT, 0o700)
    app = OUTPUT / "NIIMBOT BLE Bridge.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    plist_path = contents / "Info.plist"
    plist_path.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "NIIMBOT BLE Bridge",
        "CFBundleExecutable": "niimbot-ble-bridge",
        "CFBundleVersion": "1",
        "CFBundlePackageType": "APPL",
        "LSUIElement": True,
        "NSBluetoothAlwaysUsageDescription": USAGE,
    }))
    binary = macos / "niimbot-ble-bridge"
    env = dict(os.environ)
    temporary = OUTPUT / "tmp"
    temporary.mkdir(exist_ok=True, mode=0o700)
    env["TMPDIR"] = str(temporary)
    env["CLANG_MODULE_CACHE_PATH"] = str(OUTPUT / "clang-module-cache")
    subprocess.run([
        compiler, str(Path(__file__).with_name("ble_bridge.swift")),
        "-o", str(binary), "-module-cache-path", str(OUTPUT / "swift-module-cache"),
        "-framework", "Foundation", "-framework", "CoreBluetooth",
        "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
        "-Xlinker", "__info_plist", "-Xlinker", str(plist_path),
    ], check=True, env=env)
    codesign = shutil.which("codesign")
    if not codesign:
        raise RuntimeError("codesign unavailable; binary built but signing/launch unverified")
    subprocess.run([codesign, "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(app)], check=True)
    subprocess.run([codesign, "--verify", "--strict", str(app)], check=True)
    print(json.dumps({"app": str(app), "binary": str(binary), "launcher": str(Path(__file__).with_name("launch_ble_bridge.py").resolve()), "plist": str(plist_path), "signed": "ad-hoc"}))


if __name__ == "__main__":
    try:
        build()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
