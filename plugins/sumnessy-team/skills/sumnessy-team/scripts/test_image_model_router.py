"""Offline selection and actual SDK request tests; no paid API requests."""
import base64
import copy
from email import policy as email_policy
from email.parser import BytesParser
import io
import json
from pathlib import Path
import unittest

import httpx
from openai import OpenAI
from PIL import Image

from image_model_router import FLAGS, FLARE, SUNBURST, api_key, choose_model, prepare, run
import test_validate_delivery
from validate_delivery import digest, read_json, validate


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.fx = test_validate_delivery.DeliveryTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root = self.fx.root
        self.batch = self.fx.batch
        self.batch["backend"] = "openai_api"
        for variant in self.batch["variants"]:
            name = variant["id"]
            self.fx.mutate_prompt(name, lambda p: p.update(canvas={"width": 1280, "height": 720}))
            variant["routing_job"] = name + "-job.json"
        self.job = {
            "schema_version": 1, "batch": "batch.json", "variant_id": "A", "attempt": 1,
            "visual_evidence": "Synthetic fixture; not a real thumbnail review.",
            "constraints": {k: False for k in FLAGS}, "size": "1280x720", "quality": "high",
            "images": [{"path": "logo.png", "sha256": digest(self.fx.asset), "role": "logo", "description": "Fixture logo"}]
        }
        for name in "ABC":
            job = copy.deepcopy(self.job)
            job["variant_id"] = name
            self.fx.write(name + "-job.json", job)
        self.batch_path = self.fx.write("batch.json", self.batch)
        self.job_path = self.root / "A-job.json"
        self.policy_path = self.fx.write("policy.json", {"model_routing": {"backend": "openai_api", "api_use_authorized": True}})
        self.calls = []
        self.response_error = None
        data = io.BytesIO()
        Image.new("RGB", (1280, 720), "orange").save(data, format="PNG")
        self.output = base64.b64encode(data.getvalue()).decode()
        def handler(request):
            self.calls.append(request)
            if self.response_error:
                raise self.response_error
            return httpx.Response(200, headers={"x-request-id": "req_offline_test"},
                                  json={"created": 1, "data": [{"b64_json": self.output}]})
        self.client = OpenAI(api_key="test-not-a-real-key", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(self.client.close)

    def save_job(self):
        self.fx.write("A-job.json", self.job)

    def execute(self):
        self.save_job()
        return run(self.job_path, self.policy_path, client=self.client)

    def free_generation(self):
        self.fx.contract.update(assets=[], text_lock=[])
        self.fx.write("contract.json", self.fx.contract)
        for name in "ABC":
            self.fx.mutate_prompt(name, lambda p: p.update(asset_ids=[], text_lock=[], preserve_exactly=[]))
        self.job["images"] = []
        self.save_job()

    def test_every_precision_signal_and_revision_select_sunburst(self):
        for key in FLAGS:
            with self.subTest(key=key):
                signals = {k: k == key for k in FLAGS}
                self.assertEqual(choose_model(signals, phase="initial")["model"], SUNBURST)
        self.assertEqual(choose_model({k: False for k in FLAGS}, phase="revision")["model"], SUNBURST)

    def test_free_generation_selects_flare(self):
        self.free_generation()
        plan = prepare(self.job_path)[0]
        self.assertEqual(plan["requested_model"], FLARE)
        self.assertEqual(len(self.calls), 0)

    def test_override_requires_explicit_evidence(self):
        args = {"phase": "initial", "override": {"model": FLARE}}
        with self.assertRaises(ValueError):
            choose_model({k: True for k in FLAGS}, **args)
        args["override"].update(user_quote="Flareを使って", evidence="synthetic test request")
        self.assertEqual(choose_model({k: True for k in FLAGS}, **args)["model"], FLARE)

    def test_precision_failure_switches_from_flare(self):
        decision = choose_model({k: False for k in FLAGS}, phase="initial", previous={"model": FLARE, "failures": ["text"]})
        self.assertEqual(decision["model"], SUNBURST)

    def test_hand_trial_is_not_unconditional(self):
        for failures, isolated, expected in [(["hands"], True, FLARE), (["hands"], False, SUNBURST), (["hands", "identity"], True, SUNBURST)]:
            route = choose_model({k: True for k in FLAGS}, phase="revision", previous={"model": SUNBURST, "failures": failures}, local_hand_fix=isolated)
            self.assertEqual(route["model"], expected)

    def test_sdk_edit_uploads_real_image_and_model(self):
        result = self.execute()
        request = self.calls[0]
        self.assertEqual(request.url.path, "/v1/images/edits")
        message = BytesParser(policy=email_policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.iter_parts()}
        self.assertEqual(fields["model"].decode(), SUNBURST)
        self.assertEqual(fields["n"], b"1")
        self.assertIn(self.fx.asset.read_bytes(), fields.values())
        receipt = read_json(Path(result["receipt"]))
        self.assertEqual(receipt["api_request_id"], "req_offline_test")
        self.assertEqual(receipt["state"], "succeeded")
        self.assertIsNone(receipt["reported_model"])
        self.assertEqual(receipt["visual_quality"], "not_checked")

    def test_sdk_generate_binds_flare_model(self):
        self.free_generation()
        self.execute()
        self.assertEqual(self.calls[0].url.path, "/v1/images/generations")
        self.assertEqual(json.loads(self.calls[0].content)["model"], FLARE)

    def test_refuse_missing_asset_before_network(self):
        self.job["images"] = []
        with self.assertRaisesRegex(ValueError, "not attached"):
            self.execute()
        self.assertFalse(self.calls)

    def test_changed_asset_is_rejected(self):
        self.fx.asset.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.execute()
        self.assertFalse(self.calls)

    def test_reference_review_and_three_variant_gate(self):
        self.batch["variants"].pop()
        self.fx.write("batch.json", self.batch)
        with self.assertRaisesRegex(ValueError, "expected 3"):
            self.execute()
        self.assertFalse(self.calls)

    def test_repeat_request_is_blocked(self):
        self.execute()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_timeout_has_no_automatic_retry_or_sensitive_error_log(self):
        self.response_error = httpx.ReadTimeout("PRIVATE_MARKER")
        with self.assertRaises(RuntimeError):
            self.execute()
        receipt = (self.root / "api-runs/A/attempt-01/receipt.json").read_text()
        self.assertNotIn("PRIVATE_MARKER", receipt)
        self.assertEqual(json.loads(receipt)["state"], "failed_or_unknown")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(ValueError):
            self.execute()

    def test_invalid_dimensions_rejected_before_charge(self):
        for size in ("1281x720", "32x16", "4096x2160", "0x0"):
            self.job["size"] = size
            with self.subTest(size=size), self.assertRaises(ValueError):
                self.execute()
        self.assertFalse(self.calls)

    def test_attempt_limit(self):
        self.job["attempt"] = 3
        with self.assertRaisesRegex(ValueError, "At most two"):
            self.execute()
        self.assertFalse(self.calls)

    def test_revision_requires_approved_base_first(self):
        self.fx.revision()
        self.fx.write("batch.json", self.batch)
        with self.assertRaisesRegex(ValueError, "approved baseline"):
            self.execute()

    def test_real_qc_and_mask_allow_hand_fallback(self):
        first = self.execute()
        source = Path(first["image"])
        self.fx.write("previous-qc.json", {"image_sha256": digest(source), "checked_at": "2026-09-22", "note": "Synthetic hand failure", "failures": ["hands"]})
        mask = Image.new("RGBA", (1280, 720), (0, 0, 0, 255))
        mask.putpixel((100, 100), (0, 0, 0, 0))
        mask.save(self.root / "mask.png")
        self.job.update(attempt=2, previous_receipt=first["receipt"], previous_qc="previous-qc.json",
                        isolated_hand_fix=True, protected_region_note="Synthetic face protection record",
                        mask={"path": "mask.png", "sha256": digest(self.root / "mask.png")})
        self.job["images"] = [{"path": str(source), "role": "edit_target", "sha256": digest(source), "description": "Previous result"},
                              *self.job["images"],
                              {"path": "logo.png", "role": "identity", "sha256": digest(self.fx.asset), "description": "Synthetic identity fixture"}]
        second = self.execute()
        self.assertEqual(second["requested_model"], FLARE)
        self.assertEqual(len(self.calls), 2)
        self.assertIn(b'name="mask"', self.calls[1].content)

    def test_fake_qc_hash_rejected(self):
        first = self.execute()
        self.fx.write("previous-qc.json", {"image_sha256": "wrong", "checked_at": "2026-09-22", "note": "fixture", "failures": ["hands"]})
        self.job.update(attempt=2, previous_receipt=first["receipt"], previous_qc="previous-qc.json")
        with self.assertRaisesRegex(ValueError, "actual previous image"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_missing_api_authorization_blocks_request(self):
        self.fx.write("policy.json", {"model_routing": {"backend": "openai_api"}})
        with self.assertRaisesRegex(ValueError, "not been authorized"):
            self.execute()
        self.assertFalse(self.calls)

    def test_delivery_checks_actual_model_receipt(self):
        first = self.execute()
        self.batch["variants"][0].update(original=first["image"], model_receipt=first["receipt"])
        self.fx.write("batch.json", self.batch)
        errors = validate(self.batch_path, "delivery")
        self.assertFalse(any(x.startswith("A:") and ("receipt" in x or "API" in x or "prompt changed" in x) for x in errors))
        self.job["quality"] = "medium"
        self.save_job()
        self.assertTrue(any("routing job changed" in x for x in validate(self.batch_path, "delivery")))

    def test_env_parser_does_not_interpolate(self):
        path = self.root / "confirmed.env"
        path.write_text('OPENAI_API_KEY="literal-${DO_NOT_EXPAND}"\n')
        self.assertEqual(api_key(path), "literal-${DO_NOT_EXPAND}")

    def test_batch_cannot_skip_api_receipt_validation(self):
        self.batch.pop("backend")
        self.fx.write("batch.json", self.batch)
        with self.assertRaisesRegex(ValueError, "Batch backend"):
            self.execute()
        self.assertFalse(self.calls)

    def test_mask_without_protected_pixels_is_rejected(self):
        source = self.root / "source.png"
        Image.new("RGBA", (1280, 720), (0, 0, 0, 255)).save(source)
        mask = self.root / "mask.png"
        Image.new("RGBA", (1280, 720), (0, 0, 0, 0)).save(mask)
        self.job["images"].insert(0, {"path": "source.png", "sha256": digest(source), "role": "edit_target", "description": "Fixture target"})
        self.job["mask"] = {"path": "mask.png", "sha256": digest(mask)}
        with self.assertRaisesRegex(ValueError, "opaque protected"):
            self.execute()
        self.assertFalse(self.calls)

    def test_invalid_response_is_preserved_without_regeneration(self):
        data = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(data, format="PNG")
        self.output = base64.b64encode(data.getvalue()).decode()
        with self.assertRaises(RuntimeError):
            self.execute()
        receipt = read_json(self.root / "api-runs/A/attempt-01/receipt.json")
        self.assertEqual(receipt["state"], "response_invalid")
        self.assertTrue((self.root / "api-runs/A/attempt-01/original.png").is_file())
        self.assertEqual(len(self.calls), 1)

    def test_contract_person_role_cannot_be_disabled_in_job(self):
        self.free_generation()
        self.fx.contract["assets"] = [{"id": "person", "path": "logo.png", "role": "person", "sha256": digest(self.fx.asset)}]
        self.fx.write("contract.json", self.fx.contract)
        for name in "ABC":
            self.fx.mutate_prompt(name, lambda p: p.update(asset_ids=["person"]))
        self.job["images"] = [{"path": "logo.png", "role": "other", "sha256": digest(self.fx.asset), "description": "Synthetic person reference"}]
        self.save_job()
        self.assertEqual(prepare(self.job_path)[0]["requested_model"], SUNBURST)


if __name__ == "__main__":
    unittest.main()
