#!/usr/bin/env python3
"""Create a side-by-side visual QC sheet for person identity; never scores identity."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


PANEL = (520, 360)
FACE = (300, 300)
THUMB = (168, 94)
MARGIN = 24
LABEL_H = 38


def parse_box(value: str) -> tuple[int, int, int, int]:
    parts = [int(part.strip()) for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("box must be x1,y1,x2,y2")
    x1, y1, x2, y2 = parts
    if x2 <= x1 or y2 <= y1:
        raise argparse.ArgumentTypeError("box must have positive width and height")
    return x1, y1, x2, y2


def font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def open_rgb(path: Path) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    copy = image.copy()
    copy.thumbnail(size, Image.Resampling.LANCZOS)
    panel = Image.new("RGB", size, "#111827")
    panel.paste(copy, ((size[0] - copy.width) // 2, (size[1] - copy.height) // 2))
    return panel


def crop_face(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    x1, y1, x2, y2 = box
    if x1 < 0 or y1 < 0 or x2 > image.width or y2 > image.height:
        raise ValueError(f"face box {box} is outside image size {image.size}")
    return ImageOps.fit(image.crop(box), FACE, method=Image.Resampling.LANCZOS)


def labeled(canvas: Image.Image, image: Image.Image, xy: tuple[int, int], label: str) -> None:
    x, y = xy
    draw = ImageDraw.Draw(canvas)
    draw.text((x, y), label, font=font(24), fill="white")
    canvas.paste(image, (x, y + LABEL_H))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference-face-box", type=parse_box, required=True)
    parser.add_argument("--candidate-face-box", type=parse_box, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    reference = open_rgb(args.reference)
    candidate = open_rgb(args.candidate)
    reference_face = crop_face(reference, args.reference_face_box)
    candidate_face = crop_face(candidate, args.candidate_face_box)

    width = MARGIN * 3 + PANEL[0] * 2
    height = MARGIN * 4 + LABEL_H * 2 + PANEL[1] + FACE[1] + 150
    canvas = Image.new("RGB", (width, height), "#0b1020")
    labeled(canvas, fit(reference, PANEL), (MARGIN, MARGIN), "REFERENCE — full")
    labeled(canvas, fit(candidate, PANEL), (MARGIN * 2 + PANEL[0], MARGIN), "CANDIDATE — full")

    y_face = MARGIN * 2 + LABEL_H + PANEL[1]
    labeled(canvas, reference_face, (MARGIN + 110, y_face), "REFERENCE — face")
    labeled(canvas, candidate_face, (MARGIN * 2 + PANEL[0] + 110, y_face), "CANDIDATE — face")

    y_thumb = y_face + LABEL_H + FACE[1] + MARGIN
    draw = ImageDraw.Draw(canvas)
    draw.text((MARGIN, y_thumb), "168x94 thumbnail check", font=font(24), fill="white")
    canvas.paste(fit(candidate, THUMB), (MARGIN, y_thumb + LABEL_H))
    warning = "VISUAL REVIEW REQUIRED — this sheet does not calculate or prove identity."
    draw.text((MARGIN + THUMB[0] + 28, y_thumb + LABEL_H + 28), warning, font=font(20), fill="#fbbf24")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.output)
    print(f"output={args.output}")
    print(f"size={canvas.width}x{canvas.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
