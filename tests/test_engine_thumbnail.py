import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from scripts.engine_thumbnail import ThumbnailRejected, render_thumbnail


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.video = Path(work.name) / "edit.mp4"
        self.cover = Path(work.name) / "cover.jpg"
        writer = cv2.VideoWriter(str(self.video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (360, 640))
        self.assertTrue(writer.isOpened())
        frame = np.full((640, 360, 3), (20, 30, 45), dtype=np.uint8)
        frame[:220] = (50, 110, 210)
        frame[320:550] = (215, 95, 40)
        frame[320:440] = (115, 222, 80)
        try:
            for _ in range(80):
                writer.write(frame)
        finally:
            writer.release()
        self.metadata = {"hook": "His first Vice City chase", "layout": "react",
                         "start": 41.0, "peak": 46.2, "words": [{"s": 46.1, "e": 46.3}],
                         "facecam_panel": [0, 0, 1, 220 / 640], "gameplay_on_output": [0, 0.5, 1, 230 / 640],
                         "face_on_output": [127 / 360, 43 / 640, 103 / 360, 129 / 640]}

    def test_cover_uses_peak_relative_to_edit_and_keeps_the_real_panels(self):
        with patch("scripts.engine_thumbnail._face_choice", return_value=(3.1, (127, 43, 230, 172))):
            result = render_thumbnail(self.video, self.metadata, self.cover)
        self.assertEqual(result["headline"], self.metadata["hook"])
        self.assertAlmostEqual(result["frame_offset"], 5.2, places=1)
        self.assertTrue(result["face_detected"])
        self.assertLess(result["size_bytes"], 2_000_000)
        with Image.open(self.cover) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (1080, 1920))
            red, green, blue = image.getpixel((540, 480))
            self.assertGreater(red, max(green, blue) + 35)
            red, green, blue = image.getpixel((540, 1600))
            self.assertGreater(blue, max(green, red) + 35)

    def test_rejects_when_no_usable_face_is_detected(self):
        with patch("scripts.engine_thumbnail._face_choice", return_value=None):
            with self.assertRaisesRegex(ThumbnailRejected, "clear streamer face"):
                render_thumbnail(self.video, self.metadata, self.cover)
        self.assertFalse(self.cover.exists())
