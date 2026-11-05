#!/usr/bin/env python3
"""B1 Pro BLE via an explicitly selected native JSON-lines helper; never opens USB."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

import niimbot_usb as usb

SERVICE = "e7810a71-73ae-499d-8c15-faa9aef0c3f2"
CHARACTERISTIC = "bef8d6c9-9c21-4c9e-b632-bd58c1009f9f"


class BridgeError(RuntimeError):
    pass


def check_app_not_running():
    result = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise BridgeError("cannot check competing NIIMBOT process")
    if any(line.strip().endswith("/NIIMBOT.app/Contents/MacOS/NIIMBOT") or line.strip() == "NIIMBOT"
           for line in result.stdout.splitlines()):
        raise BridgeError("quit NIIMBOT App before direct BLE; no process was terminated")


def helper_path(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(str(path), os.X_OK):
        raise BridgeError("--helper must be an existing absolute executable native helper path")
    return str(path)


def device_id(value):
    try:
        return str(uuid.UUID(value)).upper()
    except (ValueError, AttributeError, TypeError):
        raise BridgeError("--device must be a CoreBluetooth UUID, not a name or MAC address")


class HelperSession:
    """Own one child; capture every stdout/stderr line without using the desktop."""
    def __init__(self, command, diagnostics=None, popen=subprocess.Popen):
        self.diagnostics = diagnostics if diagnostics is not None else []
        self.queue = queue.Queue()
        self.process = popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
        self.closed = False
        self.threads = [threading.Thread(target=self._reader, args=(stream, name), daemon=True)
                        for stream, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr"))]
        for thread in self.threads:
            thread.start()

    def _reader(self, stream, source):
        try:
            for line in stream:
                raw = line.rstrip("\r\n")
                item = {"source": source, "raw": raw}
                if source == "stdout":
                    try:
                        event = json.loads(raw)
                        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                            raise ValueError("missing event object")
                        item["json"] = event
                    except (ValueError, TypeError) as exc:
                        item["schema_error"] = str(exc)
                self.diagnostics.append(item)
                self.queue.put(item)
        except Exception as exc:
            item = {"source": source, "reader_error": str(exc)}
            self.diagnostics.append(item)
            self.queue.put(item)
        finally:
            if source == "stdout":
                self.queue.put({"source": source, "eof": True})

    def next_event(self, deadline):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("native helper event timeout; no retry")
            try:
                item = self.queue.get(timeout=left)
            except queue.Empty:
                raise TimeoutError("native helper event timeout; no retry")
            if item.get("eof"):
                raise BridgeError("native helper disconnected/EOF (exit %s)" % self.process.poll())
            if item.get("reader_error") or item.get("schema_error"):
                raise BridgeError("native helper event mismatch: " + str(item))
            if item["source"] == "stderr":
                continue  # retained in full diagnostics
            return item["json"]

    def send(self, obj):
        if self.process.poll() is not None:
            raise BridgeError("native helper exited before write")
        try:
            self.process.stdin.write(json.dumps(obj, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise BridgeError("native helper stdin failed: " + str(exc))

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self.process.terminate()  # owned helper only; never terminates the NIIMBOT App
            try:
                self.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=0.5)
        for thread in self.threads:
            thread.join(timeout=0.2)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()


class BridgeTransport:
    """Synchronous usb.Link transport over native helper events; no implicit retries."""
    def __init__(self, session, device, startup_timeout=15):
        self.session = session
        self.timeout, self.write_timeout = 0.05, 0.5
        self.buffer = bytearray()
        self.next_id = 1
        self.last_write_at = None
        self.ready = None
        self.waiting_id = None
        self.acknowledged_id = None
        self.operation_deadline = None
        self.parser_boundary = None
        self.out_of_band_notifications = []
        end = time.monotonic() + startup_timeout
        while self.ready is None:
            self._event(end, startup=True)
        ready = self.ready
        if device_id(ready.get("device")) != device_id(device):
            raise BridgeError("bridge ready device does not match requested UUID")
        if str(ready.get("service", "")).lower() != SERVICE or str(ready.get("characteristic", "")).lower() != CHARACTERISTIC:
            raise BridgeError("unexpected BLE service/characteristic")
        maximum = ready.get("max_write")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 58:
            raise BridgeError("no-response max_write must be >=58; row frames will not be split")
        # Require explicit capabilities from the actual discovered characteristic.
        for capability in ("notify", "write_without_response"):
            if ready.get(capability) is not True:
                raise BridgeError("bridge characteristic lacks " + capability)
        if ready.get("write_mode") != "without_response":
            raise BridgeError("bridge must use no-response BLE writes")
        self.max_write = maximum

    def _deadline(self, timeout):
        end = time.monotonic() + timeout
        return min(end, self.operation_deadline) if self.operation_deadline is not None else end

    def _event(self, deadline, startup=False):
        event = self.session.next_event(deadline)
        kind = event.get("event")
        if kind == "data":
            value = event.get("hex")
            if not isinstance(value, str) or len(value) % 2:
                raise BridgeError("invalid bridge data hex")
            try:
                incoming = bytes.fromhex(value)
                # Observed standalone BLE side-channel notification, not a framed ACK.
                # Never drop four zero bytes belonging to an incomplete protocol frame.
                if (incoming == b"\x00" * 4 and not self.buffer and self.parser_boundary is not None
                        and self.parser_boundary()):
                    self.out_of_band_notifications.append({"hex": value, "reason": "standalone zero notification between frames"})
                else:
                    self.buffer.extend(incoming)
            except ValueError:
                raise BridgeError("invalid bridge data hex")
        elif kind == "ready" and startup and self.ready is None:
            self.ready = event
        elif kind == "written":
            if type(event.get("id")) is not int or event["id"] != self.waiting_id:
                raise BridgeError("unexpected bridge written id")
            self.acknowledged_id = event["id"]
        elif kind in ("error", "disconnected", "disconnect"):
            raise BridgeError("native helper %s: %s" % (kind, event.get("message", "link lost")))
        else:
            raise BridgeError("unexpected bridge event: " + json.dumps(event, ensure_ascii=False))

    def read(self, size):
        if size <= 0:
            return b""
        end = self._deadline(self.timeout)
        while not self.buffer:
            try:
                self._event(end)
            except TimeoutError:
                return b""  # normal timed read; usb.Link owns the protocol deadline
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def write(self, raw):
        if len(raw) > self.max_write:
            raise BridgeError("frame exceeds no-response payload; refusing to split")
        end = self._deadline(self.write_timeout)
        if self.last_write_at is not None:
            pause = max(0, self.last_write_at + 0.01 - time.monotonic())
            if time.monotonic() + pause >= end:
                raise TimeoutError("BLE write pacing deadline exceeded")
            time.sleep(pause)
        if time.monotonic() >= end:
            raise TimeoutError("BLE operation deadline exceeded")
        self.waiting_id = self.next_id
        self.next_id += 1
        self.acknowledged_id = None
        self.session.send({"op": "write", "id": self.waiting_id, "hex": bytes(raw).hex()})
        self.last_write_at = time.monotonic()
        while self.acknowledged_id != self.waiting_id:
            self._event(end)  # preserve data received before helper write acknowledgement
        self.waiting_id = None
        return len(raw)

    def close(self):
        self.session.close()


def scan(helper, seconds, diagnostics):
    session = HelperSession([helper, "scan", "--seconds", str(seconds)], diagnostics)
    found = []
    try:
        end = time.monotonic() + seconds + 15
        while True:
            event = session.next_event(end)
            kind = event.get("event")
            if kind == "discovered":
                device_id(event.get("device"))
                signal = event.get("rssi", event.get("RSSI"))
                if not isinstance(event.get("name"), (str, type(None))) or type(signal) is not int:
                    raise BridgeError("discovered event must contain name and RSSI")
                found = [old for old in found if old["device"] != event["device"]]
                found.append(event)
            elif kind in ("complete", "scan_complete"):
                return {"scan_completed": True, "discovered": found, "protocol_completed": False}
            elif kind == "error":
                raise BridgeError("native helper scan error: " + str(event.get("message")))
            else:
                raise BridgeError("unexpected scan event: " + json.dumps(event))
    finally:
        session.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--seconds", type=int, choices=range(1, 16), default=8)
    for command in ("status", "print"):
        p = sub.add_parser(command)
        p.add_argument("--device", required=True)
        p.add_argument("--expect-serial", required=True)
        if command == "print":
            p.add_argument("--image", type=Path, required=True)
            p.add_argument("--execute", action="store_true", required=True)
    for p in (s, *[sub.choices[c] for c in ("status", "print")]):
        p.add_argument("--helper", required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    report = {"command": args.command, "transport": "BLE native bridge", "ok": False,
              "physical_output": "unverified", "protocol_completed": False, "bridge_diagnostics": [], "events": []}
    if args.command == "print":
        report.update(page_acknowledged=False, page_counter=None, print_end_acknowledged=False,
                      possible_partial_output=False, copies=1)
    session = None
    try:
        helper = helper_path(args.helper)
        image = None
        if args.command == "print":
            from PIL import Image
            image = Image.open(args.image)
            image.load()
            if image.format != "PNG" or image.mode != "1" or image.size != (usb.W, usb.H):
                raise ValueError("native 1-bit 354x236 PNG required; no resize/dither")
            report["image_sha256"] = hashlib.sha256(args.image.read_bytes()).hexdigest()
        check_app_not_running()
        if args.command == "scan":
            report.update(scan(helper, args.seconds, report["bridge_diagnostics"]))
        else:
            requested = device_id(args.device)
            session = HelperSession([helper, "bridge", "--device", requested], report["bridge_diagnostics"])
            transport = BridgeTransport(session, requested)
            report["bridge_ready"] = transport.ready
            deadline = time.monotonic() + (40 if args.command == "print" else 15)
            transport.operation_deadline = deadline
            link = usb.Link(transport, deadline, report["events"])
            transport.parser_boundary = lambda: not link.parser.buffer
            report["bridge_out_of_band"] = transport.out_of_band_notifications
            link.pump(0.02)
            report["printer"] = usb.query_status(link, args.expect_serial)
            report["ready"] = report["printer"]["cover_closed"] and report["printer"]["paper_present"]
            if not report["ready"]:
                raise usb.ProtocolError("cover open or no paper")
            if args.command == "print":
                usb.print_one(link, image, report["printer"], report)
            report["protocol_completed"] = True
        report["ok"] = True
    except Exception as exc:
        report.update(error=str(exc), automatic_retry=False)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception as exc:
                report.update(ok=False, close_error=str(exc))
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
