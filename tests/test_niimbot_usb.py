import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

spec = importlib.util.spec_from_file_location("niimbot_usb", Path(__file__).parents[1] / "scripts/niimbot_usb.py")
usb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usb)


class FakeTransport:
    def __init__(self, progress=True, nack=None, complete_progress=b"\x00\x01\x64\x64"):
        self.now = 0.0
        self.timeout = 0.05
        self.write_timeout = 0.5
        self.incoming = bytearray()
        self.sent = []
        self.progress = progress
        self.nack = nack
        self.complete_progress = complete_progress

    def write(self, raw):
        cmd = raw[2]
        self.sent.append((cmd, raw))
        response = {0x21: 0x31, 0x23: 0x33, 0x01: 0x02, 0x13: 0x14, 0xE3: 0xE4, 0xF3: 0xF4}.get(cmd)
        if response:
            self.incoming.extend(usb.frame(response, b"\x00" if cmd == self.nack else b"\x01"))
        if cmd == 0xA3:
            self.incoming.extend(usb.frame(0xB3, self.complete_progress if self.progress else b"\x00\x00\x00\x00"))
        return len(raw)

    def read(self, size):
        self.now += self.timeout
        raw = bytes(self.incoming[:size])
        del self.incoming[:size]
        return raw


class ProtocolTests(unittest.TestCase):
    def test_recorded_packet_vectors(self):
        # niimbluelib pinned B1_PRO_V2_12 dump, no private identity.
        self.assertEqual(usb.frame(0xC1, b"\x01").hex(), "035555c10101c1aaaa")
        self.assertEqual(usb.frame(0x40, b"\x08").hex(), "555540010849aaaa")
        self.assertEqual(usb.frame(0xDC, b"\x03").hex(), "5555dc0103deaaaa")
        self.assertEqual(usb.Parser().feed(bytes.fromhex("5555480210015baaaa")), [(0x48, b"\x10\x01")])

    def test_fragmented_coalesced_and_corrupt(self):
        first = usb.frame(0x48, b"\x10\x01")
        second = usb.frame(0xE4, b"\x01")
        p = usb.Parser()
        self.assertEqual(p.feed(first[:1]), [])
        self.assertEqual(p.feed(first[1:5]), [])
        self.assertEqual(p.feed(first[5:] + second), [(0x48, b"\x10\x01"), (0xE4, b"\x01")])
        bad = bytearray(first)
        bad[-3] ^= 1
        with self.assertRaises(usb.ProtocolError):
            usb.Parser().feed(bad)
        with self.assertRaises(usb.ProtocolError):
            usb.Parser().feed(b"\x00" + first)

    def test_non_byte_width_black_and_blank(self):
        image = Image.new("1", (usb.W, usb.H), 1)
        image.putpixel((0, 0), 0)
        image.putpixel((353, 0), 0)
        rows = list(usb.raster_rows(image, 576))
        cmd, data = rows[0]
        self.assertEqual(cmd, 0x85)
        self.assertEqual(data[:6], bytes((0, 0, 1, 1, 0, 1)))  # thirds of 576/8/3=24 bytes
        self.assertEqual(len(data), 51)
        self.assertEqual(data[6], 0x80)
        self.assertEqual(data[-1], 0x40)
        self.assertEqual(data[-1] & 0x3F, 0)
        self.assertEqual(rows[1], (0x84, b"\x00\x01\x01"))
        black = list(usb.raster_rows(Image.new("1", (usb.W, usb.H), 0), 576))[0][1]
        self.assertEqual(black[2:5], bytes((192, 162, 0)))
        self.assertEqual(black[-1], 0xC0)
        # Different actual head changes boundaries; a 354px head needs total fallback.
        fallback = list(usb.raster_rows(Image.new("1", (usb.W, usb.H), 0), 360))[0][1]
        self.assertEqual(fallback[2:5], bytes((120, 120, 114)))

    def test_truncated_heartbeat_from_competing_client_is_not_accepted(self):
        heartbeat = usb.frame(0xD9, bytes.fromhex("1f555049000001000000000000"))[:-2]
        ack = usb.frame(0xE4, b"\x01")
        parser = usb.Parser()
        self.assertEqual(parser.feed(heartbeat), [])
        with self.assertRaises(usb.ProtocolError):
            parser.feed(heartbeat + ack)
        corrupt = bytearray(heartbeat)
        corrupt[-1] ^= 1
        with self.assertRaises(usb.ProtocolError):
            usb.Parser().feed(corrupt + ack)
        total = list(usb.raster_rows(Image.new("1", (usb.W, usb.H), 0), 354))[0][1]
        self.assertEqual(total[2:5], bytes((0, 98, 1)))

    def link(self, fake, timeout=40):
        return usb.Link(fake, timeout, clock=lambda: fake.now)

    def test_negative_ack_stops_without_retry(self):
        fake = FakeTransport(nack=0x01)
        report = {}
        with self.assertRaises(usb.ProtocolError):
            usb.print_one(self.link(fake), Image.new("1", (usb.W, usb.H), 1),
                          {"cover_closed": True, "paper_present": True, "printhead_pixels": 576}, report)
        self.assertEqual([c for c, _ in fake.sent], [0x21, 0x23, 0x01])
        self.assertTrue(report["possible_partial_output"])
        self.assertFalse(report["page_acknowledged"])

    def test_error_response_retains_raw_event(self):
        fake = FakeTransport()
        fake.incoming.extend(usb.frame(0xDB, b"\x01"))
        link = self.link(fake)
        with self.assertRaises(usb.ProtocolError):
            link.request(0x21, b"\x03", 0x31, ack=True)
        self.assertIn({"direction": "rx", "hex": (usb.frame(0xDB, b"\x01") + usb.frame(0x31, b"\x01")).hex()}, link.events)
        self.assertEqual(len(fake.sent), 1)

    def test_stale_ack_cannot_acknowledge_next_command(self):
        fake = FakeTransport()
        link = self.link(fake)
        link.pending.append((0x31, b"\x01"))
        fake.nack = 0x21
        with self.assertRaises(usb.ProtocolError):
            link.request(0x21, b"\x03", 0x31, ack=True)
        self.assertEqual(link.pending, [(0x31, b"\x01")])

    def test_occupied_port_and_no_implicit_input_flush(self):
        with patch.object(usb.shutil, "which", return_value="/mock/lsof"), patch.object(
                usb.subprocess, "run", return_value=SimpleNamespace(stdout="123\n", stderr="", returncode=0)):
            with self.assertRaisesRegex(RuntimeError, "occupied"):
                usb.assert_port_available("/dev/cu.unit")

        class FakeSerial:
            def __init__(self, **options):
                self.options = options
                self.flushed = False
                self.opened = False

            def _reset_input_buffer(self):
                self.flushed = True

            def open(self):
                self._reset_input_buffer()  # mimic installed pyserial.open()
                self.opened = True

        port = SimpleNamespace(device="/dev/cu.unit", vid=0x3513, pid=2, serial_number="B1 Pro-UNIT123456")
        with patch("serial.Serial", FakeSerial), patch("serial.tools.list_ports.comports", return_value=[port]), patch.object(
                usb, "assert_port_available") as check:
            device = usb.open_transport(port.device, port.serial_number)
            self.assertTrue(device.opened)
            self.assertFalse(device.flushed)
            self.assertTrue(device.options["exclusive"])
            self.assertEqual([c.args[0] for c in check.call_args_list], ["/dev/cu.unit", "/dev/tty.unit"])

    def test_timeout_is_bounded_and_no_restart(self):
        fake = FakeTransport(progress=False)
        report = {}
        with self.assertRaises(TimeoutError):
            usb.print_one(self.link(fake, 5), Image.new("1", (usb.W, usb.H), 1),
                          {"cover_closed": True, "paper_present": True, "printhead_pixels": 576}, report)
        self.assertLessEqual(fake.now, 5.001)
        self.assertEqual(sum(c == 0x01 for c, _ in fake.sent), 1)
        self.assertEqual(sum(c == 0xE3 for c, _ in fake.sent), 1)
        self.assertEqual(sum(c == 0xF3 for c, _ in fake.sent), 0)
        self.assertTrue(report["page_acknowledged"])
        self.assertFalse(report["print_end_acknowledged"])

    def test_success_sequence_and_dimensions(self):
        fake = FakeTransport()
        report = {}
        usb.print_one(self.link(fake), Image.new("1", (usb.W, usb.H), 1),
                      {"cover_closed": True, "paper_present": True, "printhead_pixels": 576}, report)
        commands = [c for c, _ in fake.sent]
        self.assertEqual(commands[:5], [0x21, 0x23, 0x01, 0xA3, 0x13])
        self.assertEqual(commands[-3:], [0xE3, 0xA3, 0xF3])
        self.assertNotIn(0x03, commands)
        self.assertNotIn(0x20, commands)
        dimensions = [raw[4:-3] for c, raw in fake.sent if c == 0x13][0]
        self.assertEqual(dimensions, struct.pack(">HHH", 236, 354, 1) + b"\0" * 7)
        self.assertTrue(report["page_acknowledged"] and report["print_end_acknowledged"])
        self.assertEqual(report["physical_output"], "unverified")

    def test_completed_page_with_reset_percentages_finishes_without_reprint(self):
        fake = FakeTransport(complete_progress=b"\x00\x01\x00\x00")
        report = {}
        usb.print_one(self.link(fake), Image.new("1", (usb.W, usb.H), 1),
                      {"cover_closed": True, "paper_present": True, "printhead_pixels": 576}, report)
        self.assertTrue(report["print_end_acknowledged"])
        self.assertEqual(report["page_counter"], 1)
        self.assertEqual(sum(c == 0x01 for c, _ in fake.sent), 1)

    def test_observed_b1pro_page_dimensions_ack_extension(self):
        class ExtendedPageAck(FakeTransport):
            def write(self, raw):
                n = super().write(raw)
                if raw[2] == 0x13:
                    self.incoming[:] = usb.frame(0x14, b"\x01\x00")
                return n
        fake = ExtendedPageAck()
        report = {}
        usb.print_one(self.link(fake), Image.new("1", (usb.W, usb.H), 1),
                      {"cover_closed": True, "paper_present": True, "printhead_pixels": 576}, report)
        self.assertTrue(report["print_end_acknowledged"])

    def test_status_handshake_identity_and_no_print_commands(self):
        class StatusTransport(FakeTransport):
            def write(self, raw):
                if raw[0] == 3:
                    raw = raw[1:]
                cmd, data = raw[2], raw[4:-3]
                self.sent.append((cmd, raw))
                replies = {(0xC1, b"\x01"): (0xC2, b"\x03"),
                           (0xA5, b"\x01"): (0xB5, b"\0" * 11 + b"\x03\x02"),
                           (0x40, b"\x08"): (0x48, b"\x10\x01"),
                           (0x40, b"\x0b"): (0x4B, b"UNIT123456"),
                           (0xDC, b"\x03"): (0xDE, bytes.fromhex("0201020c024003020100")),
                           (0xDC, b"\x04"): (0xD9, bytes.fromhex("1dde2847000001000000000000"))}
                response, payload = replies[(cmd, data)]
                self.incoming.extend(usb.frame(response, payload))
                return len(raw) + (1 if cmd == 0xC1 else 0)
        fake = StatusTransport()
        status = usb.query_status(self.link(fake), "B1 Pro-UNIT123456")
        self.assertEqual(status["printhead_pixels"], 576)
        self.assertEqual(status["dpi"], 300)
        self.assertEqual(status["firmware_version"], "2.12")
        self.assertEqual(status["hardware_version"], "2.01")
        self.assertIsNone(status["chipset"])
        self.assertTrue(status["cover_closed"] and status["paper_present"])
        self.assertEqual(set(c for c, _ in fake.sent), {0xC1, 0xA5, 0x40, 0xDC})
        with self.assertRaisesRegex(usb.ProtocolError, "serial mismatch"):
            fake = StatusTransport()
            usb.query_status(self.link(fake), "B1 Pro-WRONG")


if __name__ == "__main__":
    unittest.main()
