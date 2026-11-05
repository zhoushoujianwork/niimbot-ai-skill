import contextlib
import io
import json
from pathlib import Path
import queue
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import niimbot_ble as ble
import niimbot_usb as usb
from PIL import Image

DEVICE = "00000000-0000-4000-8000-000000000001"


def ready(**updates):
    result = {"event": "ready", "device": DEVICE, "name": "B1 Pro test",
              "service": ble.SERVICE, "characteristic": ble.CHARACTERISTIC, "max_write": 182,
              "notify": True, "write_without_response": True, "write_mode": "without_response"}
    result.update(updates)
    return result


class FakeSession:
    def __init__(self, events=None, workflow=False):
        self.events = queue.Queue()
        for item in events or [ready()]:
            self.events.put(item)
        self.sent = []
        self.closed = False
        self.workflow = workflow

    def next_event(self, deadline):
        try:
            return self.events.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError("fake event timeout")

    def send(self, obj):
        self.sent.append(obj)
        if self.workflow:
            raw = bytes.fromhex(obj["hex"])
            if raw[0] == 3:
                raw = raw[1:]
            cmd, data = raw[2], raw[4:-3]
            responses = {(0xC1, b"\x01"): (0xC2, b"\x03"),
                         (0xA5, b"\x01"): (0xB5, b"\0" * 11 + b"\x03\x02"),
                         (0x40, b"\x08"): (0x48, b"\x10\x01"),
                         (0x40, b"\x0b"): (0x4B, b"UNIT123456"),
                         (0xDC, b"\x03"): (0xDE, bytes.fromhex("02010210024003020100")),
                         (0xDC, b"\x04"): (0xD9, bytes.fromhex("1dde2847000001000000000000"))}
            response = responses.get((cmd, data))
            if response is None:
                ack = {0x21: 0x31, 0x23: 0x33, 0x01: 0x02, 0x13: 0x14, 0xE3: 0xE4, 0xF3: 0xF4}.get(cmd)
                if ack:
                    response = (ack, b"\x01\x00" if cmd == 0x13 else b"\x01")
                elif cmd == 0xA3:
                    response = (0xB3, b"\x00\x01\x00\x00")  # percentages may reset on completion
            if response:
                packet = usb.frame(*response)
                # Notify fragments arrive before native write acceptance event.
                for part in (packet[:1], packet[1:4], packet[4:]):
                    self.events.put({"event": "data", "hex": part.hex()})
            self.events.put({"event": "written", "id": obj["id"]})

    def close(self):
        self.closed = True


