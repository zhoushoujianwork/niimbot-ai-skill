#!/usr/bin/env python3
"""Offline native label composition using a supplied official WeChat mini-program code."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw, ImageFont

W, H, DPI = 354, 236, 300
LATIN_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
NARROW_FONT = "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf"
CHINESE_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
CODE_BOX = (162, 6, 342, 186)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def website_host(value):
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("website must be an HTTPS origin")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or parsed.port not in (None, 443)):
        raise ValueError("website must be HTTPS with a host; no credentials, path, query or token")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    labels = host.split(".")
    if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("invalid website host")
    return host


def official_image(identity):
    image_path = Path(identity["miniapp_code_path"])
    source_path = Path(identity["miniapp_code_source"])
    if not image_path.is_absolute() or not source_path.is_absolute():
        raise ValueError("miniapp image and source paths must be absolute")
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    if source.get("official_api_response") is not True:
        raise ValueError("official API response provenance is required; no QR fallback")
    endpoint = urlsplit(source.get("endpoint", ""))
    if (endpoint.scheme != "https" or endpoint.netloc != "api.weixin.qq.com"
            or endpoint.path != "/wxa/getwxacode" or endpoint.query or endpoint.fragment):
        raise ValueError("unrecognized official getwxacode endpoint")
    if source.get("env_version") != "release":
        raise ValueError("release mini-program code required")
    page = source.get("path")
    if not isinstance(page, str):
        raise ValueError("missing mini-program page path")
    parsed = urlsplit(page)
    if (parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("pages/")
            or any(part in (".", "..", "") for part in parsed.path.split("/"))):
        raise ValueError("invalid mini-program page path")
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if query.get("device_id") != [identity["device_id"]]:
        raise ValueError("mini-program source device_id mismatch")
    if any(key.lower() in ("access_token", "token", "secret", "appid", "appsecret") for key in query):
        raise ValueError("credential-like mini-program query is not allowed")
    image_bytes = image_path.read_bytes()
    sha = digest(image_bytes)
    if not isinstance(source.get("image_sha256"), str) or sha != source["image_sha256"].lower():
        raise ValueError("official code image SHA256 mismatch")
    with Image.open(image_path) as original:
        original.verify()
    with Image.open(image_path) as original:
        original.load()
        if original.format not in ("JPEG", "PNG") or original.width != original.height or original.width < 180:
            raise ValueError("complete square official JPEG/PNG of at least 180px required; never crop")
        if source.get("image_size") is not None and source["image_size"] != list(original.size):
            raise ValueError("official code source image-size mismatch")
        source_format, source_size = original.format, list(original.size)
        rgba = original.convert("RGBA")
        canvas = Image.new("RGBA", original.size, "white")
        canvas.alpha_composite(rgba)
        lanczos = getattr(Image, "Resampling", Image).LANCZOS
        tile = canvas.convert("L").resize((180, 180), lanczos)
        tile = tile.point(lambda pixel: 255 if pixel >= 160 else 0, mode="1")
    evidence = {"code_type": "wechat_mini_program", "official_api_response": True,
                "endpoint": source["endpoint"], "page_path": page, "env_version": source["env_version"],
                "source_json_sha256": digest(source_bytes), "source_image_sha256": sha,
                "source_image_format": source_format, "source_image_pixels": source_size,
                "placed_pixels": [180, 180], "resize": "Lanczos, full image, no crop", "threshold": 160,
                "generic_qr_decode": "not applicable; not used as mini-program validation",
                "wechat_physical_scan": "unverified"}
    return tile, evidence


def validate_layout(boxes):
    for name, a in boxes.items():
        top = 6 if name == "miniapp_code" else 12
        side = 6 if name == "miniapp_code" else 12
        if (len(a) != 4 or a[0] < side or a[1] < top or a[2] > W - side or a[3] > 230
                or a[0] >= a[2] or a[1] >= a[3]):
            raise ValueError(name + " out of bounds")
        for other, b in boxes.items():
            if name != other and a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                raise ValueError("overlap: " + name + "/" + other)


def render(job, output, latin_font=LATIN_FONT, narrow_font=NARROW_FONT, chinese_font=CHINESE_FONT):
    job, output = Path(job), Path(output)
    if not job.is_absolute() or not output.is_absolute():
        raise ValueError("job/output must be absolute paths")
    identity = json.loads(job.read_text())
    for key in ("device_id", "model", "variant", "hardware_revision", "firmware", "core"):
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("missing/invalid identity field: " + key)
    if (identity.get("width_mm"), identity.get("height_mm"), identity.get("copies")) != (30, 20, 1):
        raise ValueError("explicit 30x20mm one-copy job required")
    host = website_host(identity["website"])
    code, evidence = official_image(identity)
    image = Image.new("1", (W, H), 1)
    boxes, sizes, strings = {"miniapp_code": list(CODE_BOX)}, {}, {}
    image.paste(code, CODE_BOX[:2])

    def text(name, value, x, y, size, font_path, max_width, stroke=0):
        font = ImageFont.truetype(font_path, size, index=0)
        b = font.getbbox(value, stroke_width=stroke)
        width, height = b[2] - b[0], b[3] - b[1]
        if width > max_width:
            raise ValueError(name + " too wide; no truncation or automatic shrinking")
        if x is None:
            x = (W - width) // 2
        box = [x, y, x + width, y + height]
        trial = dict(boxes, **{name: box})
        validate_layout(trial)
        tile = Image.new("L", (width, height), 255)
        ImageDraw.Draw(tile).text((-b[0], -b[1]), value, font=font, fill=0, stroke_width=stroke, stroke_fill=0)
        tile = tile.point(lambda pixel: 255 if pixel >= 128 else 0, mode="1")
        image.paste(tile, (x, y))
        boxes[name], sizes[name], strings[name] = box, size, value

    text("model", identity["model"], 12, 12, 22, latin_font, 138)
    text("variant", identity["variant"], 12, 42, 24, chinese_font, 138, stroke=1)
    text("pcb", "PCB " + identity["hardware_revision"], 12, 78, 20, latin_font, 138)
    text("firmware_caption", "固件", 12, 114, 20, chinese_font, 138, stroke=1)
    text("firmware", identity["firmware"], 12, 148, 22, narrow_font, 138)
    text("device_id", identity["device_id"], 12, 186, 28, narrow_font, 330)
    text("website_host", host, None, 209, 22, latin_font, 330)
    validate_layout(boxes)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    image.save(output / "label.png", dpi=(DPI, DPI))
    nearest = getattr(Image, "Resampling", Image).NEAREST
    image.resize((W * 4, H * 4), nearest).save(output / "preview.png")
    metadata = {"pixels": [W, H], "dpi": DPI, "stock_mm": [30, 20], "mode": "1",
                "visible_strings": strings, "boxes": boxes, "font_sizes_px": sizes,
                "font_paths": {"latin": latin_font, "narrow": narrow_font, "chinese": chinese_font},
                "chinese_font_index": 0, "chinese_stroke_pixels": 1,
                "core_metadata_only": identity["core"], "website": identity["website"],
                "miniapp_code": evidence, "label_sha256": digest((output / "label.png").read_bytes()),
                "physical_output": "unverified", "wechat_physical_scan": "unverified"}
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--latin-font", default=LATIN_FONT)
    parser.add_argument("--narrow-font", default=NARROW_FONT)
    parser.add_argument("--chinese-font", default=CHINESE_FONT)
    args = parser.parse_args(argv)
    try:
        result = render(args.job, args.output, args.latin_font, args.narrow_font, args.chinese_font)
        print(json.dumps({"ok": True, "metadata": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "printed": False}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
