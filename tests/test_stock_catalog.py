import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("usb_stock", Path(__file__).parents[1] / "scripts/niimbot_usb.py")
usb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usb)


def payload(barcode=b"DEMO-SKU"):
    return bytes(8) + bytes([len(barcode)]) + barcode + b"\x04DEMO" + bytes.fromhex("0180000501")


class StockTests(unittest.TestCase):
    def identify(self, entries, raw=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps({"version": 1, "stocks": entries}))
            printer = {}
            link = Mock()
            link.request.return_value = payload() if raw is None else raw
            usb.identify_stock(link, printer, path)
            link.request.assert_called_once_with(0x1A, b"\x01", 0x1B)
            return printer

    def entry(self, **kw):
        return dict(barcode="DEMO-SKU", consumables_type=1,
                    width_mm=30, height_mm=20, source="fictional test fixture", **kw)

    def test_exact_match_and_no_capacity_inference(self):
        printer = self.identify([self.entry()])
        self.assertEqual(printer["stock_mm"], [30, 20])
        usb.require_supported_stock(printer)
        self.assertEqual(printer["rfid"]["total_labels"], 384)
        self.assertIsNone(self.identify([])["stock_mm"])

    def test_unknown_absent_wrong_type_and_unvalidated_sizes_block(self):
        wrong = self.entry(); wrong["consumables_type"] = 2
        larger = self.entry(); larger["width_mm"] = 40
        for printer in (self.identify([]), self.identify([], b"\x00"),
                        self.identify([wrong]), self.identify([larger])):
            with self.assertRaises(usb.ProtocolError):
                usb.require_supported_stock(printer)
        self.assertEqual(self.identify([larger])["stock_mm"], [40, 20])

    def test_truncation_extensions_and_utf8(self):
        raw = payload()
        for n in range(len(raw)):
            if n != 1:
                with self.assertRaises((usb.ProtocolError, UnicodeDecodeError)):
                    usb.parse_rfid(raw[:n])
        self.assertEqual(usb.parse_rfid(raw + b"\x01\x40")["capacity"], 320)
        self.assertIn("tag_uid2", usb.parse_rfid(raw + b"\x01\x40" + bytes(8)))
        with self.assertRaises(usb.ProtocolError):
            usb.parse_rfid(raw + b"\x01")
        with self.assertRaises(UnicodeDecodeError):
            usb.parse_rfid(payload(b"\xff"))

    def test_invalid_ambiguous_and_unconfirmed_catalogs(self):
        for value in (0, -1, True, float("nan"), float("inf"), "30"):
            entry = self.entry(); entry["width_mm"] = value
            with self.assertRaises(ValueError): self.identify([entry])
        with self.assertRaises(ValueError): self.identify([self.entry(), self.entry()])
        entry = self.entry(); entry["source"] = ""
        with self.assertRaises(ValueError): self.identify([entry])


if __name__ == "__main__":
    unittest.main()
