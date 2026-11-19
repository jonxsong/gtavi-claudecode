"""Fail-closed rights and policy checks for authorized source packages."""

import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from check import banned_in
from engine_state import now, read_json, timestamp
from engine_twitch import CREATORS

CHANNEL_ID = "UCAtLKFamARYmLeTcwzA3akw"
CAMPAIGNS = {
    "kaicenat": ("1d2bcb00-c904-415f-aa82-f95b96baad86", "#KaiCenat"),
    "jasontheween": ("0b178e3c-fbd3-4294-8ded-394e3da9c62f", "#JasonTheWeen"),
}
MEDIA_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
EVIDENCE_FILES = {".pdf", ".eml", ".txt", ".md"}
CLIP_ID = re.compile(r"[A-Za-z0-9_-]{1,160}")
PRIVATE_MESSAGE = re.compile(r"(?i)https?://|www\.|@|(?:\d[\s().-]*){7}|\b(?:dox|doxx)\b")
CHECKS = {"gta_vi_segment", "accurate_context", "source_and_audio_cleared", "original_analysis",
          "youtube_community_guidelines", "youtube_reused_content", "youtube_ad_suitability",
          "no_major_story_spoilers"}


class Blocked(ValueError):
    """Evidence or an external integration is missing and can be supplied later."""


class Rejected(ValueError):
    """The package does not meet a fixed requirement."""


def is_evidence(value):
    if not isinstance(value, str):
        return False
    url = urlsplit(value)
    return (url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
            or bool(re.fullmatch(r"[a-zA-Z0-9_-]+\.(pdf|eml|txt|md)", value)))


def safe_file(root, name, extensions):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ":" in name:
        raise Rejected("asset path must be relative to inbox")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() not in extensions:
        raise Rejected("asset path or file type is invalid")
    return path


def evidence_exists(root, reference):
    return is_evidence(reference) and (reference.startswith("https://")
                                       or safe_file(root / "evidence", reference, EVIDENCE_FILES).is_file())


def check_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Rejected("asset SHA-256 is required")
    return value


