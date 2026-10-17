#!/usr/bin/env python3
"""Check thumbnail batch records, not visual quality. Read-only; Python 3.10+."""
import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from PIL import Image

QC_IDS = {"text", "layout", "assets", "typography", "preservation",
          "hierarchy", "finish", "format"}
DESIGN_KEYS = ("canvas", "text_lock", "layout", "typography", "text_aspect_lock",
               "typography_style_lock", "person_identity", "asset_ids", "references",
               "background_selection")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def pointer(document, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("locked_values keys must be JSON Pointers")
    for token in path[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        document = document[int(token)] if isinstance(document, list) else document[token]
    return document


def differences(a, b, path=""):
    if type(a) is not type(b):
        return [path]
    if isinstance(a, dict):
        found = []
        for key in sorted(a.keys() | b.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            found.extend([child] if key not in a or key not in b else differences(a[key], b[key], child))
        return found
    if isinstance(a, list):
        if len(a) != len(b):
            return [path]
        return [p for i, (x, y) in enumerate(zip(a, b)) for p in differences(x, y, path + f"/{i}")]
    return [] if a == b else [path]


def validate(batch_path, stage):
    errors = []
    root = batch_path.resolve().parent

    def check(ok, message):
        if not ok:
            errors.append(message)

    def file_at(value, label, base=root):
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}: missing path")
            return None
        path = (base / value).resolve()
        if not path.is_file():
            errors.append(f"{label}: file not found: {path}")
            return None
        return path

    batch = read_json(batch_path)
    check(bool(batch.get("request_id")), "request_id is required")
    phase = batch.get("phase")
    check(phase in ("initial", "revision"), "phase must be initial or revision")
    count = 3 if phase == "initial" else 2
    override = batch.get("count_override")
    if override is not None:
        valid = (type(override.get("count")) is int and override["count"] > 0
                 and bool(override.get("user_quote")) and bool(override.get("evidence")))
        check(valid, "count_override requires positive count, actual user_quote and evidence")
        if valid:
            count = override["count"]
    variants = batch.get("variants", [])
    check(len(variants) == count, f"{phase}: expected {count} variants, got {len(variants)}")
    ids = [v.get("id") for v in variants]
    check(all(ids) and len(set(ids)) == len(ids), "variant IDs must be nonempty and unique")

    contract_path = file_at(batch.get("contract"), "contract")
    if contract_path is None:
        return errors
    contract = read_json(contract_path)
    check(isinstance(contract.get("text_lock"), list), "contract.text_lock must be a list")
    requirements = contract.get("requirements", [])
    req_ids = [r.get("id") for r in requirements]
    check(bool(req_ids) and all(req_ids) and len(set(req_ids)) == len(req_ids),
          "requirements must contain unique nonempty IDs")
    check(not (set(req_ids) & QC_IDS), "requirement IDs must not replace standard QC IDs")
    for req in requirements:
        check(bool(req.get("instruction")) and bool(req.get("basis")),
              f"{req.get('id')}: instruction and basis required")

    assets = contract.get("assets", [])
    asset_ids = [a.get("id") for a in assets]
    check(all(asset_ids) and len(set(asset_ids)) == len(asset_ids), "asset IDs must be unique")
    for asset in assets:
        path = file_at(asset.get("path"), f"asset {asset.get('id')}", contract_path.parent)
        check(bool(asset.get("role")), f"asset {asset.get('id')}: role required")
        if path:
            check(digest(path) == asset.get("sha256"), f"asset {asset.get('id')}: SHA-256 mismatch")

    review_dir = batch.get("review_dir")
    if not isinstance(review_dir, str) or not review_dir:
        check(False, "review_dir is required")
    else:
        folder = root / review_dir
        index_file = file_at(str(folder / "review-index.json"), "review index")
        notes_file = file_at(str(folder / "page-reviews.jsonl"), "review notes")
        if index_file and notes_file:
            pages = {p["page_id"] for p in read_json(index_file)["pages"]}
            notes = [json.loads(line) for line in notes_file.read_text().splitlines() if line.strip()]
            viewed = {n.get("page_id") for n in notes if n.get("viewed") is True and n.get("notes") and n.get("at")}
            check(bool(pages) and pages == viewed, "reference review has missing or unknown pages")

    baseline = None
    allowed = []
    if phase == "revision":
        revision = batch.get("revision") or {}
        base_image = file_at(revision.get("image"), "revision base image")
        base_prompt = file_at(revision.get("prompt"), "revision base prompt")
        for path, key in ((base_image, "image_sha256"), (base_prompt, "prompt_sha256")):
            if path:
                check(digest(path) == revision.get(key), f"revision {key}: mismatch")
        if base_prompt:
            baseline = read_json(base_prompt)
        allowed = revision.get("allowed_change_paths", [])
        check(isinstance(allowed, list) and all(isinstance(p, str) and p.startswith("/") and len(p) > 1 for p in allowed),
              "revision.allowed_change_paths must be JSON Pointers")

    pixel_hashes = set()
    for variant in variants:
        name = str(variant.get("id"))
        check(bool(variant.get("intent")), f"{name}: design intent required")
        path = file_at(variant.get("prompt"), f"{name} prompt")
        if path is None:
            continue
        prompt = read_json(path)
        context = prompt.get("batch_context", {})
        check(context == {"request_id": batch.get("request_id"), "phase": phase, "variant_id": variant.get("id")},
              f"{name}: batch_context mismatch")
        check(prompt.get("output", {}).get("count") == 1, f"{name}: one image per generation required")
        check(prompt.get("text_lock") == contract.get("text_lock"), f"{name}: exact text_lock mismatch")
        check(set(prompt.get("asset_ids", [])) == set(asset_ids), f"{name}: shared asset IDs mismatch")
        for key in ("text_aspect_lock", "typography_style_lock", "change_only", "preserve_exactly"):
            check(key in prompt, f"{name}: missing {key}")
        for key, value in contract.get("locked_values", {}).items():
            try:
                check(pointer(prompt, key) == value, f"{name}: locked value changed: {key}")
            except (KeyError, ValueError, IndexError, TypeError):
                check(False, f"{name}: locked path missing or invalid: {key}")
        if baseline is not None:
            before = {k: baseline.get(k) for k in DESIGN_KEYS}
            after = {k: prompt.get(k) for k in DESIGN_KEYS}
            for change in differences(before, after):
                check(any(change == p or change.startswith(p + "/") for p in allowed),
                      f"{name}: unrequested design change: {change}")
        if stage == "plan":
            continue
        original_path = file_at(variant.get("original"), f"{name} original")
        qc_file = file_at(variant.get("qc_file"), f"{name} QC record")
        if qc_file:
            check(bool(qc_file.read_text().strip()), f"{name}: empty QC record")
        try:
            datetime.fromisoformat(variant.get("checked_at") or "")
        except ValueError:
            check(False, f"{name}: valid checked_at required")
        checks = variant.get("qc_checks", {})
        for key in sorted(QC_IDS | set(req_ids)):
            item = checks.get(key, {})
            check(item.get("status") == "pass" and bool(item.get("note")), f"{name}: incomplete QC: {key}")
        image_path = file_at(variant.get("image"), f"{name} delivery image")
        if image_path:
            check(digest(image_path) == variant.get("image_sha256"), f"{name}: image changed after QC or hash missing")
            check(image_path.stat().st_size < batch.get("max_bytes", 2000000), f"{name}: image exceeds byte limit")
            with Image.open(image_path) as image:
                image.load()
                canvas = prompt.get("canvas", {})
                check(image.size == (canvas.get("width"), canvas.get("height")), f"{name}: image/canvas dimensions mismatch")
                pixels = hashlib.sha256(str(image.size).encode() + image.convert("RGBA").tobytes()).hexdigest()
                check(pixels not in pixel_hashes, f"{name}: duplicate image pixels")
                pixel_hashes.add(pixels)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--stage", choices=("plan", "delivery"), default="delivery")
    args = parser.parse_args()
    try:
        errors = validate(args.batch, args.stage)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors = [f"Invalid or unreadable input: {exc}"]
    print(json.dumps({"stage": args.stage, "passed": not errors, "errors": errors,
                      "limit": "Checks files and declared records only; visual inspection remains mandatory."}, ensure_ascii=False, indent=2))
    raise SystemExit(2 if errors else 0)


if __name__ == "__main__":
    main()
