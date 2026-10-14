"""Regression tests for the user's 3/2 delivery and preservation rules."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from validate_delivery import QC_IDS, digest, validate


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.asset = self.root / "logo.png"
        Image.new("RGB", (16, 16), "orange").save(self.asset)
        self.contract = {
            "text_lock": [{"id": "result", "text": "編集作業10h→1h"}],
            "assets": [{"id": "logo", "path": "logo.png", "role": "logo", "sha256": digest(self.asset)}],
            "locked_values": {"/layout/elements/0/stroke_width": 47},
            "requirements": [{"id": "R01", "instruction": "×の太さを保つ", "basis": "test user request"}],
        }
        self.write("contract.json", self.contract)
        self.write("review/review-index.json", {"pages": [{"page_id": "p1"}]})
        (self.root / "review/page-reviews.jsonl").write_text(json.dumps({"page_id": "p1", "viewed": True, "at": "2026-09-22", "notes": "Synthetic test record, not actual visual review."}))
        self.batch = {"request_id": "test", "phase": "initial", "contract": "contract.json", "review_dir": "review", "variants": []}
        for i, name in enumerate("ABC"):
            prompt = {
                "batch_context": {"request_id": "test", "phase": "initial", "variant_id": name},
                "canvas": {"width": 32, "height": 18}, "text_lock": self.contract["text_lock"],
                "asset_ids": ["logo"], "output": {"count": 1}, "references": [],
                "layout": {"elements": [{"stroke_width": 47, "opacity": 0.8}]},
                "typography_style_lock": {"fills_and_gradients": ["yellow-gold"]},
                "text_aspect_lock": {"scale": "uniform"}, "change_only": [], "preserve_exactly": ["gradient"],
            }
            self.write(name + ".json", prompt)
            path = self.root / (name + ".png")
            Image.new("RGB", (32, 18), (20 + i * 40, 80, 120)).save(path)
            (self.root / (name + ".md")).write_text("Synthetic fixture: checks validator only.")
            self.batch["variants"].append({"id": name, "intent": "test variation", "prompt": name + ".json", "original": name + ".png", "image": name + ".png", "image_sha256": digest(path), "qc_file": name + ".md", "checked_at": "2026-09-22T10:00:00+09:00", "qc_checks": {key: {"status": "pass", "note": "synthetic fixture"} for key in QC_IDS | {"R01"}}})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def check_batch(self, stage="delivery"):
        return validate(self.write("batch.json", self.batch), stage)

    def mutate_prompt(self, name, callback):
        prompt = json.loads((self.root / (name + ".json")).read_text())
        callback(prompt)
        self.write(name + ".json", prompt)

    def revision(self):
        base = self.root / "A.json"
        self.write("baseline.json", json.loads(base.read_text()))
        self.batch["phase"] = "revision"
        self.batch["variants"] = self.batch["variants"][:2]
        self.batch["revision"] = {"image": "A.png", "image_sha256": digest(self.root / "A.png"), "prompt": "baseline.json", "prompt_sha256": digest(self.root / "baseline.json"), "allowed_change_paths": ["/layout/elements/0/opacity"]}
        for name in "AB":
            def edit(prompt):
                prompt["batch_context"]["phase"] = "revision"
                prompt["layout"]["elements"][0]["opacity"] = 0.65
            self.mutate_prompt(name, edit)

    def test_initial_three_and_revision_two_pass(self):
        self.assertEqual(self.check_batch(), [])
        self.revision()
        self.assertEqual(self.check_batch(), [])

    def test_initial_one_fails(self):
        self.batch["variants"] = self.batch["variants"][:1]
        self.assertTrue(any("expected 3" in x for x in self.check_batch()))

    def test_revision_one_fails(self):
        self.revision()
        self.batch["variants"].pop()
        self.assertTrue(any("expected 2" in x for x in self.check_batch()))

    def test_explicit_override_requires_evidence(self):
        self.batch["variants"] = self.batch["variants"][:1]
        self.batch["count_override"] = {"count": 1}
        self.assertTrue(self.check_batch())
        self.batch["count_override"].update(user_quote="今回は1案だけ", evidence="synthetic request")
        self.assertEqual(self.check_batch(), [])

    def test_duplicate_pixels_with_distinct_file_names_fail(self):
        Image.open(self.root / "A.png").save(self.root / "B.png")
        self.batch["variants"][1]["image_sha256"] = digest(self.root / "B.png")
        self.assertTrue(any("duplicate image pixels" in x for x in self.check_batch()))

    def test_unit_changed_fails(self):
        self.mutate_prompt("B", lambda p: p.update(text_lock=[{"id": "result", "text": "編集作業10時間→1時間"}]))
        self.assertTrue(any("text_lock mismatch" in x for x in self.check_batch("plan")))

    def test_asset_mutation_fails(self):
        Image.new("RGB", (16, 16), "blue").save(self.asset)
        self.assertTrue(any("asset logo: SHA-256 mismatch" in x for x in self.check_batch("plan")))

    def test_thickness_regression_fails(self):
        self.revision()
        self.mutate_prompt("A", lambda p: p["layout"]["elements"][0].update(stroke_width=20))
        errors = self.check_batch("plan")
        self.assertTrue(any("locked value changed" in x for x in errors))
        self.assertTrue(any("unrequested design change" in x for x in errors))

    def test_gradient_regression_fails(self):
        self.revision()
        self.mutate_prompt("B", lambda p: p["typography_style_lock"].update(fills_and_gradients=["yellow-flat"]))
        self.assertTrue(any("unrequested design change" in x for x in self.check_batch("plan")))

    def test_background_replacement_requires_explicit_change_scope(self):
        for name in "ABC":
            self.mutate_prompt(name, lambda p: p.update(background_selection={"asset_id": "background-original"}))
        self.revision()
        self.mutate_prompt("A", lambda p: p["background_selection"].update(asset_id="background-other"))
        self.assertTrue(any("unrequested design change: /background_selection/asset_id" in x for x in self.check_batch("plan")))
        self.batch["revision"]["allowed_change_paths"].append("/background_selection/asset_id")
        self.assertEqual(self.check_batch("plan"), [])

    def test_missing_requirement_qc_fails(self):
        del self.batch["variants"][2]["qc_checks"]["R01"]
        self.assertTrue(any("incomplete QC: R01" in x for x in self.check_batch()))

    def test_image_changed_after_qc_fails(self):
        Image.new("RGB", (32, 18), "red").save(self.root / "C.png")
        self.assertTrue(any("image changed after QC" in x for x in self.check_batch()))

    def test_missing_review_page_fails(self):
        self.write("review/review-index.json", {"pages": [{"page_id": "p1"}, {"page_id": "p2"}]})
        self.assertTrue(any("missing or unknown pages" in x for x in self.check_batch("plan")))

    def test_plan_does_not_require_generated_images(self):
        (self.root / "C.png").unlink()
        self.assertEqual(self.check_batch("plan"), [])
        self.assertTrue(self.check_batch("delivery"))


if __name__ == "__main__":
    unittest.main()
