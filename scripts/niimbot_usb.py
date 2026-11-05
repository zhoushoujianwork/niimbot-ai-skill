#!/usr/bin/env python3
"""B1 Pro USB only. Rendering is offline; hardware writes require print --execute."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import time

W, H, DPI = 354, 236, 300
DEFAULT_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
DEFAULT_ID_FONT = "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf"


class ProtocolError(RuntimeError):
    pass


def frame(cmd, data=b""):
    data = bytes(data)
    if not 0 <= cmd <= 255 or len(data) > 255:
        raise ValueError("invalid command/payload length")
    check = cmd ^ len(data)
    for b in data:
        check ^= b
    packet = b"\x55\x55" + bytes((cmd, len(data))) + data + bytes((check,)) + b"\xaa\xaa"
    return (b"\x03" + packet) if cmd == 0xC1 else packet


class Parser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        packets = []
        while self.buffer:
            if self.buffer[0] != 0x55 or (len(self.buffer) > 1 and self.buffer[1] != 0x55):
                raise ProtocolError("bad frame header; raw input preserved in events")
            if len(self.buffer) < 4:
                break
            size = self.buffer[3] + 7
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            cmd, length = raw[2:4]
            check = cmd ^ length
            for b in raw[4:-3]:
                check ^= b
            if raw[-2:] != b"\xaa\xaa" or check != raw[-3]:
                raise ProtocolError("bad frame tail/checksum; raw input preserved in events")
            del self.buffer[:size]
            packets.append((cmd, raw[4:-3]))
        return packets


class Link:
    def __init__(self, transport, deadline, events=None, clock=time.monotonic):
        self.transport, self.deadline, self.clock = transport, deadline, clock
        self.events = events if events is not None else []
        self.parser = Parser()
        self.pending = []

    def remaining(self):
        left = self.deadline - self.clock()
        if left <= 0:
            raise TimeoutError("total operation deadline exceeded; do not automatically retry")
        return left

    def send(self, cmd, data=b"\x01"):
        self.remaining()
        raw = frame(cmd, data)
        self.events.append({"direction": "tx", "hex": raw.hex()})
        self.transport.write_timeout = min(0.5, self.remaining())
        written = self.transport.write(raw)
        if written != len(raw):
            raise ProtocolError("short serial write; possible partial output")

    def pump(self, wait=0.02):
        self.transport.timeout = min(wait, self.remaining())
        raw = self.transport.read(4096)
        if raw:
            self.events.append({"direction": "rx", "hex": raw.hex()})
            packets = self.parser.feed(raw)
            self.pending.extend(packets)
            for cmd, data in packets:
                if cmd in (0x00, 0xDB):
                    raise ProtocolError("printer error response 0x%02x: %s" % (cmd, data.hex()))

    def request(self, cmd, data, response, timeout=1.5, ack=False):
        # A response already received cannot acknowledge the next command.
        old_pending = len(self.pending)
        self.send(cmd, data)
        end = min(self.deadline, self.clock() + timeout)
        while self.clock() < end:
            for index in range(old_pending, len(self.pending)):
                rx_cmd, payload = self.pending[index]
                if rx_cmd == response:
                    self.pending.pop(index)
                    # B1 Pro firmware 2.16 reports accepted page dimensions as 01 00.
                    valid_ack = payload == b"\x01" or (response == 0x14 and payload == b"\x01\x00")
                    if ack and not valid_ack:
                        raise ProtocolError("negative/malformed ACK 0x%02x: %s" % (response, payload.hex()))
                    return payload
            self.pump(min(0.05, max(0.001, end - self.clock())))
        raise TimeoutError("response 0x%02x timed out; no retry" % response)


def query_status(link, expect_serial):
    connect = link.request(0xC1, b"\x01", 0xC2)
    if connect != b"\x03":
        raise ProtocolError("unsupported connect result: " + connect.hex())
    status = link.request(0xA5, b"\x01", 0xB5)
    if len(status) < 13:
        raise ProtocolError("short printer status")
    version = status[11] * 100 + status[12]
    if version < 300:
        raise ProtocolError("B1 Pro v4/v5 protocol required")
    model_raw = link.request(0x40, b"\x08", 0x48)
    if model_raw != b"\x10\x01":
        raise ProtocolError("expected B1 Pro model 4097, got " + model_raw.hex())
    serial = link.request(0x40, b"\x0b", 0x4B).decode("ascii", errors="strict")
    # USB descriptor includes marketing model, protocol serial does not.
    expected = expect_serial[len("B1 Pro-"):] if expect_serial.startswith("B1 Pro-") else expect_serial
    if serial != expected:
        raise ProtocolError("printer serial mismatch")
    info = link.request(0xDC, b"\x03", 0xDE)
    if len(info) != 10:
        raise ProtocolError("unknown printer-info layout")
    head = int.from_bytes(info[4:6], "big")
    if info[6] != 3 or not W <= head <= 768:
        raise ProtocolError("unexpected resolution/head width: %s/%s" % (info[6], head))
    hb = link.request(0xDC, b"\x04", 0xD9)
    if len(hb) != 13 or hb[4] not in (0, 1) or hb[5] not in (0, 1):
        raise ProtocolError("unknown B1 Pro heartbeat layout")
    return {"model_id": 4097, "serial": serial, "protocol": 5 if version >= 302 else 4,
            "firmware_version": "%d.%02d" % (info[2], info[3]),
            "hardware_version": "%d.%02d" % (info[0], info[1]),
            "chipset": None,
            "dpi": DPI, "printhead_pixels": head, "cover_closed": hb[4] == 0,
            "paper_present": hb[5] == 0, "paper_rfid_read": hb[6] != 0,
            "stock_mm": [30, 20], "stock_basis": "explicit configuration, not paper-size query"}


def raster_rows(image, printhead_pixels):
    """niimbluelib auto counts: thirds of head, total fallback if stride exceeds thirds."""
    if image.mode != "1" or image.size != (W, H):
        raise ValueError("native 1-bit 354x236 image required; no resize/dither")
    chunk = printhead_pixels // 8 // 3
    if chunk < 1:
        raise ValueError("invalid printhead width")
    stride = (W + 7) // 8
    for y in range(H):
        row = bytearray(stride)
        for x in range(W):
            if image.getpixel((x, y)) == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        if not any(row):
            yield 0x84, struct.pack(">HB", y, 1)
            continue
        counts = [sum(bin(b).count("1") for b in row[i * chunk:(i + 1) * chunk]) for i in range(3)]
        if stride > chunk * 3:
            total = sum(bin(b).count("1") for b in row)
            counts = [0, total & 255, total >> 8]
        if max(counts) > 255:
            raise ValueError("printhead pixel-count chunk overflow")
        yield 0x85, struct.pack(">H", y) + bytes(counts) + b"\x01" + bytes(row)


def print_one(link, image, status, report):
    if not status["cover_closed"] or not status["paper_present"]:
        raise ProtocolError("cover open or no paper; printing refused")
    # Finish encoding/validation before any print setup writes.
    rows = list(raster_rows(image, status["printhead_pixels"]))
    report.update(page_acknowledged=False, page_counter=None, print_end_acknowledged=False,
                  physical_output="unverified", possible_partial_output=False, copies=1)
    link.request(0x21, b"\x03", 0x31, ack=True)
    link.request(0x23, b"\x01", 0x33, ack=True)
    report["possible_partial_output"] = True  # ambiguity begins with PrintStart transmission
    link.request(0x01, b"\x00\x01\x00\x00\x00\x00\x00\x01\x00", 0x02, ack=True)
    link.send(0xA3)
    link.pump(0.03)
    link.request(0x13, struct.pack(">HHH", H, W, 1) + b"\x00" * 7, 0x14, ack=True)
    for cmd, data in rows:
        link.send(cmd, data)
        link.pump(0.01)  # preserve asynchronous feedback and pace every row
    link.request(0xE3, b"\x01", 0xE4, timeout=10, ack=True)
    report["page_acknowledged"] = True
    while True:
        progress = link.request(0xA3, b"\x01", 0xB3, timeout=1)
        if len(progress) < 4 or progress[2] > 100 or progress[3] > 100:
            raise ProtocolError("malformed print progress")
        count, printed, fed = struct.unpack(">HBB", progress[:4])
        report.update(page_counter=count, print_percent=printed, feed_percent=fed)
        # Upstream completes on the page counter; percentages can reset on completion.
        if count >= 1:
            break
        link.pump(0.1)
    link.request(0xF3, b"\x01", 0xF4, ack=True)
    report["print_end_acknowledged"] = True
    report["possible_partial_output"] = False


def assert_port_available(port):
    executable = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else None)
    if not executable:
        raise RuntimeError("lsof unavailable; cannot establish exclusive port availability")
    result = subprocess.run([executable, "-t", "--", port], capture_output=True, text=True, timeout=3)
    if result.stdout.strip():
        raise RuntimeError("port occupied (PID %s); no process was terminated" % result.stdout.strip().replace("\n", ","))
    if result.returncode != 1 or result.stderr.strip():
        raise RuntimeError("lsof occupancy check inconclusive: " + result.stderr.strip())


def open_transport(port, expect_serial):
    import serial
    from serial.tools.list_ports import comports
    processes = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True, timeout=3)
    if processes.returncode != 0:
        raise RuntimeError("cannot check competing NIIMBOT process")
    if any(name.strip().endswith("/NIIMBOT.app/Contents/MacOS/NIIMBOT") or name.strip() == "NIIMBOT"
           for name in processes.stdout.splitlines()):
        raise RuntimeError("quit NIIMBOT App before direct USB; its vendor USB access may not appear in lsof")
    matches = [p for p in comports() if p.device == port]
    if len(matches) != 1:
        raise RuntimeError("port missing or ambiguous")
    p = matches[0]
    if (p.vid, p.pid) != (0x3513, 0x0002) or p.serial_number != expect_serial:
        raise RuntimeError("USB descriptor identity mismatch")
    # /dev/tty.* and /dev/cu.* may refer to the same macOS device.
    assert_port_available(port)
    if port.startswith("/dev/cu."):
        assert_port_available(port.replace("/dev/cu.", "/dev/tty.", 1))
    class PreserveInputSerial(serial.Serial):
        def _reset_input_buffer(self):
            # POSIX pyserial.open calls this; do not tcflush away prior events.
            pass

    device = PreserveInputSerial(port=None, baudrate=115200, timeout=0.05, write_timeout=0.5, exclusive=True)
    device.dtr = False
    device.rts = False
    device.port = port
    device.open()
    return device


def render(job, output, font=DEFAULT_FONT, id_font=DEFAULT_ID_FONT):
    from PIL import Image, ImageDraw, ImageFont
    import qrcode
    import cv2
    import numpy as np
    if not job.is_absolute() or not output.is_absolute():
        raise ValueError("job/output must be absolute paths")
    identity = json.loads(job.read_text())
    for key in ("device_id", "model", "hardware_revision", "firmware", "core", "variant"):
        if not isinstance(identity.get(key), str) or not identity[key].strip():
            raise ValueError("missing identity field " + key)
    if (identity.get("width_mm"), identity.get("height_mm"), identity.get("copies")) != (30, 20, 1):
        raise ValueError("only explicit 30x20mm / one-copy jobs supported")
    payload = identity["device_id"]
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=4)
    qr.add_data(payload)
    qr.make(fit=False)
    qr_image = qr.make_image(fill_color="black", back_color="white").convert("1")
    if qr_image.size != (116, 116):
        raise ValueError("QR must fit version1 with four-pixel modules and four-module quiet zone")
    image = Image.new("1", (W, H), 1)
    boxes, sizes = {}, {}

    def text(name, value, xy, size, path, max_width):
        chosen = ImageFont.truetype(path, size)
        box = chosen.getbbox(value)
        width, height = box[2] - box[0], box[3] - box[1]
        if width > max_width:
            raise ValueError(name + " does not fit; adjust configured font, never truncate")
        tile = Image.new("L", (width, height), 255)
        ImageDraw.Draw(tile).text((-box[0], -box[1]), value, font=chosen, fill=0)
        tile = tile.point(lambda p: 255 if p >= 128 else 0, mode="1")
        image.paste(tile, xy)
        boxes[name] = [xy[0], xy[1], xy[0] + width, xy[1] + height]
        sizes[name] = size

    text("model_revision", identity["model"] + " " + identity["hardware_revision"], (12, 14), 26, font, 330)
    text("firmware", identity["firmware"], (12, 67), 22, font, 202)
    text("core", "core " + identity["core"], (12, 116), 22, font, 202)
    image.paste(qr_image, (226, 52))
    boxes["qr_with_quiet_zone"] = [226, 52, 342, 168]
    text("device_id", payload, (12, 192), 28, id_font, 330)
    for name, a in boxes.items():
        if a[0] < 12 or a[1] < 12 or a[2] > W - 12 or a[3] > H - 12:
            raise ValueError(name + " out of bounds")
        for other, b in boxes.items():
            if other != name and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                raise ValueError("overlap: " + name + "/" + other)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(np.array(image.convert("L")))
    if decoded != payload:
        raise ValueError("independent cv2 QR decode mismatch")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    image.save(output / "label.png", dpi=(DPI, DPI))
    nearest = getattr(Image, "Resampling", Image).NEAREST
    image.resize((W * 4, H * 4), nearest).save(output / "preview.png")
    metadata = {"identity": {k: identity[k] for k in ("device_id", "model", "variant", "hardware_revision", "firmware", "core")},
                "dpi": DPI, "pixels": [W, H], "stock_mm": [30, 20], "mode": "1", "font_sizes_px": sizes,
                "font_paths": {"text": font, "device_id": id_font}, "boxes": boxes, "qr_payload": payload,
                "qr": {"module_pixels": 4, "quiet_zone_modules": 4, "version": 1, "decoded_exact": True},
                "label_sha256": hashlib.sha256((output / "label.png").read_bytes()).hexdigest(), "physical_output": "unverified"}
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render")
    r.add_argument("--job", type=Path, required=True)
    r.add_argument("--output", type=Path, required=True)
    r.add_argument("--font", default=DEFAULT_FONT)
    r.add_argument("--id-font", default=DEFAULT_ID_FONT)
    for command in ("status", "print"):
        p = sub.add_parser(command)
        p.add_argument("--port", required=True)
        p.add_argument("--expect-serial", required=True)
        if command == "print":
            p.add_argument("--image", type=Path, required=True)
            p.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    report = {"command": args.command, "ok": False, "physical_output": "unverified", "events": []}
    if args.command == "print":
        report.update(page_acknowledged=False, page_counter=None, print_end_acknowledged=False,
                      possible_partial_output=False, copies=1)
    transport = None
    started = time.monotonic()
    try:
        if args.command == "render":
            report["render"] = render(args.job, args.output, args.font, args.id_font)
        else:
            image = None
            if args.command == "print":
                from PIL import Image
                image = Image.open(args.image)
                image.load()
                if image.size != (W, H) or image.mode != "1":
                    raise ValueError("print requires native 1-bit 354x236 PNG")
                if image.format != "PNG":
                    raise ValueError("PNG required")
                report["image_sha256"] = hashlib.sha256(args.image.read_bytes()).hexdigest()
            deadline = started + (40 if args.command == "print" else 15)
            transport = open_transport(args.port, args.expect_serial)
            link = Link(transport, deadline, report["events"])
            link.pump(0.02)  # preserve queued events before issuing the handshake
            report["printer"] = query_status(link, args.expect_serial)
            report["ready"] = report["printer"]["cover_closed"] and report["printer"]["paper_present"]
            if not report["ready"]:
                raise ProtocolError("cover open or no paper")
            if args.command == "print":
                print_one(link, image, report["printer"], report)
        report["ok"] = True
    except Exception as exc:
        report["error"] = str(exc)
        report["automatic_retry"] = False
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception as exc:
                report.update(ok=False, close_error=str(exc))
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
