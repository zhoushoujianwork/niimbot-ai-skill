#!/usr/bin/env python3
"""Pipe shim for a private LaunchServices app; never launch the bare BLE binary."""
import errno
import json
import os
from pathlib import Path
import plistlib
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

STATE = Path.home() / ".local/state/niimbot-label/ble-bridge"
BUNDLE_ID = "org.niimbot-ai-skill.ble-bridge"


def validate_app(path):
    app = Path(path).expanduser()
    if not app.is_absolute() or app.suffix != ".app":
        raise ValueError("NIIMBOT_BLE_APP must be an absolute .app path")
    app = app.resolve(strict=True)
    contents = app / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    if (info.get("CFBundleIdentifier") != BUNDLE_ID or
            info.get("CFBundleExecutable") != "niimbot-ble-bridge" or
            info.get("CFBundlePackageType") != "APPL" or
            not info.get("NSBluetoothAlwaysUsageDescription")):
        raise ValueError("App identity or Bluetooth usage description invalid")
    binary = (contents / "MacOS/niimbot-ble-bridge").resolve(strict=True)
    if binary.parent != (contents / "MacOS").resolve() or not os.access(binary, os.X_OK):
        raise ValueError("App executable invalid")
    return app, binary


def launch_argv(app, fifo, stdout, stderr, pidfile, args):
    return ["/usr/bin/open", "-n", "-g", "-W", "--stdin", str(fifo),
            "--stdout", str(stdout), "--stderr", str(stderr), str(app),
            "--args", *args, "--pid-file", str(pidfile)]


def owned_pid(pidfile, binary):
    """Signal only our private pidfile's still-running, exact executable."""
    try:
        metadata = pidfile.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                stat.S_IMODE(metadata.st_mode) & 0o077):
            return None
        pid = int(pidfile.read_text().strip())
        if pid <= 1 or pid == os.getpid():
            return None
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip() == str(binary):
            return pid
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def forward_input(source_fd, fifo_fd, stop):
    try:
        while not stop.is_set():
            if not select.select([source_fd], [], [], 0.1)[0]:
                continue
            data = os.read(source_fd, 65536)
            if not data:
                return
            while data and not stop.is_set():
                try:
                    sent = os.write(fifo_fd, data)
                    data = data[sent:]
                except BlockingIOError:
                    select.select([], [fifo_fd], [], 0.1)
    except OSError as exc:
        if exc.errno not in (errno.EBADF, errno.EPIPE):
            stop.set()
    finally:
        os.close(fifo_fd)  # Last writer closes on stdin EOF; native readLine sees EOF.


def drain(reader, destination):
    while True:
        data = reader.read(65536)
        if not data:
            return
        destination.write(data)
        destination.flush()


def stop_native(pidfile, binary):
    pid = owned_pid(pidfile, binary)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if owned_pid(pidfile, binary) != pid:
            return
        time.sleep(0.05)
    if owned_pid(pidfile, binary) == pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run(args, *, app=None, stdin=None, stdout=None, stderr=None, popen=None):
    if "--pid-file" in args:
        raise ValueError("--pid-file is reserved for launcher lifecycle management")
    app, binary = validate_app(app or os.environ.get("NIIMBOT_BLE_APP", str(STATE / "NIIMBOT BLE Bridge.app")))
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    stderr = sys.stderr.buffer if stderr is None else stderr
    popen = subprocess.Popen if popen is None else popen
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        with tempfile.TemporaryDirectory(prefix="launch-", dir=STATE) as folder:
            private = Path(folder)
            os.chmod(private, 0o700)
            fifo, out, err, pidfile = [private / name for name in ("stdin.fifo", "stdout.log", "stderr.log", "native.pid")]
            os.mkfifo(fifo, 0o600)
            for path in (out, err):
                path.touch(mode=0o600)
            holder = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            process = None
            thread = None
            try:
                with out.open("rb", buffering=0) as out_reader, err.open("rb", buffering=0) as err_reader:
                    try:
                        process = popen(launch_argv(app, fifo, out, err, pidfile, args),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        startup_deadline = time.monotonic() + 20
                        while True:
                            drain(out_reader, stdout)
                            drain(err_reader, stderr)
                            # Native startup means LaunchServices already opened its FIFO reader.
                            if thread is None and pidfile.exists():
                                thread = threading.Thread(target=forward_input,
                                                          args=(stdin.fileno(), holder, stop), daemon=True)
                                thread.start()
                                holder = None  # Thread owns descriptor and closes on EOF.
                            code = process.poll()
                            if code is not None:
                                return code
                            if stop.is_set():
                                return 128 + signal.SIGTERM
                            if thread is None and time.monotonic() > startup_deadline:
                                raise RuntimeError("LaunchServices app startup timed out (20 seconds)")
                            time.sleep(0.02)
                    finally:
                        stop.set()
                        stop_native(pidfile, binary)
                        if process is not None:
                            try:
                                process.wait(timeout=4)
                            except subprocess.TimeoutExpired:
                                process.terminate()
                                try:
                                    process.wait(timeout=2)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=2)
                        # Cover an app that finished launching while open was being reaped.
                        stop_native(pidfile, binary)
                        # App termination closes its log descriptors; forward final bytes too.
                        drain(out_reader, stdout)
                        drain(err_reader, stderr)
            finally:
                stop.set()
                if thread is not None:
                    thread.join(timeout=1)
                if holder is not None:
                    os.close(holder)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main():
    try:
        return run(sys.argv[1:])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"event": "error", "message": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
