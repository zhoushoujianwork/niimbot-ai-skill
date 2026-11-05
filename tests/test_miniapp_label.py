"""Synthetic composition/provenance fixtures only; these are NOT real official codes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from PIL import Image, ImageDraw, ImageOps

spec = importlib.util.spec_from_file_location("render_miniapp_label", Path(__file__).parents[1] / "scripts/render_miniapp_label.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class MiniappLabelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-miniapp-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.picture = self.root / "synthetic-not-real-code.png"
        image = Image.new("L", (400, 400), 255)
        drawing = ImageDraw.Draw(image)
        drawing.ellipse((30, 30, 370, 370), outline=0, width=12)
        drawing.rectangle((160, 160, 240, 240), fill=0)
        for x, y in ((0, 0), (360, 0), (0, 360), (360, 360)):
            drawing.rectangle((x, y, x + 39, y + 39), fill=0)
        image.save(self.picture)
        self.source_path = self.root / "synthetic-source.json"
        self.identity = {"device_id": "UNIT-1234567890", "model": "UnitBoard", "variant": "测试版",
                         "hardware_revision": "V1.0", "firmware": "123.456.789", "core": "V1000",
                         "width_mm": 30, "height_mm": 20, "copies": 1, "website": "https://example.test",
                         "miniapp_code_path": str(self.picture), "miniapp_code_source": str(self.source_path)}
        self.source = {"official_api_response": True, "endpoint": "https://api.weixin.qq.com/wxa/getwxacode",
                       "path": "pages/test/index?" + urlencode({"device_id": self.identity["device_id"]}),
                       "env_version": "release", "image_sha256": hashlib.sha256(self.picture.read_bytes()).hexdigest(),
                       "image_size": [400, 400]}
        self.job = self.root / "job.json"
        self.write_inputs()

    def write_inputs(self):
        self.source_path.write_text(json.dumps(self.source))
        self.job.write_text(json.dumps(self.identity))

    def test_native_output_full_strings_and_uncropped_features(self):
        out = self.root / "out"
        metadata = renderer.render(self.job, out)
        with Image.open(out / "label.png") as image:
            self.assertEqual(image.mode, "1")
            self.assertEqual(image.size, (354, 236))
            self.assertAlmostEqual(image.info["dpi"][0], 300, places=2)
            for key in ("variant", "device_id", "model", "firmware"):
                self.assertEqual(metadata["visible_strings"][key], self.identity[key])
                box = metadata["boxes"][key]
                ink = ImageOps.invert(image.crop(box).convert("L")).getbbox()
                self.assertIsNotNone(ink)
                self.assertGreater(ink[2] - ink[0], 10)
                self.assertGreater(ink[3] - ink[1], 10)
            expected, _ = renderer.official_image(self.identity)
            placed = image.crop(metadata["boxes"]["miniapp_code"])
            self.assertEqual(placed.tobytes(), expected.tobytes())
            # Distinct edge features would disappear under a center crop.
            for x, y in ((3, 3), (176, 3), (3, 176), (176, 176)):
                self.assertEqual(placed.getpixel((x, y)), 0)
            self.assertEqual(placed.getpixel((90, 90)), 0)
        self.assertEqual(metadata["miniapp_code"]["code_type"], "wechat_mini_program")
        self.assertEqual(metadata["wechat_physical_scan"], "unverified")
        self.assertNotIn("core", metadata["visible_strings"])
        self.assertEqual(metadata["core_metadata_only"], self.identity["core"])
        self.assertEqual(metadata["font_sizes_px"]["variant"], 24)
        self.assertEqual(metadata["font_sizes_px"]["device_id"], 28)
        with Image.open(out / "preview.png") as preview:
            self.assertEqual(preview.size, (1416, 944))

    def test_reject_provenance_hash_page_and_environment_mismatch(self):
        cases = [("official_api_response", False), ("image_sha256", "0" * 64),
                 ("endpoint", "https://example.test/wxa/getwxacode"),
                 ("path", "pages/test/index?device_id=WRONG"),
                 ("path", "pages/test/index?device_id=UNIT-1234567890&device_id=WRONG"),
                 ("env_version", "trial")]
        original = dict(self.source)
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.source = dict(original, **{key: value})
                self.write_inputs()
                with self.assertRaises(ValueError):
                    renderer.render(self.job, self.root / "rejected")
                self.assertFalse((self.root / "rejected").exists())

    def test_hash_valid_but_invalid_image_is_rejected(self):
        self.picture.write_bytes(b"not an image")
        self.source["image_sha256"] = hashlib.sha256(self.picture.read_bytes()).hexdigest()
        self.write_inputs()
        with self.assertRaises(OSError):
            renderer.render(self.job, self.root / "rejected")

    def test_bounds_overlap_and_no_silent_shrinking(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            renderer.validate_layout({"miniapp_code": list(renderer.CODE_BOX), "device_id": [12, 185, 300, 205]})
        with self.assertRaisesRegex(ValueError, "out of bounds"):
            renderer.validate_layout({"footer": [12, 211, 342, 231]})
        with self.assertRaisesRegex(ValueError, "out of bounds"):
            renderer.validate_layout({"text": [11, 12, 140, 40]})
        for field in ("model", "variant", "device_id"):
            with self.subTest(field=field):
                modified = dict(self.identity, **{field: self.identity[field] * 20})
                # Match source ID so a long ID reaches typography validation.
                self.source["path"] = "pages/test/index?" + urlencode({"device_id": modified["device_id"]})
                self.source_path.write_text(json.dumps(self.source))
                self.job.write_text(json.dumps(modified))
                with self.assertRaisesRegex(ValueError, "too wide"):
                    renderer.render(self.job, self.root / "rejected")

    def test_website_host_is_complete_and_credential_free(self):
        self.assertEqual(renderer.website_host("https://EXAMPLE.test/"), "example.test")
        self.assertEqual(renderer.website_host("https://xn--bcher-kva.example"), "xn--bcher-kva.example")
        for value in ("http://example.test", "https://example_user@example.test", "https://example.test?token=secret",
                      "https://example.test/path", "https://example.test/#fragment", "https://bad_host.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                renderer.website_host(value)


if __name__ == "__main__":
    unittest.main()
