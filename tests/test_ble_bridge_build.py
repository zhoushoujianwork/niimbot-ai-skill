"""Nonradio checks only: do not invoke scan or bridge from tests."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_ble_bridge", ROOT / "scripts/build_ble_bridge.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildTests(unittest.TestCase):
    def test_build_embeds_identity_and_signs_without_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)
            with patch.object(builder, "OUTPUT", target), \
                 patch.object(builder.sys, "platform", "darwin"), \
                 patch.object(builder.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                 patch.object(builder.subprocess, "run") as run:
                builder.build()
            info = plistlib.loads((target / "NIIMBOT BLE Bridge.app/Contents/Info.plist").read_bytes())
            self.assertEqual(info["CFBundleIdentifier"], builder.BUNDLE_ID)
            self.assertEqual(info["CFBundleName"], "NIIMBOT BLE Bridge")
            self.assertEqual(info["NSBluetoothAlwaysUsageDescription"], builder.USAGE)
            self.assertTrue(info["LSUIElement"])
            self.assertEqual(info["CFBundlePackageType"], "APPL")
            compile_command = run.call_args_list[0].args[0]
            self.assertIn("__TEXT", compile_command)
            self.assertIn("__info_plist", compile_command)
            self.assertIn(str(target / "NIIMBOT BLE Bridge.app/Contents/Info.plist"), compile_command)
            self.assertEqual(run.call_count, 3)  # compile, sign, verify; never BLE launch
            self.assertIn("--sign", run.call_args_list[1].args[0])
            self.assertIn("--verify", run.call_args_list[2].args[0])
            self.assertEqual(run.call_args_list[0].kwargs["env"]["TMPDIR"], str(target / "tmp"))

    def test_built_executable_self_test_and_invalid_arguments(self):
        if not os.environ.get("NIIMBOT_BLE_BUILD_DIR"):
            self.skipTest("Native check requires an explicitly isolated NIIMBOT_BLE_BUILD_DIR")
        binary = builder.OUTPUT / "NIIMBOT BLE Bridge.app/Contents/MacOS/niimbot-ble-bridge"
        if not binary.exists():
            self.skipTest("Run scripts/build_ble_bridge.py first")
        result = subprocess.run([str(binary), "--self-test"], capture_output=True, text=True, timeout=10, check=True)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        report = json.loads(lines[0])
        self.assertTrue(report["ok"])
        self.assertTrue(report["hex_and_whole_frame_tests"])
        self.assertTrue(report["never_split"])
        self.assertEqual(report["write_mode"], "without_response")
        app = binary.parents[2]
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True, capture_output=True)
        info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        self.assertTrue(info["LSUIElement"])
        self.assertEqual(info["NSBluetoothAlwaysUsageDescription"], builder.USAGE)
        self.assertEqual(report["bundle_id"], builder.BUNDLE_ID)
        self.assertEqual(report["usage_description"], builder.USAGE)
        with tempfile.TemporaryDirectory(dir=builder.OUTPUT) as folder:
            pidfile = Path(folder) / "native.pid"
            subprocess.run([str(binary), "--self-test", "--pid-file", str(pidfile)],
                           capture_output=True, timeout=10, check=True)
            self.assertGreater(int(pidfile.read_text()), 1)
            self.assertEqual(pidfile.stat().st_mode & 0o777, 0o600)
            existing = pidfile.read_bytes()
            result = subprocess.run([str(binary), "--self-test", "--pid-file", str(pidfile)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(pidfile.read_bytes(), existing)
        # Each argument list fails before Bridge.start() and cannot touch the radio.
        for args in ([], ["scan", "--seconds", "16"], ["scan", "--seconds", "nan"],
                     ["bridge", "--device", "invalid"]):
            result = subprocess.run([str(binary)] + args, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["event"], "error")


if __name__ == "__main__":
    unittest.main()
