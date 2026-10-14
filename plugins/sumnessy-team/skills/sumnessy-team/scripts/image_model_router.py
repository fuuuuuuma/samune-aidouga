#!/usr/bin/env python3
"""Deterministic model routing and a resumable OpenAI Image API adapter.

The assistant classifies the inspected assets; this program never claims to see
images or to prove visual quality. Plan is offline. Run makes one paid request.
"""
import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import time

from PIL import Image

from validate_delivery import digest, read_json, validate

FLARE = "gpt-image-2.5-flare"
SUNBURST = "gpt-image-2.5-sunburst"
MODELS = (FLARE, SUNBURST)
FLAGS = ("preserve_identity", "preserve_layout", "exact_text", "genuine_logo", "unchanged_elsewhere")
ROLES = {"rough", "identity", "logo", "edit_target", "style", "background", "other"}
FAILURES = {"identity", "hands", "text", "logo", "layout", "preservation", "finish"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def choose_model(signals, *, phase, previous=None, local_hand_fix=False, override=None):
    """Return an auditable decision, independent of any credentials or network."""
    if override:
        require(override.get("model") in MODELS and override.get("user_quote") and override.get("evidence"),
                "Model override requires a supported model and explicit user evidence")
        return {"model": override["model"], "rule": "explicit_user_choice", "reason": override["user_quote"]}
    if (previous and previous["model"] == SUNBURST and previous["failures"] == ["hands"]
            and local_hand_fix):
        return {"model": FLARE, "rule": "isolated_hand_trial", "reason": "Sunburstの手だけの不具合を、マスク付き局所編集でFlareに切り替えて比較する"}
    if previous and previous["model"] == FLARE and set(previous["failures"]) & (FAILURES - {"hands", "finish"}):
        return {"model": SUNBURST, "rule": "precision_repair", "reason": "Flareの保持条件違反をSunburstで限定修正する"}
    protected = [key for key in FLAGS if signals[key]]
    if phase == "revision" or protected:
        return {"model": SUNBURST, "rule": "precision_first", "reason": "保持・編集精度を優先: " + ", ".join(protected or ["revision"])}
    return {"model": FLARE, "rule": "free_generation", "reason": "厳密な保持対象がない新規構図・背景の生成"}


def checked_image(root, entry):
    require(isinstance(entry, dict) and entry.get("path") and entry.get("sha256"), "Image path and SHA-256 required")
    path = (root / entry["path"]).resolve()
    require(path.is_file() and digest(path) == entry["sha256"], "Input image missing or hash changed")
    require(path.stat().st_size < 50 * 1024 * 1024, "Input image must be below 50 MB")
    with Image.open(path) as image:
        image.load()
        require(image.format in ("PNG", "JPEG", "WEBP"), "Export PSD/HEIC to PNG/JPEG/WebP before attachment")
    return path


def prepare(job_path):
    """Validate a whole 3/2 batch, bind assets, and create a local route plan."""
    job_path = job_path.resolve()
    root = job_path.parent
    job = read_json(job_path)
    require(job.get("schema_version") == 1, "Unsupported routing job version")
    batch_path = (root / job["batch"]).resolve()
    errors = validate(batch_path, "plan")
    require(not errors, "Batch plan failed: " + "; ".join(errors))
    batch = read_json(batch_path)
    require(batch.get("backend") == "openai_api", "Batch backend must explicitly be openai_api")
    require(re.fullmatch(r"[A-Za-z0-9_-]+", str(job.get("variant_id", ""))), "Unsafe or missing variant ID")
    variants = [v for v in batch["variants"] if v["id"] == job["variant_id"]]
    require(len(variants) == 1, "Variant not in validated batch")
    variant = variants[0]
    require(variant.get("routing_job") and (batch_path.parent / variant["routing_job"]).resolve() == job_path,
            "Batch must explicitly bind this routing_job")
    prompt_path = (batch_path.parent / variant["prompt"]).resolve()
    prompt = read_json(prompt_path)
    contract_path = (batch_path.parent / batch["contract"]).resolve()
    contract = read_json(contract_path)
    images = job.get("images", [])
    require(isinstance(images, list), "images must be a list")
    paths = [checked_image(root, item) for item in images]
    require(all(i.get("role") in ROLES and i.get("description") for i in images), "Each image needs a known role and description")
    # Supplied materials must actually be uploaded, not merely named in a prompt.
    actual = {str(p): item["sha256"] for p, item in zip(paths, images)}
    for asset in contract.get("assets", []):
        expected = str((contract_path.parent / asset["path"]).resolve())
        require(actual.get(expected) == asset["sha256"], "A contract asset is not attached to the API request")
    constraints = job.get("constraints", {})
    require(all(type(constraints.get(k)) is bool for k in FLAGS), "Every constraint must be explicitly true/false after inspection")
    require(bool(job.get("visual_evidence")), "Record the actual rough/asset inspection before routing")
    roles = {i["role"] for i in images} | {a.get("role") for a in contract.get("assets", [])}
    signals = dict(constraints)
    signals["preserve_identity"] |= bool({"identity", "person"} & roles) or bool((prompt.get("person_identity") or {}).get("primary_reference"))
    signals["preserve_layout"] |= "rough" in roles or "edit_target" in roles
    signals["exact_text"] |= bool(prompt.get("text_lock"))
    signals["genuine_logo"] |= "logo" in roles
    signals["unchanged_elsewhere"] |= bool(prompt.get("preserve_exactly")) or batch["phase"] == "revision"
    attempt = job.get("attempt", 1)
    require(type(attempt) is int and 1 <= attempt <= 2, "At most two requests per variant; do not retry indefinitely")
    previous = None
    if attempt == 2:
        receipt_path = (root / job["previous_receipt"]).resolve()
        receipt = read_json(receipt_path)
        qc = read_json(root / job["previous_qc"])
        prev_image = receipt_path.parent / receipt["output"]
        require(receipt.get("state") == "succeeded" and receipt.get("attempt") == 1,
                "Retry requires a completed first request, not an ambiguous API failure")
        require(receipt.get("request_id") == batch["request_id"] and receipt.get("variant_id") == variant["id"],
                "Previous result belongs to another variant")
        require(prev_image.is_file() and digest(prev_image) == receipt.get("output_sha256") == qc.get("image_sha256"),
                "Previous QC must describe the actual previous image")
        failures = qc.get("failures", [])
        require(qc.get("checked_at") and qc.get("note") and failures and set(failures) <= FAILURES,
                "Record actual visual failures before a second request")
        require(paths and paths[0] == prev_image.resolve() and images[0]["role"] == "edit_target",
                "The second request must edit the inspected previous output")
        require(receipt.get("requested_model") in MODELS, "Unknown previous model")
        previous = {"model": receipt["requested_model"], "failures": sorted(set(failures))}
    else:
        require(not job.get("previous_receipt") and not job.get("previous_qc"), "First attempt cannot claim a previous result")
    if batch["phase"] == "revision" and attempt == 1:
        baseline = (batch_path.parent / batch["revision"]["image"]).resolve()
        require(paths and paths[0] == baseline and images[0]["role"] == "edit_target",
                "Revision must attach the approved baseline as first image")
    mask_path = None
    if job.get("mask"):
        require(paths and images[0]["role"] == "edit_target", "Mask requires edit_target first")
        mask_path = checked_image(root, job["mask"])
        with Image.open(paths[0]) as source, Image.open(mask_path) as mask:
            require(mask.format == source.format == "PNG" and mask.size == source.size and "A" in mask.getbands(),
                    "Mask and first image must be matching PNGs, with an alpha channel in the mask")
            require(mask.getchannel("A").getextrema() == (0, 255), "Mask needs both transparent edit and opaque protected pixels")
    isolated = job.get("isolated_hand_fix") is True
    if isolated:
        require(mask_path and job.get("protected_region_note") and "identity" in roles,
                "Hand fallback requires a mask, original identity reference, and protected-region inspection")
    route = choose_model(signals, phase=batch["phase"], previous=previous,
                         local_hand_fix=isolated, override=job.get("override_model"))
    quality = job.get("quality", "high")
    require(quality in ("low", "medium", "high", "xhigh", "max", "auto"), "Unsupported quality")
    if route["rule"] == "isolated_hand_trial":
        quality = "high"
    background = job.get("background")
    require(background is None or background in ("transparent", "opaque", "auto"),
            "Unsupported background; use transparent, opaque or auto")
    size = job.get("size", "1280x720")
    require(re.fullmatch(r"\d+x\d+", size), "Use explicit WIDTHxHEIGHT")
    w, h = map(int, size.split("x"))
    require(w > 0 and h > 0 and w % 16 == h % 16 == 0 and max(w, h) <= 3840
            and 1 / 3 <= w / h <= 3 and 655360 <= w * h <= 8294400, "Unsupported model dimensions")
    require((w, h) == (prompt["canvas"]["width"], prompt["canvas"]["height"]), "API size must equal the planned canvas")
    # Use one JSON prompt; local paths are not needed by the remote model.
    payload = {"design": prompt, "attachments": [{"index": n + 1, "role": item["role"], "description": item["description"]}
                                                for n, item in enumerate(images)]}
    plan = {"schema_version": 1, "request_id": batch["request_id"], "variant_id": variant["id"],
            "phase": batch["phase"], "attempt": attempt, "backend": "openai_api",
            "requested_model": route["model"], "rule": route["rule"], "reason": route["reason"],
            "signals": signals, "quality": quality, "size": size, "n": 1,
            "endpoint": "images.edit" if paths else "images.generate",
            "job_sha256": digest(job_path), "prompt_sha256": digest(prompt_path),
            "inputs": [{"role": item["role"], "sha256": item["sha256"]} for item in images],
            "mask_sha256": job.get("mask", {}).get("sha256") if job.get("mask") else None,
            "visual_quality": "not_checked"}
    if background is not None:
        plan["background"] = background
    output_dir = batch_path.parent / "api-runs" / variant["id"] / f"attempt-{attempt:02d}"
    return plan, payload, paths, mask_path, output_dir


def api_key(env_file=None):
    if env_file:
        from dotenv import dotenv_values
        require(env_file.is_file() and not env_file.is_symlink(), "Confirmed env file not found or is a symlink")
        key = dotenv_values(env_file, interpolate=False).get("OPENAI_API_KEY")
    else:
        key = os.environ.get("OPENAI_API_KEY")
    require(bool(key and key.strip()), "OPENAI_API_KEY is not configured; use secure key setup")
    return key.strip()


def client_for(key):
    from openai import OpenAI
    # Never redirect user material through OPENAI_BASE_URL or retry a billable request implicitly.
    return OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0, timeout=240.0)


