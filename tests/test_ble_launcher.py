"""Simulate LaunchServices and its app files; never construct CBCentralManager."""
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("launch_ble_bridge", ROOT / "scripts/launch_ble_bridge.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        state_patch = patch.object(launcher, "STATE", self.base)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.app = self.base / "Mock.app"
        self.binary = self.app / "Contents/MacOS/niimbot-ble-bridge"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("not an executable test fixture\n")
        self.binary.chmod(0o700)
        (self.app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": launcher.BUNDLE_ID,
            "CFBundleExecutable": "niimbot-ble-bridge",
            "CFBundlePackageType": "APPL",
            "NSBluetoothAlwaysUsageDescription": "测试用途说明",
        }))

    def test_argv_identity_and_override_validation(self):
        self.assertEqual(launcher.validate_app(self.app), (self.app, self.binary))
        argv = launcher.launch_argv(self.app, Path("/fifo"), Path("/out"), Path("/err"),
                                    Path("/pid"), ["scan", "--seconds", "8"])
        self.assertEqual(argv, ["/usr/bin/open", "-n", "-g", "-W", "--stdin", "/fifo",
                                "--stdout", "/out", "--stderr", "/err", str(self.app),
                                "--args", "scan", "--seconds", "8", "--pid-file", "/pid"])
        with self.assertRaises(ValueError):
            launcher.validate_app("relative.app")
        with self.assertRaises(ValueError):
            launcher.run(["scan", "--pid-file", "/arbitrary"], app=self.app)

    def test_fifo_eof_and_unmodified_log_tunnel(self):
        source_read, source_write = os.pipe()
        source = os.fdopen(source_read, "rb", buffering=0)
        self.addCleanup(source.close)
        payload = b'{"op":"write","id":7,"hex":"aabb"}\n'
        os.write(source_write, payload)
        os.close(source_write)
        stdout, stderr = io.BytesIO(), io.BytesIO()
        observed = {}

        class MockOpen:
            def __init__(self, argv, **kwargs):
                self.code = None
                self.thread = threading.Thread(target=self.app_worker, args=(argv,))
                self.thread.start()

            def app_worker(self, argv):
                fifo = Path(argv[argv.index("--stdin") + 1])
                out = Path(argv[argv.index("--stdout") + 1])
                err = Path(argv[argv.index("--stderr") + 1])
                pidfile = Path(argv[argv.index("--pid-file") + 1])
                observed["private"] = fifo.parent
                observed["mode"] = fifo.parent.stat().st_mode & 0o777
                observed["fifo_mode"] = fifo.stat().st_mode & 0o777
                with fifo.open("rb", buffering=0) as reader:
                    pidfile.write_text("2147483646\n")
                    pidfile.chmod(0o600)
                    out.write_bytes(b'{"event":"ready","max_write":180}\n')
                    observed["input"] = reader.read()  # Must end at parent EOF, not timeout.
                    with out.open("ab") as writer:
                        writer.write(b'{"event":"written","id":7}\n')
                    err.write_bytes(b"native diagnostic\n")
                self.code = 0

            def poll(self):
                return self.code

            def wait(self, timeout):
                self.thread.join(timeout)
                if self.thread.is_alive():
                    raise subprocess.TimeoutExpired("mockopen", timeout)
                return self.code

        with patch.object(launcher, "STATE", self.base), patch.object(launcher, "stop_native") as cleanup:
            code = launcher.run(["bridge", "--device", "mock-uuid"], app=self.app,
                                stdin=source, stdout=stdout, stderr=stderr, popen=MockOpen)
        self.assertEqual(code, 0)
        self.assertEqual(observed["input"], payload)
        self.assertEqual(observed["mode"], 0o700)
        self.assertEqual(observed["fifo_mode"], 0o600)
        self.assertFalse(observed["private"].exists())
        self.assertEqual(stdout.getvalue(), b'{"event":"ready","max_write":180}\n{"event":"written","id":7}\n')
        self.assertEqual(stderr.getvalue(), b"native diagnostic\n")
        self.assertEqual(cleanup.call_count, 2)

    def test_sigterm_stops_owned_app_and_drains_final_output(self):
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, "rb", buffering=0)
        self.addCleanup(source.close)
        self.addCleanup(os.close, write_fd)
        stdout = io.BytesIO()
        observed = {}

        class MockOpen:
            def __init__(self, argv, **kwargs):
                self.code = None
                self.polls = 0
                self.out = Path(argv[argv.index("--stdout") + 1])
                observed["directory"] = self.out.parent
                pidfile = Path(argv[argv.index("--pid-file") + 1])
                pidfile.write_text("123456\n")
                pidfile.chmod(0o600)

            def poll(self):
                self.polls += 1
                if self.polls == 2:
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                return self.code

            def wait(self, timeout):
                self.out.write_bytes(b'{"event":"data","hex":"00"}\n')
                self.code = 0
                return 0

        old_handler = signal.getsignal(signal.SIGTERM)
        with patch.object(launcher, "STATE", self.base), \
             patch.object(launcher, "owned_pid", side_effect=[123456, None, None]), \
             patch.object(launcher.os, "kill") as kill:
            code = launcher.run(["bridge", "--device", "mock-uuid"], app=self.app,
                                stdin=source, stdout=stdout, stderr=io.BytesIO(), popen=MockOpen)
        self.assertEqual(code, 143)
        kill.assert_called_once_with(123456, signal.SIGTERM)
        self.assertEqual(stdout.getvalue(), b'{"event":"data","hex":"00"}\n')
        self.assertFalse(observed["directory"].exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), old_handler)

    def test_cleanup_checks_pid_and_exact_command(self):
        pidfile = self.base / "native.pid"
        pidfile.write_text("123456\n")
        pidfile.chmod(0o600)
        with patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, str(self.binary) + "\n", "")):
            self.assertEqual(launcher.owned_pid(pidfile, self.binary), 123456)
        with patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/different/executable\n", "")), \
             patch.object(launcher.os, "kill") as kill:
            launcher.stop_native(pidfile, self.binary)
            kill.assert_not_called()
        with patch.object(launcher, "owned_pid", side_effect=[123456, None]), \
             patch.object(launcher.os, "kill") as kill:
            launcher.stop_native(pidfile, self.binary)
            kill.assert_called_once_with(123456, signal.SIGTERM)

    def test_launch_failure_closes_private_tunnel(self):
        with patch.object(launcher, "STATE", self.base):
            before = set(self.base.iterdir())
            with self.assertRaises(OSError):
                launcher.run(["scan"], app=self.app, stdin=io.BytesIO(), stdout=io.BytesIO(),
                             stderr=io.BytesIO(), popen=lambda *a, **kw: (_ for _ in ()).throw(OSError("mock failure")))
            self.assertEqual(set(self.base.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
