#!/usr/bin/env python3
"""Resize/crop a thumbnail to 3840x2160 and compress under a size limit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps


def cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")
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


def save_under_limit(image: Image.Image, output: Path, max_bytes: int, start_quality: int, min_quality: int) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    last_size = 0
    for quality in range(start_quality, min_quality - 1, -5):
        image.save(output, "JPEG", quality=quality, optimize=True, progressive=True, subsampling=1)
        last_size = output.stat().st_size
        if last_size <= max_bytes:
            return quality
    image.save(output, "JPEG", quality=min_quality, optimize=True, progressive=True, subsampling=2)
    return min_quality


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--width", type=int, default=3840)
    parser.add_argument("--height", type=int, default=2160)
    parser.add_argument("--max-mb", type=float, default=2.0)
    parser.add_argument("--quality", type=int, default=92)
    parser.add_argument("--min-quality", type=int, default=67)
    args = parser.parse_args()

    max_bytes = int(args.max_mb * 1024 * 1024)
    image = Image.open(args.input)
    image = cover_resize(image, (args.width, args.height))
    final_quality = save_under_limit(image, args.output, max_bytes, args.quality, args.min_quality)
    size = args.output.stat().st_size

    print(f"output={args.output}")
    print(f"size_bytes={size}")
    print(f"quality={final_quality}")
    if size > max_bytes:
        print(f"warning=still_over_limit max_bytes={max_bytes}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