def run(job_path, policy_path, env_file=None, *, client=None):
    policy = read_json(policy_path).get("model_routing", {})
    require(policy.get("backend") == "openai_api" and policy.get("api_use_authorized") is True,
            "OpenAI API backend has not been authorized in the production policy")
    plan, payload, paths, mask_path, folder = prepare(job_path)
    require(not folder.exists(), "This attempt already exists; inspect its receipt instead of repeating a possible charge")
    if client is None:
        client = client_for(api_key(env_file))
    folder.mkdir(parents=True, exist_ok=False)
    record = dict(plan, state="requesting", started_at=now())
    receipt_path = folder / "receipt.json"
    save_json(receipt_path, record)
    started = time.monotonic()
    try:
        with ExitStack() as stack:
            kwargs = {"model": plan["requested_model"], "prompt": json.dumps(payload, ensure_ascii=False),
                      "n": 1, "quality": plan["quality"], "size": plan["size"], "output_format": "png"}
            if "background" in plan:
                kwargs["background"] = plan["background"]
            if paths:
                kwargs["image"] = [stack.enter_context(p.open("rb")) for p in paths]
                if mask_path:
                    kwargs["mask"] = stack.enter_context(mask_path.open("rb"))
                result = client.images.edit(**kwargs)
            else:
                result = client.images.generate(**kwargs)
        record["api_request_id"] = getattr(result, "_request_id", None)
        record["reported_model"] = getattr(result, "model", None)
        usage = getattr(result, "usage", None)
        record["usage"] = usage.model_dump() if hasattr(usage, "model_dump") else None
        record["state"] = "response_received"
        save_json(receipt_path, record)
        require(len(result.data or []) == 1 and result.data[0].b64_json, "API did not return exactly one base64 image")
        data = base64.b64decode(result.data[0].b64_json, validate=True)
        original = folder / "original.png"
        original.write_bytes(data)
        record.update(output="original.png", output_sha256=digest(original))
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            require(im.format == "PNG" and im.size == tuple(map(int, plan["size"].split("x"))), "Unexpected output format or dimensions; preserve output for inspection")
        record["state"] = "succeeded"
    except Exception as exc:
        # Error bodies can echo submitted material. Store metadata only.
        record.update(state="response_invalid" if record["state"] == "response_received" else "failed_or_unknown",
                      error_type=type(exc).__name__, status_code=getattr(exc, "status_code", None))
        raise RuntimeError("Request did not complete normally. Inspect receipt; do not automatically repeat it.") from None
    finally:
        record.update(finished_at=now(), elapsed_seconds=round(time.monotonic() - started, 3))
        save_json(receipt_path, record)
    return {"receipt": str(receipt_path), "image": str(original), "requested_model": record["requested_model"],
            "visual_quality": "not_checked"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "check-connection"))
    parser.add_argument("--job", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            require(args.job is not None, "--job required")
            result = prepare(args.job)[0]
        elif args.command == "run":
            require(args.job is not None and args.policy is not None, "--job and --policy required")
            result = run(args.job, args.policy, args.env_file)
        else:
            client = client_for(api_key(args.env_file))
            result = {"models": {}, "generation_tested": False}
            for model in MODELS:
                try:
                    client.models.retrieve(model)
                    result["models"][model] = "model_visible; image endpoint not tested"
                except Exception as exc:
                    result["models"][model] = {"error_type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        # Locally authored validation messages are safe; unexpected I/O text is not emitted.
        print(json.dumps({"error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