def authorize(root, manifest):
    """Check the authorization record before opening or even probing source media."""
    creator = manifest.get("creator")
    if not isinstance(creator, str) or creator not in CREATORS:
        raise Rejected("creator is outside the Twitch whitelist")
    video_id = manifest.get("twitch_video_id")
    if not isinstance(video_id, str) or not re.fullmatch(r"[0-9]{1,22}", video_id):
        raise Rejected("an exact Twitch VOD ID is required")
    grant_id = manifest.get("license_id")
    if not isinstance(grant_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", grant_id):
        raise Blocked("a verified footage permission record is required")
    try:
        grants = read_json(root / "rights.json")
        if not isinstance(grants, dict):
            raise ValueError("rights are not an object")
        grant = grants.get(grant_id)
    except FileNotFoundError as exc:
        raise Blocked("rights.json has no verified footage permissions") from exc
    except (OSError, ValueError) as exc:
        raise Blocked("rights.json is temporarily unreadable or malformed") from exc
    if not isinstance(grant, dict) or grant.get("status") != "verified":
        raise Blocked("footage permission is unverified or revoked")
    if grant.get("creator") != creator or grant.get("twitch_video_id") != video_id:
        raise Blocked("permission does not cover this creator and Twitch source")
    if grant.get("channel_id") != CHANNEL_ID or grant.get("platform") != "youtube_shorts":
        raise Blocked("permission must cover Shorts on the designated channel")
    if any(grant.get(flag) is not True for flag in ("commercial", "edit", "upload", "gta_vi")):
        raise Blocked("permission does not cover commercial GTA VI edits and uploads")
    if not isinstance(grant.get("verified_by"), str) or not grant["verified_by"].strip():
        raise Blocked("record who verified the license")
    current = now()
    try:
        verified_at = timestamp(grant.get("verified_at"))
        valid_from = timestamp(grant.get("valid_from"))
        valid_through = timestamp(grant["valid_through"]) if grant.get("valid_through") else None
    except ValueError as exc:
        raise Blocked("permission dates are invalid") from exc
    if verified_at > current or current < valid_from or (valid_through and current >= valid_through):
        raise Blocked("permission is not currently effective")
    evidence = grant.get("evidence")
    if not isinstance(evidence, dict) or not all(evidence_exists(root, evidence.get(key)) for key in
                                                 ("creator", "gameplay", "audio", "source_delivery")):
        raise Blocked("clearance evidence is required for creator, gameplay, audio and source delivery")
    twitch_clip_id = manifest.get("twitch_clip_id")
    if twitch_clip_id is not None:
        if not isinstance(twitch_clip_id, str) or not CLIP_ID.fullmatch(twitch_clip_id) or manifest.get("source"):
            raise Rejected("native Twitch intake requires an exact clip ID and no local source override")
        if grant.get("twitch_clip_id") != twitch_clip_id or grant.get("download") is not True:
            raise Blocked("permission must cover direct download of this exact Twitch clip")
        if not evidence_exists(root, evidence.get("platform")):
            raise Blocked("direct import requires Twitch off-platform reuse evidence")
        if grant.get("source_sha256") is not None:
            check_hash(grant["source_sha256"])
    else:
        source_digest = check_hash(manifest.get("source_sha256"))
        if grant.get("source_sha256") != source_digest:
            raise Blocked("permission does not identify the delivered source file")
    campaign = manifest.get("campaign_id")
    if grant.get("campaign_id") != campaign:
        raise Blocked("campaign does not match the permission record")
    if campaign and CAMPAIGNS.get(creator, (None,))[0] != campaign:
        raise Rejected("campaign is outside the verified campaign list")
    return grant


def editorial_policy(editorial, campaign, root):
    if not isinstance(editorial, dict):
        raise Rejected("original editorial material is required")
    policy = editorial.get("policy")
    if not isinstance(policy, dict) or any(policy.get(check) is not True for check in CHECKS):
        raise Rejected("editorial input requires positive rights, accuracy and YouTube checks")
    if not evidence_exists(root, policy.get("evidence")):
        raise Rejected("editorial input must reference the source of its checks")
    if policy.get("made_for_kids") is not False or policy.get("age_restricted") is not False:
        raise Rejected("candidate is not eligible for this channel's unattended Shorts workflow")
    texts = [editorial.get(field) for field in ("event", "angle", "narration")]
    if not all(isinstance(value, str) and value.strip() for value in texts):
        raise Rejected("specific event, editorial angle and authored narration are required")
    if banned_in(" ".join(texts)):
        raise Rejected("editorial copy contains rejected topic terms")
    if campaign and policy.get("campaign_requirements_met") is not True:
        raise Rejected("campaign candidate requires evidence of its specific submission rules")


def cleared_viewer_messages(root, grant, editorial, start, end):
    """Take genuine, cleared quotes with file-relative timing and omit viewer identities."""
    quotes = editorial.get("viewer_messages")
    policy = editorial.get("policy", {})
    if (not isinstance(quotes, list) or not isinstance(policy, dict)
            or policy.get("viewer_messages_safe") is not True
            or not evidence_exists(root, grant["evidence"].get("chat"))):
        return []
    approved = []
    for message in quotes[:20]:
        if not isinstance(message, dict) or not evidence_exists(root, message.get("reuse_evidence")):
            continue
        offset, quote = message.get("offset"), message.get("text")
        if (type(offset) not in (float, int) or not math.isfinite(offset) or not start <= offset <= end - 1
                or not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 60
                or not quote.strip().isprintable() or PRIVATE_MESSAGE.search(quote)
                or banned_in(quote)):
            continue
        approved.append({"offset": round(offset - start, 3), "text": quote.strip()})
    selected, seen = [], set()
    for message in sorted(approved, key=lambda item: item["offset"]):
        text_key = message["text"].casefold()
        if text_key not in seen and (not selected or message["offset"] >= selected[-1]["offset"] + 2):
            selected.append(message)
            seen.add(text_key)
        if len(selected) == 3:
            break
    return selected
