#!/usr/bin/env python3
"""Render a finished YouTube thumbnail from a JSON composition spec."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps


CANVAS = (1280, 720)
PREVIEW = (168, 94)


FONT_CANDIDATES = {
    "heavy": [
        "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W8.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "bold": [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
    "regular": [
        "/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ],
}


def parse_color(value: Any, default_alpha: int = 255) -> tuple[int, int, int, int]:
    if isinstance(value, (list, tuple)):
        if len(value) == 3:
            return int(value[0]), int(value[1]), int(value[2]), default_alpha
        if len(value) == 4:
            return int(value[0]), int(value[1]), int(value[2]), int(value[3])
    if not isinstance(value, str):
        raise ValueError(f"Unsupported color: {value!r}")
    value = value.strip()
    if value.lower() == "transparent":
        return 0, 0, 0, 0
    if not value.startswith("#"):
        raise ValueError(f"Color must be hex: {value}")
    raw = value[1:]
    if len(raw) in (3, 4):
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 6:
        raw += f"{default_alpha:02x}"
    if len(raw) != 8:
        raise ValueError(f"Color must be #rgb, #rgba, #rrggbb, or #rrggbbaa: {value}")
    return tuple(int(raw[i : i + 2], 16) for i in range(0, 8, 2))  # type: ignore[return-value]


def resolve_path(base_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGBA")
    target_w, target_h = size
    src_w, src_h = image.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        image = image.crop((left, 0, left + new_w, src_h))
    elif src_ratio < target_ratio:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        image = image.crop((0, top, src_w, top + new_h))
    return image.resize(size, Image.Resampling.LANCZOS)


def contain_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGBA")
    image.thumbnail(size, Image.Resampling.LANCZOS)
    return image


def make_background(spec: dict[str, Any], base_dir: Path) -> Image.Image:
    background = spec.get("background", {})
    if isinstance(background, str):
        background = {"path": background}
    path = resolve_path(base_dir, background.get("path"))
    if path:
        return cover_resize(Image.open(path), CANVAS)

    gradient = background.get("gradient")
    if gradient:
        start = parse_color(gradient[0])
        end = parse_color(gradient[1])
        image = Image.new("RGBA", CANVAS)
        draw = ImageDraw.Draw(image)
        for y in range(CANVAS[1]):
            ratio = y / max(1, CANVAS[1] - 1)
            color = tuple(int(start[i] * (1 - ratio) + end[i] * ratio) for i in range(4))
            draw.line((0, y, CANVAS[0], y), fill=color)
        return image

    fill = parse_color(background.get("fill", "#111827"))
    return Image.new("RGBA", CANVAS, fill)


def load_font(layer: dict[str, Any], size: int) -> ImageFont.ImageFont:
    import os
    font_path = layer.get("font_path") or os.environ.get("SUMNESSY_FONT")
    if font_path and Path(font_path).expanduser().exists():
        return ImageFont.truetype(str(Path(font_path).expanduser()), size=size)

    family = layer.get("font", "heavy")
    candidates = FONT_CANDIDATES.get(family, FONT_CANDIDATES["heavy"])
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    raise ValueError("Japanese-capable font required: set layer.font_path or SUMNESSY_FONT to a locally licensed font file")


def text_lines(value: str) -> list[str]:
    return [line for line in str(value).replace("\\n", "\n").splitlines() if line.strip()]


def measure_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.ImageFont,
    stroke_width: int,
    line_spacing: int,
) -> tuple[int, int, list[tuple[int, int, tuple[int, int, int, int]]]]:
    measured: list[tuple[int, int, tuple[int, int, int, int]]] = []
    total_h = 0
    max_w = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        measured.append((width, height, bbox))
        max_w = max(max_w, width)
        total_h += height
    total_h += max(0, len(lines) - 1) * line_spacing
    return max_w, total_h, measured


def fit_font(
    draw: ImageDraw.ImageDraw,
    layer: dict[str, Any],
    lines: list[str],
    box: tuple[int, int, int, int],
) -> tuple[ImageFont.ImageFont, list[tuple[int, int, tuple[int, int, int, int]]], int, int]:
    x1, y1, x2, y2 = box
    max_size = int(layer.get("font_size", layer.get("max_font_size", 110)))
    min_size = int(layer.get("min_font_size", 32))
    stroke_width = int(layer.get("stroke_width", 0))
    line_spacing = int(layer.get("line_spacing", 0))
    for size in range(max_size, min_size - 1, -2):
        font = load_font(layer, size)
        width, height, measured = measure_lines(draw, lines, font, stroke_width, line_spacing)
        if width <= x2 - x1 and height <= y2 - y1:
            return font, measured, width, height
    font = load_font(layer, min_size)
    width, height, measured = measure_lines(draw, lines, font, stroke_width, line_spacing)
    return font, measured, width, height


def normalize_padding(value: Any) -> tuple[int, int, int, int]:
    if value is None:
        return 18, 10, 18, 10
    if isinstance(value, int):
        return value, value, value, value
    if len(value) == 2:
        return int(value[0]), int(value[1]), int(value[0]), int(value[1])
    return int(value[0]), int(value[1]), int(value[2]), int(value[3])


def draw_text_layer(canvas: Image.Image, layer: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas)
    lines = text_lines(layer.get("text", ""))
    if not lines:
        return
    box = tuple(int(v) for v in layer["box"])
    x1, y1, x2, y2 = box
    stroke_width = int(layer.get("stroke_width", 0))
    line_spacing = int(layer.get("line_spacing", 0))
    font, measured, block_w, block_h = fit_font(draw, layer, lines, box)
    align = layer.get("align", "left")
    valign = layer.get("valign", "center")
    if valign == "top":
        cursor_y = y1
    elif valign == "bottom":
        cursor_y = y2 - block_h
    else:
        cursor_y = y1 + ((y2 - y1) - block_h) // 2

    fill = parse_color(layer.get("fill", "#ffffff"))
    stroke_fill = parse_color(layer.get("stroke_fill", "#000000"))
    shadow = layer.get("shadow")
    backplates = layer.get("backplates", [])
    if isinstance(backplates, dict):
        backplates = [backplates]

    positions: list[tuple[str, int, int, int, int, tuple[int, int, int, int]]] = []
    for line, (line_w, line_h, bbox) in zip(lines, measured):
        if align == "center":
            text_x = x1 + ((x2 - x1) - line_w) // 2
        elif align == "right":
            text_x = x2 - line_w
        else:
            text_x = x1
        positions.append((line, text_x, cursor_y, line_w, line_h, bbox))
        cursor_y += line_h + line_spacing

    for plate in backplates:
        pad_l, pad_t, pad_r, pad_b = normalize_padding(plate.get("padding"))
        offset_x, offset_y = [int(v) for v in plate.get("offset", [0, 0])]
        radius = int(plate.get("radius", 14))
        plate_fill = parse_color(plate.get("fill", "#000000cc"))
        outline = plate.get("outline")
        outline_fill = parse_color(outline) if outline else None
        width = int(plate.get("width", 0))
        per_line = bool(plate.get("per_line", True))
        if per_line:
            for _line, text_x, text_y, line_w, _line_h, bbox in positions:
                rect = (
                    text_x + bbox[0] - pad_l + offset_x,
                    text_y + bbox[1] - pad_t + offset_y,
                    text_x + bbox[0] + line_w + pad_r + offset_x,
                    text_y + bbox[3] + pad_b + offset_y,
                )
                draw.rounded_rectangle(rect, radius=radius, fill=plate_fill, outline=outline_fill, width=width)
        else:
            rect = (x1 - pad_l + offset_x, y1 - pad_t + offset_y, x2 + pad_r + offset_x, y2 + pad_b + offset_y)
            draw.rounded_rectangle(rect, radius=radius, fill=plate_fill, outline=outline_fill, width=width)

    if shadow:
        shadow_fill = parse_color(shadow.get("fill", "#00000099"))
        sx, sy = [int(v) for v in shadow.get("offset", [6, 8])]
        for line, text_x, text_y, _line_w, _line_h, _bbox in positions:
            draw.text((text_x + sx, text_y + sy), line, font=font, fill=shadow_fill, stroke_width=stroke_width, stroke_fill=shadow_fill)

    for line, text_x, text_y, _line_w, _line_h, _bbox in positions:
        draw.text((text_x, text_y), line, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def paste_image(canvas: Image.Image, layer: dict[str, Any], base_dir: Path) -> None:
    path = resolve_path(base_dir, layer.get("path"))
    if not path:
        return
    role = layer.get("role")
    identity_mode = layer.get("identity_mode")
    if role == "person":
        if identity_mode != "original_pixels":
            raise ValueError('Person layers require identity_mode="original_pixels"')
        if float(layer.get("opacity", 1.0)) != 1.0:
            raise ValueError("Identity-protected person layers must use opacity 1.0")
        if layer.get("fit", "contain") == "cover" and not layer.get("allow_identity_crop", False):
            raise ValueError("Identity-protected person layers default to fit=contain; set allow_identity_crop=true explicitly")
    image = Image.open(path).convert("RGBA")
    x1, y1, x2, y2 = [int(v) for v in layer["box"]]
    box_w = x2 - x1
    box_h = y2 - y1
    frame = layer.get("frame")
    if frame:
        draw = ImageDraw.Draw(canvas)
        radius = int(frame.get("radius", 28))
        fill = parse_color(frame.get("fill", "#ffffff"))
        outline = parse_color(frame.get("outline", "#111111"))
        width = int(frame.get("width", 6))
        draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill, outline=outline, width=width)
        pad = int(frame.get("padding", 16))
        x1 += pad
        y1 += pad
        x2 -= pad
        y2 -= pad
        box_w = x2 - x1
        box_h = y2 - y1

    fit = layer.get("fit", "contain")
    fitted = cover_resize(image, (box_w, box_h)) if fit == "cover" else contain_resize(image, (box_w, box_h))
    anchor = layer.get("anchor", "center")
    px = x1 + (box_w - fitted.width) // 2
    py = y1 + (box_h - fitted.height) // 2
    if "left" in anchor:
        px = x1
    if "right" in anchor:
        px = x2 - fitted.width
    if "top" in anchor:
        py = y1
    if "bottom" in anchor:
        py = y2 - fitted.height

    opacity = float(layer.get("opacity", 1.0))
    if opacity < 1:
        alpha = fitted.getchannel("A").point(lambda p: int(p * opacity))
        fitted.putalpha(alpha)

    shadow = layer.get("shadow")
    if shadow:
        shadow_fill = parse_color(shadow.get("fill", "#00000088"))
        sx, sy = [int(v) for v in shadow.get("offset", [10, 12])]
        blur = int(shadow.get("blur", 10))
        alpha = fitted.getchannel("A")
        shadow_layer = Image.new("RGBA", fitted.size, shadow_fill)
        shadow_layer.putalpha(alpha.filter(ImageFilter.GaussianBlur(blur)))
        canvas.alpha_composite(shadow_layer, (px + sx, py + sy))

    stroke_width = int(layer.get("stroke_width", 0))
    if stroke_width > 0:
        stroke_fill = parse_color(layer.get("stroke_fill", "#ffffff"))
        alpha = fitted.getchannel("A")
        expanded = alpha
        for _ in range(stroke_width):
            expanded = expanded.filter(ImageFilter.MaxFilter(3))
        outline_alpha = ImageChops.subtract(expanded, alpha)
        outline = Image.new("RGBA", fitted.size, stroke_fill)
        outline.putalpha(outline_alpha)
        canvas.alpha_composite(outline, (px, py))

    canvas.alpha_composite(fitted, (px, py))


def draw_shape(canvas: Image.Image, layer: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas)
    shape = layer.get("shape", "rounded_rect")
    fill = parse_color(layer.get("fill", "transparent"))
    outline_value = layer.get("outline")
    outline = parse_color(outline_value) if outline_value else None
    width = int(layer.get("width", 1))
    if shape == "line":
        coords = tuple(int(v) for v in layer["coords"])
        draw.line(coords, fill=fill, width=width)
        return
    box = tuple(int(v) for v in layer["box"])
    if shape == "ellipse":
        draw.ellipse(box, fill=fill, outline=outline, width=width)
    elif shape == "rect":
        draw.rectangle(box, fill=fill, outline=outline, width=width)
    else:
        radius = int(layer.get("radius", 18))
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def draw_arrow(canvas: Image.Image, layer: dict[str, Any]) -> None:
    draw = ImageDraw.Draw(canvas)
    x1, y1, x2, y2 = [int(v) for v in layer["coords"]]
    color = parse_color(layer.get("fill", layer.get("color", "#ff0033")))
    width = int(layer.get("width", 12))
    draw.line((x1, y1, x2, y2), fill=color, width=width)
    angle = math.atan2(y2 - y1, x2 - x1)
    head_len = int(layer.get("head", 42))
    head_angle = math.pi / 7
    points = [
        (x2, y2),
        (x2 - head_len * math.cos(angle - head_angle), y2 - head_len * math.sin(angle - head_angle)),
        (x2 - head_len * math.cos(angle + head_angle), y2 - head_len * math.sin(angle + head_angle)),
    ]
    draw.polygon(points, fill=color)


def iter_layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    if "layers" in spec:
        return list(spec["layers"])
    layers: list[dict[str, Any]] = []
    for item in spec.get("shapes", []):
        layers.append({"type": "shape", **item})
    for item in spec.get("images", []):
        layers.append({"type": "image", **item})
    for item in spec.get("arrows", []):
        layers.append({"type": "arrow", **item})
    for item in spec.get("text_blocks", []):
        layers.append({"type": "text", **item})
    return layers


def render(spec: dict[str, Any], base_dir: Path) -> Image.Image:
    canvas = make_background(spec, base_dir)
    for layer in iter_layers(spec):
        layer_type = layer.get("type")
        if layer_type == "shape":
            draw_shape(canvas, layer)
        elif layer_type == "image":
            paste_image(canvas, layer, base_dir)
        elif layer_type == "arrow":
            draw_arrow(canvas, layer)
        elif layer_type == "text":
            draw_text_layer(canvas, layer)
        else:
            raise ValueError(f"Unknown layer type: {layer_type}")
    return canvas.convert("RGB")


def example_spec() -> dict[str, Any]:
    return {
        "background": {"gradient": ["#08111f", "#141414"]},
        "quality": 92,
        "layers": [
            {"type": "shape", "shape": "rounded_rect", "box": [42, 48, 690, 610], "radius": 18, "fill": "#00000066"},
            {
                "type": "text",
                "text": "主役コピー\\nここに",
                "box": [64, 82, 665, 345],
                "font": "heavy",
                "font_size": 132,
                "min_font_size": 70,
                "fill": "#ffffff",
                "stroke_fill": "#050505",
                "stroke_width": 9,
                "line_spacing": 6,
                "backplates": [
                    {"fill": "#00d5ff", "offset": [12, 14], "padding": [18, 6, 22, 8], "radius": 12},
                    {"fill": "#ffffff", "offset": [0, 0], "padding": [18, 6, 22, 8], "radius": 12},
                ],
                "shadow": {"fill": "#00000088", "offset": [8, 10]},
            },
            {
                "type": "text",
                "text": "伝えたいポイント",
                "box": [72, 440, 650, 560],
                "font": "heavy",
                "font_size": 72,
                "fill": "#ffe600",
                "stroke_fill": "#000000",
                "stroke_width": 7,
                "backplates": {"fill": "#111111dd", "padding": [20, 8, 22, 10], "radius": 12},
            },
            {"type": "arrow", "coords": [700, 420, 940, 320], "fill": "#ff0033", "width": 18, "head": 52},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, help="JSON composition spec.")
    parser.add_argument("--output", type=Path, help="Final 1280x720 JPEG/PNG path.")
    parser.add_argument("--preview", type=Path, help="Mobile-size preview path, usually 168x94.")
    parser.add_argument("--write-example", type=Path, help="Write an example JSON spec and exit.")
    args = parser.parse_args()

    if args.write_example:
        args.write_example.parent.mkdir(parents=True, exist_ok=True)
        args.write_example.write_text(json.dumps(example_spec(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"example={args.write_example}")
        return 0

    if not args.spec:
        parser.error("--spec is required unless --write-example is used")
    base_dir = args.spec.resolve().parent
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    output = args.output or resolve_path(base_dir, spec.get("output")) or Path("thumbnail.jpg")
    preview = args.preview or resolve_path(base_dir, spec.get("preview"))
    quality = int(spec.get("quality", 92))

    image = render(spec, base_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() in (".jpg", ".jpeg"):
        image.save(output, "JPEG", quality=quality, optimize=True, progressive=True, subsampling=1)
    else:
        image.save(output)
    print(f"output={output}")
    print(f"size={image.size[0]}x{image.size[1]}")
    print(f"bytes={output.stat().st_size}")

    if preview:
        preview.parent.mkdir(parents=True, exist_ok=True)
        image.resize(PREVIEW, Image.Resampling.LANCZOS).save(preview, "JPEG", quality=95)
        print(f"preview={preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
