import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import engine_media as media, facecam


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
        self.assertEqual(media._live_chat_overlay(subtitles, None, 7), (subtitles, 0))
        messages = [{"offset": 4.5, "text": r"{W} \N", "duration": 2},
                    {"offset": 6.25, "text": "What a turn!", "duration": 3}]
        rendered, count = media._live_chat_overlay(subtitles, messages, 7)
        self.assertEqual(count, 2)
        self.assertIn(r"\{W\}" + " \\\u2060N", rendered)
        self.assertIn("0:00:06.25,0:00:07.00,LiveChatMessage", rendered)
        self.assertEqual(rendered.count("}Live chat"), 1)
        self.assertIn("0:00:00.00,0:00:02.00,PaidDisclosure", media._paid_disclosure(rendered))

    def test_missing_facecam_and_duplicate_gameplay_are_blocked(self):
        import numpy as np

        frames = [np.zeros((360, 640, 3), dtype=np.uint8)] * 9
        with patch.object(facecam, "sample_frames", return_value=frames):
            with patch.object(facecam, "find_cam", return_value={"mode": "none"}):
                with self.assertRaisesRegex(media.MediaFramingRejected, "clearly bounded streamer facecam"):
                    media._layout(Path("gameplay-only.mp4"), 0, 7, {})
            with self.assertRaisesRegex(media.MediaFramingRejected, "must exclude the facecam"):
                media._layout(Path("same-source.mp4"), 0, 7, {"facecam_box": [0, 0, 0.25, 0.35],
                                                               "gameplay_box": [0, 0, 1, 1]})
            with self.assertRaisesRegex(media.MediaFramingRejected, "requires facecam above gameplay"):
                media._layout(Path("requested-blur.mp4"), 0, 7, {"layout": "blur"})

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows speech synthesis required")
    def test_render_with_distinct_facecam_and_paused_gameplay(self):
        import numpy as np

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
                        "testsrc2=duration=17:size=640x360:rate=30", "-f", "lavfi", "-i",
                        "color=black:size=160x128:rate=30:duration=17", "-i", str(audio),
                        "-filter_complex", "[0:v]loop=loop=-1:size=1:start=0,hue=s=0[gray];"
                        "[1:v]geq=lum='85+30*sin(T*6)':cb=100:cr=220[red];"
                        "[gray][red]overlay=0:0[video]", "-map", "[video]", "-map", "2:a", "-af", "apad", "-t", "16",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source)])
            editorial = {
                "event": "The truck forces an alley escape",
                "angle": "The blocked lane changes the useful route before the chase reaches the next intersection.",
                "narration": "Look left and watch the car go past the truck and dodge the crash.",
                "factors": {key: 94 for key in media.DEFAULT_WEIGHTS if key not in {"timeliness", "audience_engagement"}},
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
            camera = {"mode": "cam", "confidence": 0.95, "box": [0, 0, 0.25, 128 / 360]}
            with patch.object(facecam, "find_cam", return_value=camera):
                with patch.object(facecam, "detect_faces", return_value=[[(50, 28, 60, 64, 0.95)]] * 9):
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
            self.assertEqual(result["layout"], "react")
            self.assertGreater(result["gameplay_box"][0], 0.25)
            self.assertLess(result["facecam_panel"][1], result["gameplay_on_output"][1])
            pixels = media._run([ffmpeg, "-v", "error", "-ss", "4.5", "-i", str(output), "-frames:v", "1",
                                 "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]).stdout
            frame = np.frombuffer(pixels, dtype=np.uint8).reshape(1920, 1080, 3)
            face_red, face_green, face_blue = map(int, frame[405, 540])
            gameplay_channels = list(map(int, frame[1100, 510]))
            self.assertGreater(face_red - max(face_green, face_blue), 70)
            self.assertLessEqual(max(gameplay_channels) - min(gameplay_channels), 6)


if __name__ == "__main__":
    unittest.main()