class BridgeTests(unittest.TestCase):
    def test_zero_side_notification_only_filtered_between_frames(self):
        packet = usb.frame(0xB3, b"\x00" * 4)
        session = FakeSession([ready(), {"event": "data", "hex": "00000000"},
                               {"event": "data", "hex": packet.hex()}])
        transport = ble.BridgeTransport(session, DEVICE)
        parser = usb.Parser()
        transport.parser_boundary = lambda: not parser.buffer
        self.assertEqual(parser.feed(transport.read(4096)), [(0xB3, b"\x00" * 4)])
        self.assertEqual(len(transport.out_of_band_notifications), 1)
        session = FakeSession([ready(), {"event": "data", "hex": packet[:4].hex()},
                               {"event": "data", "hex": "00000000"},
                               {"event": "data", "hex": packet[-3:].hex()}])
        transport = ble.BridgeTransport(session, DEVICE)
        parser = usb.Parser()
        transport.parser_boundary = lambda: not parser.buffer
        self.assertEqual(parser.feed(transport.read(4096)), [])
        self.assertEqual(parser.feed(transport.read(4096)), [])
        self.assertEqual(parser.feed(transport.read(4096)), [(0xB3, b"\x00" * 4)])
        self.assertEqual(transport.out_of_band_notifications, [])

    def test_notify_fragments_and_coalesced_packets(self):
        a = usb.frame(0x48, b"\x10\x01")
        b = usb.frame(0xE4, b"\x01")
        session = FakeSession([ready(), {"event": "data", "hex": a[:1].hex()},
                               {"event": "data", "hex": (a[1:] + b).hex()}])
        transport = ble.BridgeTransport(session, DEVICE)
        parser = usb.Parser()
        self.assertEqual(parser.feed(transport.read(4096)), [])
        self.assertEqual(parser.feed(transport.read(4096)), [(0x48, b"\x10\x01"), (0xE4, b"\x01")])

    def test_data_preserved_while_waiting_written_and_write_no_retry(self):
        packet = usb.frame(0xC2, b"\x03")
        session = FakeSession([ready(), {"event": "data", "hex": packet.hex()}, {"event": "written", "id": 1}])
        transport = ble.BridgeTransport(session, DEVICE)
        request = usb.frame(0xC1, b"\x01")
        self.assertEqual(transport.write(request), len(request))
        self.assertEqual(transport.read(2) + transport.read(4096), packet)
        self.assertEqual(session.sent, [{"op": "write", "id": 1, "hex": request.hex()}])

    def test_helper_error_disconnect_wrong_ack_and_timeout(self):
        for event in ({"event": "error", "message": "denied"}, {"event": "disconnected"},
                      {"event": "written", "id": 99}, {"event": "surprise"}):
            with self.subTest(event=event):
                session = FakeSession([ready(), event])
                transport = ble.BridgeTransport(session, DEVICE)
                with self.assertRaises(ble.BridgeError):
                    transport.write(usb.frame(0xC1, b"\x01"))
                self.assertEqual(len(session.sent), 1)
        session = FakeSession()
        transport = ble.BridgeTransport(session, DEVICE)
        transport.write_timeout = 0.005
        with self.assertRaises(TimeoutError):
            transport.write(usb.frame(0xC1, b"\x01"))
        self.assertEqual(len(session.sent), 1)

    def test_reject_insufficient_payload_identity_or_capability(self):
        for event in (ready(max_write=20), ready(max_write=True), ready(service="wrong"),
                      ready(characteristic="wrong"), ready(device=None), ready(device="00000000-0000-4000-8000-000000000002"),
                      ready(write_without_response=False), ready(notify=False), ready(write_mode="with_response")):
            with self.subTest(event=event), self.assertRaises(ble.BridgeError):
                ble.BridgeTransport(FakeSession([event]), DEVICE)
        session = FakeSession([ready(max_write=58)])
        transport = ble.BridgeTransport(session, DEVICE)
        with self.assertRaisesRegex(ble.BridgeError, "refusing to split"):
            transport.write(b"x" * 59)
        self.assertEqual(session.sent, [])

    def test_app_guard_and_scan_error_do_not_launch_real_helper(self):
        with patch.object(ble.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="/Applications/NIIMBOT.app/Contents/MacOS/NIIMBOT\n")):
            with self.assertRaisesRegex(ble.BridgeError, "quit NIIMBOT App"):
                ble.check_app_not_running()
        fake = FakeSession([{"event": "error", "message": "Bluetooth denied"}])
        with patch.object(ble, "HelperSession", return_value=fake):
            with self.assertRaisesRegex(ble.BridgeError, "Bluetooth denied"):
                ble.scan("/fake/helper", 8, [])
        self.assertTrue(fake.closed)

    def test_complete_printer_workflow_through_bridge_no_usb(self):
        session = FakeSession(workflow=True)
        transport = ble.BridgeTransport(session, DEVICE)
        transport.operation_deadline = time.monotonic() + 40
        link = usb.Link(transport, transport.operation_deadline)
        with patch.object(usb, "open_transport", side_effect=AssertionError("USB forbidden")) as no_usb:
            status = usb.query_status(link, "B1 Pro-UNIT123456")
            report = {}
            usb.print_one(link, Image.new("1", (usb.W, usb.H), 1), status, report)
            no_usb.assert_not_called()
        commands = [bytes.fromhex(item["hex"])[3 if item["hex"].startswith("03") else 2] for item in session.sent]
        self.assertEqual(commands.count(0x01), 1)
        self.assertEqual(commands.count(0xE3), 1)
        self.assertEqual(commands.count(0xF3), 1)
        self.assertTrue(report["page_acknowledged"] and report["print_end_acknowledged"])
        self.assertEqual(report["page_counter"], 1)
        self.assertEqual(report["physical_output"], "unverified")

    def test_native_json_reader_transcript_and_eof(self):
        diagnostics = []
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(ready()) + "\n"),
                                  stderr=io.StringIO("native diagnostic\n"), poll=lambda: 0,
                                  wait=lambda timeout: 0)
        session = ble.HelperSession(["fake-helper"], diagnostics, popen=lambda *a, **kw: process)
        event = session.next_event(time.monotonic() + 1)
        self.assertEqual(event["event"], "ready")
        with self.assertRaisesRegex(ble.BridgeError, "EOF"):
            session.next_event(time.monotonic() + 1)
        session.close()
        self.assertTrue(any(item.get("raw") == "native diagnostic" for item in diagnostics))
        self.assertTrue(any(item.get("json", {}).get("event") == "ready" for item in diagnostics))

    def test_malformed_helper_json_fails_with_diagnostic(self):
        diagnostics = []
        process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO("not-json\n"),
                                  stderr=io.StringIO(), poll=lambda: 0, wait=lambda timeout: 0)
        session = ble.HelperSession(["fake-helper"], diagnostics, popen=lambda *a, **kw: process)
        with self.assertRaisesRegex(ble.BridgeError, "event mismatch"):
            session.next_event(time.monotonic() + 1)
        session.close()
        self.assertEqual(diagnostics[0]["raw"], "not-json")

    def test_cli_status_and_scan_have_separate_completion(self):
        session = FakeSession(workflow=True)
        out = io.StringIO()
        with patch.object(ble, "helper_path", return_value="/fake/helper"), patch.object(ble, "check_app_not_running"), patch.object(
                ble, "HelperSession", return_value=session), patch.object(usb, "open_transport", side_effect=AssertionError("USB forbidden")), contextlib.redirect_stdout(out):
            code = ble.main(["status", "--device", DEVICE, "--expect-serial", "B1 Pro-UNIT123456", "--helper", "/fake/helper"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["protocol_completed"])
        scan_session = FakeSession([{"event": "discovered", "device": DEVICE, "name": "test", "RSSI": -50}, {"event": "complete"}])
        with patch.object(ble, "HelperSession", return_value=scan_session):
            report = ble.scan("/fake/helper", 8, [])
        self.assertTrue(report["scan_completed"])
        self.assertFalse(report["protocol_completed"])
        self.assertTrue(scan_session.closed)


if __name__ == "__main__":
    unittest.main()
