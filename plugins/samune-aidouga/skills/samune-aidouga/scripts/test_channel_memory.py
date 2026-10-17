#!/usr/bin/env python3
"""Behavioral tests using synthetic images; no network, real channel data or image generation."""
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import channel_memory as memory
import collect_channel
from prepare_analysis import VISUAL_FIELDS

CHANNEL_A = "UC" + "a" * 22
CHANNEL_B = "UC" + "b" * 22
VIDEO = "AbcDEF012_3"


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "memory").resolve()

    def fixture(self, channel_id=CHANNEL_A, name="テストの作風"):
        folder = memory.channel_dir(self.root, channel_id)
        image = folder / "raw" / VIDEO / "test.jpg"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (320, 180), "#334455").save(image)
        row = {"video_id": VIDEO, "channel_id": channel_id,
               "thumbnail_path": str(image.relative_to(folder)),
               "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
               "last_seen_run": "fixture", "thumbnail_status": "saved", "tabs": ["videos"]}
        (folder / "manifest.jsonl").write_text(json.dumps(row) + "\n")
        observations = folder / "analysis" / "fixture" / "observations.csv"
        observations.parent.mkdir(parents=True)
        with observations.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["video_id", "inspected"] + VISUAL_FIELDS)
            writer.writeheader()
            writer.writerow({"video_id": VIDEO, "inspected": "yes",
                             **{field: "synthetic fixture" for field in VISUAL_FIELDS}})
        profile = {"channel_id": channel_id, "channel_name": name, "status": "learned",
                   "collection_run_id": "fixture", "evidence": {
                       "observation_file": "analysis/fixture/observations.csv", "inspected_count": 1,
                       "reference_video_ids": [VIDEO], "enumerated_count": 1, "saved_count": 1,
                       "limitations": ["Synthetic test data; no actual visual learning"]},
                   "observed_patterns": [{"rule": "synthetic test"}],
                   "production_defaults": {"palette": ["#334455"]}}
        memory.write_json(folder / "channel-profile.json", profile)
        memory.write_json(folder / "runs" / "fixture.json", {
            "channel_id": channel_id, "enumerated_unique_this_run": 1,
            "saved_for_this_run": 1, "listed_scope_complete": False, "limit_per_tab": 1})
        (folder / "analysis.md").write_text("Synthetic fixture. No real channel analyzed.\n")
        registry = memory.registry_at(self.root)
        registry["channels"][channel_id] = {"channel_name": name,
            "canonical_url": "https://www.youtube.com/channel/" + channel_id,
            "source_urls": ["https://www.youtube.com/@test-" + channel_id[-1]]}
        memory.write_json(self.root / "registry.json", registry)
        return folder

    def learn(self, *args, **kwargs):
        folder = self.fixture(*args, **kwargs)
        memory.finalize(self.root, folder.name)
        return folder

    def test_first_status_is_empty_without_creating_data(self):
        state = memory.status(self.root)
        self.assertIsNone(state["active"])
        self.assertFalse(self.root.exists())

    def test_default_root_is_independent_of_working_directory(self):
        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=Path(self.temp.name)):
            self.assertEqual(memory.data_root(), (Path(self.temp.name) / ".samune-aidouga").resolve())

    def test_explicit_root_has_precedence_over_environment(self):
        with patch.dict(os.environ, {"SAMUNE_AIDOUGA_DATA_ROOT": str(self.root)}):
            self.assertEqual(memory.data_root(), self.root)
            self.assertEqual(memory.data_root(self.temp.name), Path(self.temp.name).resolve())

    def test_finalize_then_fresh_process_and_reinstalled_script_reuses_memory(self):
        self.learn()
        installed = Path(self.temp.name) / "new-plugin-version" / "channel_memory.py"
        installed.parent.mkdir()
        shutil.copy2(Path(memory.__file__), installed)
        env = dict(os.environ, SAMUNE_AIDOUGA_DATA_ROOT=str(self.root))
        result = subprocess.run([sys.executable, str(installed), "status"], cwd=installed.parent,
                                env=env, text=True, capture_output=True, check=True)
        state = json.loads(result.stdout)
        self.assertEqual(state["active"]["channel_id"], CHANNEL_A)
        self.assertTrue(state["active"]["ready"])
        self.assertEqual(state["next_action"], "create")

    def test_existing_url_reuses_without_network_or_collection(self):
        self.learn()
        with patch.object(memory, "resolve_url", side_effect=AssertionError("network")), \
                patch.object(collect_channel, "collect", side_effect=AssertionError("collection")):
            self.assertEqual(memory.prepare(self.root, "https://youtube.com/@test-a/videos?x=1")["state"], "reused")

    def test_new_video_from_same_channel_only_resolves_identity(self):
        self.learn()
        with patch.object(memory, "resolve_url", return_value={"channel_id": CHANNEL_A}) as resolver, \
                patch.object(collect_channel, "collect", side_effect=AssertionError("collection")):
            self.assertEqual(memory.prepare(self.root, "https://youtu.be/" + VIDEO)["state"], "reused")
            resolver.assert_called_once()
        # The newly remembered video URL is also reusable without resolving again.
        with patch.object(memory, "resolve_url", side_effect=AssertionError("network")):
            self.assertEqual(memory.prepare(self.root, "https://youtu.be/" + VIDEO)["state"], "reused")

    def test_two_channels_keep_distinct_memory_and_active_selection(self):
        self.learn()
        self.learn(CHANNEL_B, "別の作風")
        self.assertEqual(memory.status(self.root)["active"]["channel_id"], CHANNEL_B)
        memory.activate(self.root, "テストの作風")
        self.assertEqual(memory.status(self.root)["active"]["channel_id"], CHANNEL_A)
        self.assertEqual(len(memory.status(self.root)["channels"]), 2)

    def test_failed_new_learning_keeps_previous_active(self):
        self.learn()
        with patch.object(collect_channel, "collect", side_effect=ValueError("network failure")):
            with self.assertRaises(ValueError):
                memory.prepare(self.root, "https://www.youtube.com/channel/" + CHANNEL_B)
        state = memory.status(self.root)
        self.assertEqual(state["active"]["channel_id"], CHANNEL_A)
        self.assertFalse(next(x for x in state["channels"] if x["channel_id"] == CHANNEL_B)["ready"])

    def test_collection_does_not_finalize_visual_learning(self):
        folder = self.fixture()
        state = memory.status(self.root)
        self.assertIsNone(state["active"])
        self.assertFalse(state["channels"][0]["ready"])
        with self.assertRaises(ValueError):
            memory.activate(self.root, folder.name)

    def test_changed_profile_requires_refinalizing_and_preserves_old_snapshot(self):
        folder = self.learn()
        previous = memory.status(self.root)["active"]["last_snapshot"]
        profile = memory.read_json(folder / "channel-profile.json")
        profile["production_defaults"]["palette"] = ["#ffffff"]
        memory.write_json(folder / "channel-profile.json", profile)
        self.assertFalse(memory.status(self.root)["active"]["ready"])
        memory.finalize(self.root, CHANNEL_A)
        old = memory.read_json(folder / "profile_history" / (previous + ".json"))
        self.assertEqual(old["production_defaults"]["palette"], ["#334455"])
        self.assertTrue(memory.status(self.root)["active"]["ready"])

    def test_mismatched_observation_count_does_not_become_ready(self):
        folder = self.fixture()
        profile = memory.read_json(folder / "channel-profile.json")
        profile["evidence"]["inspected_count"] = 30
        memory.write_json(folder / "channel-profile.json", profile)
        with self.assertRaises(ValueError):
            memory.finalize(self.root, CHANNEL_A)
        self.assertIsNone(memory.status(self.root)["active"])

    def test_missing_reference_image_cannot_be_finalized(self):
        folder = self.fixture()
        (folder / "raw" / VIDEO / "test.jpg").unlink()
        with self.assertRaises(ValueError):
            memory.finalize(self.root, CHANNEL_A)

    def test_deleted_evidence_after_learning_is_not_reported_ready(self):
        folder = self.learn()
        (folder / "raw" / VIDEO / "test.jpg").unlink()
        self.assertFalse(memory.status(self.root)["active"]["ready"])

    def test_short_and_live_links_choose_their_own_listing(self):
        self.assertEqual(memory.infer_tabs("https://youtube.com/shorts/" + VIDEO), ["shorts"])
        self.assertEqual(memory.infer_tabs("https://youtube.com/@test/shorts?view=0"), ["shorts"])
        self.assertEqual(memory.infer_tabs("https://youtube.com/live/" + VIDEO), ["streams"])

    def test_learning_another_video_kind_is_not_falsely_reported_reused(self):
        self.learn()
        with patch.object(collect_channel, "collect", side_effect=ValueError("needs shorts collection")):
            with self.assertRaisesRegex(ValueError, "needs shorts"):
                memory.prepare(self.root, "https://youtube.com/@test-a/shorts")

    def test_switching_back_to_learned_video_kind_restores_without_collection(self):
        folder = self.learn()
        profile = memory.read_json(folder / "channel-profile.json")
        profile["production_defaults"]["palette"] = ["#ffffff"]
        memory.write_json(folder / "channel-profile.json", profile)
        rows = collect_channel.read_records(folder / "manifest.jsonl")
        rows[VIDEO]["tabs"] = ["shorts"]
        collect_channel.write_records(folder / "manifest.jsonl", rows)
        memory.finalize(self.root, CHANNEL_A)
        self.assertEqual(memory.status(self.root)["active"]["learned_tabs"], ["shorts"])
        with patch.object(memory, "resolve_url", side_effect=AssertionError("network")), \
                patch.object(collect_channel, "collect", side_effect=AssertionError("collection")):
            result = memory.prepare(self.root, "https://youtube.com/@test-a/videos")
        self.assertEqual(result["state"], "reused")
        self.assertEqual(result["learned_tabs"], ["videos"])
        self.assertEqual(memory.read_json(folder / "channel-profile.json")["production_defaults"]["palette"], ["#334455"])

    def test_missing_limit_disclosure_cannot_be_finalized(self):
        folder = self.fixture()
        profile = memory.read_json(folder / "channel-profile.json")
        profile["evidence"]["limitations"] = []
        memory.write_json(folder / "channel-profile.json", profile)
        with self.assertRaises(ValueError):
            memory.finalize(self.root, CHANNEL_A)

    def test_registry_lock_does_not_overwrite_another_process(self):
        with memory.locked(self.root):
            with self.assertRaises(ValueError):
                with memory.locked(self.root):
                    self.fail("acquired an existing lock")
        self.assertFalse((self.root / ".registry.lock").exists())

    def test_corrupt_registry_is_not_reset(self):
        self.root.mkdir()
        path = self.root / "registry.json"
        path.write_text("broken")
        with self.assertRaises(ValueError):
            memory.status(self.root)
        self.assertEqual(path.read_text(), "broken")

    def test_url_formats_and_case_sensitive_video_ids(self):
        for source in ["https://youtu.be/" + VIDEO + "?si=share",
                       "https://www.youtube.com/watch?v=" + VIDEO + "&list=ignored",
                       "https://m.youtube.com/shorts/" + VIDEO,
                       "https://youtube.com/live/" + VIDEO]:
            self.assertEqual(memory.normalize_url(source), "https://www.youtube.com/watch?v=" + VIDEO)
        self.assertEqual(memory.normalize_url("https://youtube.com/@Example/videos?view=0"),
                         "https://www.youtube.com/@Example")
        self.assertNotEqual(memory.url_key("https://youtu.be/" + VIDEO),
                            memory.url_key("https://youtu.be/" + VIDEO.lower()))
        self.assertEqual(memory.url_key("https://youtube.com/@EXAMPLE"),
                         memory.url_key("https://youtube.com/@example"))

    def test_non_youtube_and_playlist_only_urls_are_rejected(self):
        for url in ["https://example.com/@abc", "file:///etc/passwd", "https://youtube.com.evil.test/@abc",
                    "https://youtube.com/playlist?list=PLabc", "https://user:pass@youtube.com/@abc"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                memory.normalize_url(url)

    def test_exact_shared_video_is_included_outside_recent_listing(self):
        def enumerate_one(channel_id, tab, limit, interval, receive):
            receive({"id": "12345678901", "title": "latest"}, tab, 1)
            return {"status": "limited", "entries_seen": 1, "channel_name": "Synthetic"}

        def save_image(folder, row, fetcher):
            image = folder / "raw" / row["video_id"] / "fixture.jpg"
            image.parent.mkdir(parents=True)
            Image.new("RGB", (320, 180), "#556677").save(image)
            return {"thumbnail_status": "saved", "thumbnail_path": str(image.relative_to(folder)),
                    "sha256": hashlib.sha256(image.read_bytes()).hexdigest()}

        with patch.object(memory, "resolve_url", return_value={"channel_id": CHANNEL_A,
                "reference_entry": {"id": VIDEO, "title": "explicit older video"}}), \
                patch.object(collect_channel, "enumerate_tab", side_effect=enumerate_one), \
                patch.object(collect_channel, "download", side_effect=save_image), \
                contextlib.redirect_stdout(io.StringIO()):
            result = memory.prepare(self.root, "https://youtu.be/" + VIDEO)
        self.assertEqual(result["state"], "needs_visual_analysis")
        manifest = collect_channel.read_records(self.root / "channels" / CHANNEL_A / "manifest.jsonl")
        self.assertEqual(set(manifest), {VIDEO, "12345678901"})
        self.assertTrue(manifest[VIDEO]["explicit_reference"])
        review = memory.read_json(Path(result["analysis_dir"]) / "review_index.json")
        self.assertIn(VIDEO, review["images"])
        self.assertIsNone(memory.status(self.root)["active"])


if __name__ == "__main__":
    unittest.main()
