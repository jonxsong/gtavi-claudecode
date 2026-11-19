import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from engine_rights import CAMPAIGNS, CHANNEL_ID, Blocked, authorize, cleared_viewer_messages
from engine_state import State, now, sha256
from engine_youtube import YouTubeRetryable
from shorts_engine import add_upload_hashtags, aligned_chat, feed, format_upload_title, get_source, publish_jobs, reserve


class ShortsEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = State(self.root)
        (self.root / "inbox").mkdir()
        (self.root / "edits").mkdir()
        self.source_hash = hashlib.sha256(b"fixture").hexdigest()
        self.manifest = {"creator": "xqc", "twitch_video_id": "123", "license_id": "license",
                         "source_sha256": self.source_hash}
        self.grant = {"status": "verified", "creator": "xqc", "twitch_video_id": "123", "commercial": True,
                      "edit": True, "upload": True, "gta_vi": True, "platform": "youtube_shorts",
                      "channel_id": CHANNEL_ID, "source_sha256": self.source_hash, "verified_by": "fixture",
                      "valid_from": now().isoformat(), "verified_at": now().isoformat(),
                      "evidence": dict.fromkeys(("creator", "gameplay", "audio", "source_delivery"),
                                                "https://example.com/fixture")}

    def rights(self, content):
        (self.root / "rights.json").write_text(json.dumps(content), encoding="utf-8")

    def queue(self, job_id, duration=20):
        manifest = self.root / "inbox" / f"{job_id}.json"
        manifest.write_text(json.dumps(self.manifest), encoding="utf-8")
        feed(self.state, {})
        output = self.root / "edits" / f"{job_id}.mp4"
        output.write_bytes(b"synthetic example for uploader mock")
        cover = output.with_suffix(".cover.jpg")
        cover.write_bytes(b"synthetic cover for uploader mock")
        with self.state.connect() as db:
            self.state.update(db, job_id, "ready", candidate='{"score": 95}', output_digest=sha256(output),
                              metadata=json.dumps({"title": "Fixture", "description": "Source and commentary",
                                                   "duration": duration, "layout": "react",
                                                   "face_on_output": [0.3, 0.1, 0.3, 0.2],
                                                   "gameplay_on_output": [0.0, 0.4, 1.0, 0.6],
                                                   "thumbnail": {"sha256": sha256(cover)}}))

    def get(self, job_id):
        with self.state.connect() as db:
            return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def test_rights_same_file_channel_scope_and_current_grant(self):
        with self.assertRaises(Blocked):
            authorize(self.root, self.manifest)
        self.rights([])
        with self.assertRaises(Blocked):
            authorize(self.root, self.manifest)
        self.rights({"license": {**self.grant, "source_sha256": "f" * 64}})
        with self.assertRaises(Blocked):
            authorize(self.root, self.manifest)
        self.rights({"license": self.grant})
        self.assertEqual(authorize(self.root, self.manifest), self.grant)

    def test_private_upload_is_durable_and_retries_reuse_id(self):
        self.rights({"license": self.grant})
        self.queue("first")
        youtube = Mock()
        youtube.recent_uploads.return_value = []
        youtube.start_upload.return_value = "example-session"
        youtube.resume_upload.return_value = "abcdefghijk"
        youtube.publish.side_effect = [YouTubeRetryable("processing"),
                                       {"snippet": {"publishedAt": now().isoformat()}}]
        publish_jobs(self.state, youtube)
        first = self.get("first")
        self.assertEqual(first["state"], "private")
        self.assertEqual(first["video_id"], "abcdefghijk")
        with self.state.connect() as db:
            db.execute("UPDATE jobs SET next_attempt=0")
        youtube.video.return_value = {"status": {"privacyStatus": "private"}}
        publish_jobs(self.state, youtube)
        self.assertEqual(self.get("first")["state"], "published")
        youtube.start_upload.assert_called_once()
        self.assertEqual(youtube.start_upload.call_args.kwargs["title"], "#xQc Fixture")
        self.assertEqual(json.loads(self.get("first")["metadata"])["title"], "#xQc Fixture")
        self.assertIn("#xQc #GTA6 #GTAVI #TwitchClips", youtube.start_upload.call_args.kwargs["description"])
        youtube.resume_upload.assert_called_once()
        youtube.set_shorts_thumbnail.assert_called_once()
        methods = [call[0] for call in youtube.mock_calls]
        self.assertLess(methods.index("set_shorts_thumbnail"), methods.index("publish"))

    def test_old_unprefixed_channel_upload_is_still_a_duplicate(self):
        self.rights({"license": self.grant})
        self.queue("first")
        youtube = Mock()
        youtube.recent_uploads.return_value = [{"video_id": "existing_id", "title": " fixture "}]

        publish_jobs(self.state, youtube)

        self.assertEqual(self.get("first")["state"], "rejected")
        self.assertIn("existing channel upload", self.get("first")["reason"])
        youtube.start_upload.assert_not_called()

    def test_legacy_session_reuses_its_video_and_updates_the_title_at_promotion(self):
        self.rights({"license": self.grant})
        self.queue("first")
        with self.state.connect() as db:
            self.state.update(db, "first", "uploading", session_url="existing-session")
        youtube = Mock()
        youtube.recent_uploads.return_value = [{"video_id": "abcdefghijk", "title": "Fixture"}]
        youtube.resume_upload.return_value = "abcdefghijk"
        youtube.publish.return_value = {"snippet": {"publishedAt": now().isoformat()}}

        publish_jobs(self.state, youtube)

        self.assertEqual(self.get("first")["state"], "published")
        youtube.start_upload.assert_not_called()
        self.assertEqual(youtube.resume_upload.call_args.args[0], "existing-session")
        self.assertEqual(youtube.publish.call_args.args, ("abcdefghijk",))
        self.assertEqual(youtube.publish.call_args.kwargs["title"], "#xQc Fixture")

    def test_thumbnail_failure_keeps_short_private(self):
        self.rights({"license": self.grant})
        self.queue("first")
        youtube = Mock()
        youtube.recent_uploads.return_value = []
        youtube.start_upload.return_value = "example-session"
        youtube.resume_upload.return_value = "abcdefghijk"
        youtube.set_shorts_thumbnail.side_effect = YouTubeRetryable("waiting for portrait cover")

        publish_jobs(self.state, youtube)

        self.assertEqual(self.get("first")["state"], "private")
        youtube.publish.assert_not_called()

    def test_legacy_ready_edit_is_rendered_again_instead_of_uploaded(self):
        self.rights({"license": self.grant})
        self.queue("first")
        with self.state.connect() as db:
            self.state.update(db, "first", "ready", metadata='{"duration": 20, "layout": "blur"}')
        youtube = Mock()

        publish_jobs(self.state, youtube)

        self.assertEqual(self.get("first")["state"], "queued")
        youtube.start_upload.assert_not_called()

    def test_kill_and_changed_manifest_stop_external_mutation(self):
        self.rights({"license": self.grant})
        self.queue("first")
        youtube = Mock()
        (self.root / "STOP_UPLOADS").touch()
        publish_jobs(self.state, youtube)
        self.assertEqual(self.get("first")["reason"], "upload kill switch is active")
        (self.root / "STOP_UPLOADS").unlink()
        (self.root / "inbox" / "first.json").write_text("{}", encoding="utf-8")
        with self.state.connect() as db:
            db.execute("UPDATE jobs SET next_attempt=0")
        publish_jobs(self.state, youtube)
        self.assertIn("manifest", self.get("first")["reason"])
        youtube.start_upload.assert_not_called()
        youtube.publish.assert_not_called()

    def test_upload_reservations_count_towards_daily_limit(self):
        self.queue("first")
        self.queue("second")
        with self.state.connect() as db:
            self.state.update(db, "first", "private", reserved_day=now().isoformat())
        with self.assertRaises(Blocked):
            reserve(self.state, self.get("second"), {"daily_limit": 1})

    def test_legacy_edits_outside_seven_to_thirty_never_upload(self):
        self.rights({"license": self.grant})
        self.queue("long", duration=30.001)
        self.queue("short", duration=6.999)
        youtube = Mock()
        publish_jobs(self.state, youtube)
        youtube.start_upload.assert_not_called()
        for name in ("long", "short"):
            self.assertIn("7–30", self.get(name)["reason"])

    def test_campaign_hashtags_are_exact_relevant_and_deduplicated(self):
        for creator, (campaign_id, required) in CAMPAIGNS.items():
            metadata = {"description": f"Source and commentary\n\n#GTA6 {required}",
                        "hashtags": ["#GTA6", required]}
            add_upload_hashtags(metadata, creator, campaign_id, "reaction")
            self.assertEqual(metadata["hashtags"][0], required)
            self.assertEqual(metadata["description"].count(required), 1)
            self.assertEqual(metadata["description"].count("#GTA6"), 1)
            self.assertTrue(metadata["description"].startswith("Paid clipping campaign."))
            self.assertIn("#StreamerReaction", metadata["description"])
        normal = {"description": "Source and commentary"}
        add_upload_hashtags(normal, "xqc", None, "unrelated topic")
        self.assertEqual(normal["hashtags"], ["#xQc", "#GTA6", "#GTAVI", "#TwitchClips"])

    def test_title_has_one_canonical_prefix_within_youtube_limit(self):
        for creator, (_, hashtag) in CAMPAIGNS.items():
            original = f"{hashtag.lower()}: The side gate opens. {hashtag.upper()}"
            title = format_upload_title(original, creator)
            self.assertEqual(title, f"{hashtag} The side gate opens.")
            self.assertEqual(format_upload_title(title, creator), title)
        long_title = format_upload_title("An unexpected GTA VI scene " * 8, "ishowspeed")
        self.assertTrue(long_title.startswith("#IShowSpeed "))
        self.assertTrue(long_title.endswith("…"))
        self.assertLessEqual(len(long_title), 100)
        self.assertEqual(format_upload_title(long_title, "ishowspeed"), long_title)

    def test_viewer_quotes_require_clearance_timing_and_no_identifying_fields(self):
        evidence = "https://example.com/permission"
        editorial = {"policy": {"viewer_messages_safe": True}, "viewer_messages": [
            {"offset": 12, "text": "How did that happen?", "reuse_evidence": evidence, "username": "example"},
            {"offset": 13, "text": "Nobody saw that coming", "reuse_evidence": evidence},
            {"offset": 14, "text": "@username nice", "reuse_evidence": evidence},
            {"offset": 15, "text": "No way", "reuse_evidence": evidence},
            {"offset": 16, "text": "Missing permission"},
            {"offset": 19.9, "text": "Too late", "reuse_evidence": evidence},
        ]}
        self.assertEqual(cleared_viewer_messages(self.root, self.grant, editorial, 10, 20), [])
        grant = {**self.grant, "evidence": {**self.grant["evidence"], "chat": evidence}}
        self.assertEqual(cleared_viewer_messages(self.root, grant, editorial, 10, 20), [
            {"offset": 2, "text": "How did that happen?"}, {"offset": 5, "text": "No way"},
        ])

    def test_native_source_verifies_exact_clip_rights_and_aligns_chat(self):
        manifest = {**self.manifest, "twitch_clip_id": "NativeClip"}
        self.rights({"license": self.grant})
        with self.assertRaises(Blocked):
            authorize(self.root, manifest)
        self.grant.update(twitch_clip_id="NativeClip", download=True,
                          evidence={**self.grant["evidence"], "platform": "https://example.com/permission"})
        self.rights({"license": self.grant})
        grant = authorize(self.root, manifest)
        client = Mock()
        client.get_clip.return_value = {"broadcaster_id": "5", "video_id": "123", "game_id": "6", "vod_offset": 50}
        client.get_gta_vi_game_id.return_value = "6"
        def fixture(clip_id, broadcaster, destination, *, guard, max_bytes):
            self.assertEqual((clip_id, broadcaster), ("NativeClip", "5"))
            self.assertTrue(guard())
            destination.write_bytes(b"fixture")
        client.download_clip.side_effect = fixture
        stream_start = now().replace(microsecond=0)
        with self.state.connect() as db:
            db.execute("INSERT INTO streams VALUES ('stream', 'xqc', ?, ?, NULL, 1, '{}')",
                       (stream_start.isoformat(), stream_start.isoformat()))
            for offset in (49, 51, 54):
                db.execute("INSERT INTO chat_signal VALUES ('stream', ?, 10, 4)",
                           (int(stream_start.timestamp()) + offset,))
        self.assertIsNone(aligned_chat(self.state, "other-stream", stream_start))
        with patch("shorts_engine.official_video", return_value=(client, "5", {"stream_id": "stream"})):
            result = get_source(self.state, {"digest": "manifest-test"}, manifest, grant)
            self.assertEqual(result[1:3], (self.source_hash, 50))
            self.assertEqual(result[3], [{"offset": 1, "count": 10, "reactions": 4},
                                         {"offset": 4, "count": 10, "reactions": 4}])
            self.assertEqual(get_source(self.state, {"digest": "manifest-test"}, manifest, grant), result)
        client.download_clip.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("cv2"),
                         "media dependencies are optional for this policy test")
    def test_worker_stops_before_accessing_unlicensed_media(self):
        from shorts_engine import prepare

        self.queue("first")
        with patch("shorts_engine.sha256") as read_media, patch("engine_media.analyze") as analyze:
            prepare(self.state, self.get("first"))
        read_media.assert_not_called()
        analyze.assert_not_called()
        self.assertEqual(self.get("first")["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
