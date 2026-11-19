import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import engine_media as media


class MediaTest(unittest.TestCase):
    def test_near_duplicate_rejects_static_visual_false_positive(self):
        static = {"frames": ["0013"] * 14, "shingles": []}
        self.assertFalse(media.near_duplicate(static, static))
        changing = {"frames": [f"{index:016x}" for index in range(14)], "shingles": []}
        self.assertTrue(media.near_duplicate(changing, changing))

    def test_local_semantic_ranking_preserves_editorial_score_evidence(self):
        factors = dict.fromkeys(media.DEFAULT_WEIGHTS, 50)
        factors["hook_strength"] = 96
        candidate = {"window_index": 0, "score": 50, "factors": factors, "text": "It worked.", "signals": {}}
        response_rows = [{"index": 0, **dict.fromkeys(media.SEMANTIC_FACTORS, 90)}]
        response = Mock(status=200)
        response.read.return_value = json.dumps({"done": True, "message": {
            "content": json.dumps({"rows": response_rows}),
        }}).encode()
        with patch.dict(os.environ, {"SHORTS_OLLAMA_MODEL": "local:model"}):
            with patch.object(media.http.client, "HTTPConnection") as connection:
                connection.return_value.getresponse.return_value = response
                media._semantic_rank([candidate], [{"editorial": {"factors": {"hook_strength": 96}}}],
                                     media.DEFAULT_WEIGHTS)
        connection.assert_called_once_with("127.0.0.1", 11434, timeout=12)
        self.assertEqual(candidate["factors"]["hook_strength"], 96)
        self.assertEqual(candidate["factors"]["narrative_payoff"], 64)

    def test_viewer_overlay_keeps_literal_text_and_fits_the_window(self):
        subtitles = "[V4+ Styles]\n\n[Events]\n"
        self.assertEqual(media._live_chat_overlay(subtitles, None, 7, "blur"), (subtitles, 0))
        messages = [{"offset": 4.5, "text": r"{W} \N", "duration": 2},
                    {"offset": 6.25, "text": "What a turn!", "duration": 3}]
        rendered, count = media._live_chat_overlay(subtitles, messages, 7, "blur")
        self.assertEqual(count, 2)
        self.assertIn(r"\{W\}" + " \\\u2060N", rendered)
        self.assertIn("0:00:06.25,0:00:07.00,LiveChatMessage", rendered)
        self.assertEqual(rendered.count("}Live chat"), 1)
        self.assertIn("0:00:00.00,0:00:02.00,PaidDisclosure", media._paid_disclosure(rendered))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows speech synthesis required")
    def test_render_with_measured_narration_captions(self):
        try:
            ffmpeg = media._tool("ffmpeg")
            media._tool("ffprobe")
        except media.MediaError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory(prefix="shorts media test ") as folder:
            root = Path(folder)
            audio, words, _ = media._synthesize(
                "No way, wait for the gate. The truck blocked the lane, so I turned the corner and made it out.", root,
            )
            self.assertGreater(len(words), 14)
            source = root / "source.mp4"
            media._run([ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=duration=17:size=640x360:rate=30", "-i", str(audio), "-af", "apad", "-t", "16",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source)])
            editorial = {
                "event": "The truck forces an alley escape",
                "angle": "The blocked lane changes the useful route before the chase reaches the next intersection.",
                "narration": "Look left and watch the car go past the truck and dodge the crash.",
                "factors": {key: 94 for key in media.DEFAULT_WEIGHTS if key not in {"timeliness", "audience_engagement"}},
                "layout": "blur",
                "viewer_messages": [{"offset": 1.5, "text": "That timing!!"}],
                "paid_campaign": True,
            }
            candidates = media.analyze(source, observed_at=datetime.now(timezone.utc),
                                       chat=[{"offset": 5, "count": 45, "reactions": 30}],
                                       windows=[{"start": 0, "end": 7, "editorial": editorial}], words=words)
            self.assertEqual(candidates[0]["window_index"], 0)
            self.assertGreaterEqual(candidates[0]["score"], 85)
            self.assertTrue(candidates[0]["fingerprint"]["shingles"])
            output = root / "rendered short.mp4"
            result = media.produce(source, candidates[0], editorial, output, creator="jasontheween",
                                   source_url="https://www.twitch.tv/jasontheween")
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_suffix(".json").is_file())
            self.assertEqual((result["quality"]["width"], result["quality"]["height"]), (1080, 1920))
            self.assertEqual(result["narration_type"], "synthetic")
            self.assertEqual(result["duration"], 7)
            self.assertEqual(result["viewer_messages_shown"], 1)
            self.assertTrue(result["paid_campaign_disclosure_shown"])
            self.assertEqual(len(result["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
