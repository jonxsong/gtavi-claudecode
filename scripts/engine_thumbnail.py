"""Compose a Shorts cover from a verified streamer camera and an actual game frame."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

if __package__:
    from .facecam import MODEL
else:
    from facecam import MODEL


WIDTH, HEIGHT = 1080, 1920
FACE_HEIGHT = 1070
MAX_JPEG_BYTES = 2_000_000
GENERIC = re.compile(r"(?:gta\s*(?:6|vi)\s+moment|how the gta\s*(?:6|vi)\s+moment unfolded|"
                     r"a closer look at this gta\s*(?:6|vi)\s+moment)\W*$", re.I)


class ThumbnailError(RuntimeError):
    """The local cover could not be generated."""


class ThumbnailRejected(ThumbnailError):
    """The edit has no suitable thumbnail source or headline."""


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _rect(box, width, height):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    values = [_number(value) for value in box]
    if None in values:
        return None
    x, y, w, h = values
    if not (0 <= x < x + w <= 1.001 and 0 <= y < y + h <= 1.001):
        return None
    left, top = round(x * width), round(y * height)
    right, bottom = min(width, round((x + w) * width)), min(height, round((y + h) * height))
    return (left, top, right, bottom) if right - left >= 12 and bottom - top >= 12 else None


def _regions(metadata, width, height):
    camera = _rect(metadata.get("facecam_panel"), width, height)
    face = _rect(metadata.get("face_on_output"), width, height)
    game = _rect(metadata.get("gameplay_on_output"), width, height)
    if not camera or not face or not game:
        raise ThumbnailRejected("The edit has no verified camera, face and gameplay geometry")
    return camera, face, game


def _timing(metadata, duration):
    start = _number(metadata.get("start")) or 0.0
    peak = _number(metadata.get("peak_offset"))
    if peak is None and (source_peak := _number(metadata.get("peak"))) is not None:
        peak = source_peak - start
    if peak is None or not 0 <= peak < duration:
        peak = duration * 0.56

    speech = []
    words = metadata.get("words")
    if isinstance(words, (list, tuple)):
        for word in words:
            if isinstance(word, dict):
                left, right = _number(word.get("s")), _number(word.get("e"))
                if left is not None and right is not None and start <= left < right <= start + duration:
                    speech.append((left + right) / 2 - start)
    offsets = metadata.get("caption_offsets")
    if isinstance(offsets, (list, tuple)):
        speech.extend(value for value in map(_number, offsets) if value is not None and 0 <= value < duration)

    samples = [duration * fraction for fraction in (0.16, 0.28, 0.42, 0.57, 0.72, 0.88)]
    samples.extend(peak + offset for offset in (-1, -0.48, -0.16, 0, 0.18, 0.46, 1.0))
    samples.extend(sorted(speech, key=lambda value: abs(value - peak))[:2])
    return peak, speech, samples


def _detector():
    if MODEL.is_file() and hasattr(cv2, "FaceDetectorYN"):
        try:
            return cv2.FaceDetectorYN.create(str(MODEL), "", (320, 320), 0.76, 0.3, 300), None
        except cv2.error:
            pass
    cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
    if cascade.empty():
        raise ThumbnailError("OpenCV has no local face detector")
    return None, cascade


def _face_rows(detector, frame):
    yunet, cascade = detector
    height, width = frame.shape[:2]
    scale = min(1, 800 / max(width, height))
    resized = cv2.resize(frame, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    if yunet is not None:
        yunet.setInputSize((resized.shape[1], resized.shape[0]))
        _, rows = yunet.detect(resized)
        return [] if rows is None else [(row[:4] / scale, float(row[14]), row[4:14].reshape(5, 2) / scale)
                                         for row in rows]
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    return [(np.asarray(row) / scale, 0.77, None) for row in
            cascade.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=6, minSize=(36, 36))]


def _expression(gray, landmarks, width, height):
    if landmarks is None:
        return 0.0
    right_eye, left_eye, nose, right_mouth, left_mouth = landmarks
    eyes = abs(float(right_eye[0] - left_eye[0]))
    if eyes < 0.20 * width or abs(right_eye[1] - left_eye[1]) > 0.41 * eyes:
        return -0.3
    left, right = sorted((int(right_mouth[0]), int(left_mouth[0])))
    if right - left < 4 or nose[1] <= min(right_eye[1], left_eye[1]):
        return -0.15
    mouth = float((right_mouth[1] + left_mouth[1]) / 2)
    y1, y2 = max(0, int(mouth - 0.02 * height)), min(gray.shape[0], int(mouth + 0.12 * height))
    x1, x2 = max(0, int(left + 0.20 * (right - left))), min(gray.shape[1], int(right - 0.20 * (right - left)))
    interior = gray[y1:y2, x1:x2]
    if interior.size == 0:
        return 0.0
    openness = float(np.mean(interior < max(35, np.median(interior) * 0.66)))
    return min(1.0, 0.55 * (right - left) / eyes + 1.4 * openness)


def _game_score(frame, game):
    gx1, gy1, gx2, gy2 = game
    region = frame[gy1:gy2, gx1:gx2]
    reduced = cv2.resize(region, (160, 90), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(reduced, cv2.COLOR_BGR2GRAY)
    return min(1.0, float(gray.std()) / 48), float(gray.mean())


def _face_choice(frame, camera, hint, detector):
    x1, y1, x2, y2 = camera
    region = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    best = None
    for (left, top, width, height), confidence, landmarks in _face_rows(detector, region):
        # Reject detections clipped across a border or too small to enlarge cleanly.
        if (min(width, height) < max(38, frame.shape[1] * 0.067) or width / height < 0.50
                or width / height > 1.50 or left < 0 or top < 0
                or left + width > region.shape[1] or top + height > region.shape[0]):
            continue
        cx, cy = x1 + left + width / 2, y1 + top + height / 2
        hx1, hy1, hx2, hy2 = hint
        if abs(cx - (hx1 + hx2) / 2) > (hx2 - hx1) * 0.95 or abs(cy - (hy1 + hy2) / 2) > (hy2 - hy1) * 0.80:
            continue
        detail = gray[int(top):int(top + height), int(left):int(left + width)]
        normalized = cv2.resize(detail, (112, 112), interpolation=cv2.INTER_AREA)
        mean, contrast = float(normalized.mean()), float(normalized.std())
        sharpness = float(cv2.Laplacian(normalized, cv2.CV_32F).var())
        if not 28 <= mean <= 229 or contrast < 17 or sharpness < 22:
            continue
        quality = (1.8 * confidence + 1.10 * min(1.0, math.log1p(sharpness) / 6.5)
                   + 0.9 * _expression(gray, landmarks, width, height)
                   + 0.72 * min(1.0, width / (region.shape[1] * 0.32)))
        face = (round(x1 + left), round(y1 + top), round(x1 + left + width), round(y1 + top + height))
        if best is None or quality > best[0]:
            best = (quality, face)
    return best


def _select_frame(video, metadata):
    cap = cv2.VideoCapture(str(video))
    try:
        if not cap.isOpened():
            raise ThumbnailError("Could not decode the finished Short for its cover")
        fps = _number(cap.get(cv2.CAP_PROP_FPS))
        count = _number(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps is None or fps < 1 or count is None or count < 5:
            raise ThumbnailError("The finished Short has no measurable frame timeline")
        duration = count / fps
        peak, speech, samples = _timing(metadata, duration)
        positions = sorted({max(0, min(int(count) - 2, round(sample * fps))) for sample in samples})
        detector = _detector()
        best = None
        regions = None
        for position in positions:
            cap.set(cv2.CAP_PROP_POS_FRAMES, position)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            height, width = frame.shape[:2]
            if width < 160 or abs(width / height - WIDTH / HEIGHT) > 0.02:
                raise ThumbnailRejected("The cover source must be the vertical finished Short")
            if regions is None:
                regions = _regions(metadata, width, height)
            camera, hint, game = regions
            face = _face_choice(frame, camera, hint, detector)
            if face is None:
                continue
            game_quality, light = _game_score(frame, game)
            if game_quality < 0.24 or not 14 <= light <= 240:
                continue
            offset = position / fps
            closeness = 1.0 / (1.0 + abs(offset - peak) / 1.4)
            voice = max((1.0 / (1 + 5 * abs(offset - moment)) for moment in speech), default=0)
            opening_caption = 1.20 if offset < 2.65 else 0
            score = face[0] + 1.1 * closeness + 0.31 * voice + 0.55 * game_quality - opening_caption
            if best is None or score > best[0]:
                best = (score, frame, offset, camera, face[1], game)
        if best is None:
            raise ThumbnailRejected("No sampled frame showed a clear streamer face and readable gameplay")
        return best[1:]
    finally:
        cap.release()


def _portrait(camera, face):
    left, top, right, bottom = camera
    x1, y1, x2, y2 = face
    aspect = WIDTH / FACE_HEIGHT
    height = min(bottom - top, (right - left) / aspect, max((y2 - y1) * 1.96, (x2 - x1) * 1.9 / aspect))
    width = height * aspect
    center_x = (x1 + x2) / 2
    center_y = y1 + 0.65 * (y2 - y1)
    start_x = max(left, min(right - width, center_x - width / 2))
    start_y = max(top, min(bottom - height, center_y - height / 2))
    return tuple(map(round, (start_x, start_y, start_x + width, start_y + height)))


def _headline(metadata):
    for key in ("thumbnail_headline", "short_event", "hook", "title"):
        original = metadata.get(key)
        if not isinstance(original, str):
            continue
        text = " ".join(original.split())
        if re.fullmatch(r"[\w .,!?\-–—'’&+:]+", text) and 2 <= len(text.split()) <= 14 and len(text) <= 86:
            if not GENERIC.search(text):
                return text
    raise ThumbnailRejected("No grounded, readable headline was provided for the cover")


def _font_name():
    fonts = Path(os.getenv("WINDIR", r"C:\Windows")) / "Fonts"
    for candidate in (fonts / "ariblk.ttf", fonts / "seguibl.ttf", fonts / "arialbd.ttf",
                      Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if candidate.is_file():
            return str(candidate)
    raise ThumbnailError("Install a local bold TrueType font for the cover")


def _title_lines(draw, headline):
    filename = _font_name()
    for size in range(130, 66, -4):
        font = ImageFont.truetype(filename, size)
        lines = [""]
        for word in headline.upper().split():
            combined = (lines[-1] + " " + word).lstrip()
            if lines[-1] and draw.textlength(combined, font=font) > WIDTH - 116:
                lines.append(word)
            else:
                lines[-1] = combined
        if len(lines) <= (2 if size > 104 else 3) and max(draw.textlength(line, font=font) for line in lines) <= WIDTH - 116:
            return lines, font
    raise ThumbnailRejected("The cover headline does not fit in the mobile safe area")


def _compose(frame, camera, face, game, headline):
    still = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    portrait = ImageOps.fit(still.crop(_portrait(camera, face)), (WIDTH, FACE_HEIGHT), Image.Resampling.LANCZOS)
    portrait = ImageEnhance.Contrast(portrait).enhance(1.065)
    portrait = portrait.filter(ImageFilter.UnsharpMask(radius=1.0, percent=42, threshold=4))
    gameplay = ImageOps.fit(still.crop(game), (WIDTH, HEIGHT - FACE_HEIGHT), Image.Resampling.LANCZOS,
                            centering=(0.50, 0.45))
    cover = Image.new("RGB", (WIDTH, HEIGHT))
    cover.paste(portrait, (0, 0))
    cover.paste(gameplay, (0, FACE_HEIGHT))

    rows = np.arange(HEIGHT)
    fade = np.interp(rows, (0, 670, 825, 1010, 1125, 1255, 1445, 1860, HEIGHT),
                     (12, 0, 12, 232, 228, 82, 0, 10, 55)).astype(np.uint8)
    shade = Image.fromarray(fade.reshape(HEIGHT, 1)).resize((WIDTH, HEIGHT))
    cover.paste((9, 12, 21), (0, 0, WIDTH, HEIGHT), shade)
    draw = ImageDraw.Draw(cover)
    lines, font = _title_lines(draw, headline)
    gap = round(font.size * 0.95)
    y = FACE_HEIGHT - 6 - ((len(lines) - 1) * gap) / 2
    for index, line in enumerate(lines):
        fill = "#efff72" if index == len(lines) - 1 else "#ffffff"
        draw.text((WIDTH // 2, round(y + index * gap)), line, fill=fill, stroke_width=5,
                  stroke_fill="#10131f", font=font, anchor="mm")
    return cover


def render_thumbnail(edited_video, metadata, output):
    """Write a grounded custom JPEG and report the real frame selected from the edit."""
    edited_video = Path(edited_video).resolve()
    output = Path(output).resolve()
    if edited_video.suffix.lower() != ".mp4" or not edited_video.is_file():
        raise ThumbnailError("A local finished MP4 is required for the cover")
    if output.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ThumbnailError("The cover must be a JPEG file")
    if not isinstance(metadata, dict):
        raise ThumbnailRejected("The edit requires thumbnail metadata")
    headline = _headline(metadata)
    try:
        frame, offset, camera, face, game = _select_frame(edited_video, metadata)
        cover = _compose(frame, camera, face, game, headline)
        for quality in (92, 86, 78, 68):
            with io.BytesIO() as data:
                cover.save(data, "JPEG", quality=quality, optimize=True, subsampling=0)
                contents = data.getvalue()
            if len(contents) <= MAX_JPEG_BYTES:
                break
        else:
            raise ThumbnailError("Could not compress the cover under the JPEG size limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".jpg.part", delete=False) as file:
                temporary_path = Path(file.name)
                file.write(contents)
            temporary_path.replace(output)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    except (OSError, cv2.error) as error:
        raise ThumbnailError("The local cover could not be rendered") from error
    return {"file": str(output), "frame_offset": round(offset, 3), "face_detected": True,
            "width": WIDTH, "height": HEIGHT, "size_bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(), "headline": headline}
