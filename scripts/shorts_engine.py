"""Unattended Twitch-to-YouTube Shorts service for GTA VIBING."""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import engine_metrics as metrics
from check import banned_in
from engine_rights import CAMPAIGNS, CHANNEL_ID, MEDIA_TYPES, Blocked, Rejected, authorize, check_hash
from engine_rights import cleared_viewer_messages, editorial_policy, safe_file
from engine_state import State, json_text, now, read_json, sha256, timestamp
from engine_twitch import CREATORS, EventSubMonitor, TwitchClient, TwitchError, gta_vi_eligibility, stream_keywords


LOG = logging.getLogger("shorts_engine")
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / ".shorts-engine"
GIB = 1024 ** 3
MIN_SECONDS = 7
MAX_SECONDS = 30
CREATOR_HASHTAGS = {
    "ishowspeed": "#IShowSpeed", "kaicenat": "#KaiCenat", "jasontheween": "#JasonTheWeen",
    "moistcr1tikal": "#moistcr1tikal", "xqc": "#xQc",
}
TOPIC_HASHTAGS = {"gameplay": "#Gameplay", "reaction": "#StreamerReaction",
                  "discovery": "#GTA6Discovery", "news": "#GamingNews"}


def log_event(event, **fields):
    LOG.info(json_text({"at": now().isoformat(), "event": event, **fields}))


def settings(root):
    try:
        config = read_json(root / "settings.json")
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("settings.json must contain an object")
    threshold = config.get("threshold", 85)
    daily = config.get("daily_limit", 1)
    if type(threshold) not in (int, float) or not 85 <= threshold <= 100:
        raise ValueError("threshold must be 85–100")
    if type(daily) is not int or not 1 <= daily <= 10:
        raise ValueError("daily_limit must be between 1 and 10")
    return {**config, "threshold": threshold, "daily_limit": daily}


def publish_enabled(root):
    if (root / "STOP_UPLOADS").exists():
        raise Blocked("upload kill switch is active")
    return True


def feed(state, processing):
    inbox = state.root / "inbox"
    for path in inbox.glob("*.json"):
        try:
            if path.stem in processing or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", path.stem) or path.is_symlink():
                continue
            if path.stat().st_size > 512 * 1024:
                continue
            raw = path.read_bytes()
            manifest = json.loads(raw.decode("utf-8-sig"))
            if isinstance(manifest, dict):
                state.register(path.stem, manifest, hashlib.sha256(raw).hexdigest())
        except (ValueError, OSError) as error:
            log_event("manifest_unreadable", job=path.stem, error=type(error).__name__)


def publish_grant(state, job, manifest):
    publish_enabled(state.root)
    current = state.root / "inbox" / (job["id"] + ".json")
    if not current.is_file() or current.is_symlink() or sha256(current) != job["digest"]:
        raise Blocked("the original source manifest is missing or has changed")
    grant = authorize(state.root, manifest)
    if manifest.get("campaign_id"):
        raise Blocked("campaign release requires a working Content Rewards submission path")
    metadata = json.loads(job["metadata"])
    if grant.get("source_sha256") and metadata.get("source_sha256", manifest.get("source_sha256")) != grant["source_sha256"]:
        raise Blocked("current footage clearance does not cover the source used in this edit")
    if metadata.get("viewer_messages_included"):
        candidate = json.loads(job["candidate"])
        editorial = manifest["windows"][candidate["window_index"]]["editorial"]
        messages = cleared_viewer_messages(state.root, grant, editorial, candidate["start"], candidate["end"])
        if len(messages) != metadata["viewer_messages_included"]:
            raise Blocked("viewer message clearance is no longer available")
    return grant


def official_video(manifest):
    client = TwitchClient.from_env()
    creator_ids = client.resolve_creators()
    video = client.get_video(manifest["twitch_video_id"])
    if not video or video.get("user_login") != manifest["creator"]:
        raise Blocked("official Helix metadata did not verify this VOD's creator")
    return client, creator_ids[manifest["creator"]], video


def native_clip(client, broadcaster, manifest):
    clip = client.get_clip(manifest["twitch_clip_id"])
    if not clip or clip.get("broadcaster_id") != broadcaster or clip.get("video_id") != manifest["twitch_video_id"]:
        raise Blocked("Twitch clip has not resolved to the exact licensed creator and VOD")
    game_id = client.get_gta_vi_game_id()
    if not game_id or clip.get("game_id") != game_id:
        raise Blocked("Twitch has not verified this native clip's GTA VI category")
    offset = clip.get("vod_offset")
    if type(offset) is not int or offset < 0:
        raise Blocked("Twitch has not yet reported the native clip's VOD offset")
    return clip


def aligned_chat(state, stream_id, source_start):
    if not stream_id or source_start is None:
        return None
    first = source_start.timestamp()
    with state.connect() as db:
        rows = db.execute("SELECT at,count,reactions FROM chat_signal WHERE stream_id=? AND at>=? AND at<? ORDER BY at",
                          (stream_id, first, first + 1200)).fetchall()
    return [{"offset": row["at"] - first, "count": row["count"], "reactions": row["reactions"]}
            for row in rows] or None


def add_upload_hashtags(metadata, creator, campaign, category):
    required = CAMPAIGNS[creator][1] if campaign else CREATOR_HASHTAGS[creator]
    tags = [required, "#GTA6", "#GTAVI", "#TwitchClips"]
    topic = TOPIC_HASHTAGS.get(category.casefold().strip()) if isinstance(category, str) else None
    if topic:
        tags.append(topic)
    description = metadata["description"].rstrip()
    original_hashtags = " ".join(metadata.get("hashtags", ()))
    if original_hashtags and description.endswith(original_hashtags):
        description = description[:-len(original_hashtags)].rstrip()
    if campaign:
        description = f"Paid clipping campaign.\n\n{description}"
    metadata.update(description=f"{description}\n\n{' '.join(tags)}", hashtags=tags, tags=[tag[1:] for tag in tags])


def get_source(state, job, manifest, grant):
    client, broadcaster, video = official_video(manifest)
    native = manifest.get("twitch_clip_id")
    offset = manifest.get("duration_offset", 0)
    if native:
        clip = native_clip(client, broadcaster, manifest)
        offset = clip["vod_offset"]
        source, integrity = get_native_asset(state, job, manifest, client, broadcaster)
        expected = grant.get("source_sha256") or integrity
    else:
        source = safe_file(state.root / "inbox", manifest.get("source"), MEDIA_TYPES)
        expected = grant["source_sha256"]
        if not source.is_file():
            raise Blocked("authorized source file has not arrived")
    if source.stat().st_size > 2 * GIB:
        raise Blocked("source exceeds 2 GiB")
    source_digest = sha256(source)
    if source_digest != expected:
        raise Rejected("source file does not match the licensed SHA-256")
    if type(offset) not in (float, int) or not math.isfinite(offset) or offset < 0:
        raise Rejected("VOD offset must be nonnegative seconds")
    source_start = timestamp(manifest["source_started_at"]) if manifest.get("source_started_at") else None
    if source_start is None and video.get("stream_id"):
        with state.connect() as db:
            row = db.execute("SELECT started FROM streams WHERE id=? AND creator=?",
                             (video["stream_id"], manifest["creator"])).fetchone()
        if row:
            source_start = timestamp(row["started"]) + timedelta(seconds=offset)
    chat = manifest.get("chat")
    if chat is None:
        chat = aligned_chat(state, video.get("stream_id"), source_start)
    return source, source_digest, offset, chat


def get_native_asset(state, job, manifest, client, broadcaster):
    exports = state.root / "twitch-exports"
    exports.mkdir(exist_ok=True)
    with state.connect() as db:
        record = db.execute("SELECT filename,sha256 FROM native_assets WHERE job_digest=?", (job["digest"],)).fetchone()
    if record:
        saved = safe_file(exports, record["filename"], {".mp4"})
        if saved.is_file():
            return saved, record["sha256"]

    def guard():
        authorize(state.root, manifest)
        return True

    source = exports / (secrets.token_hex(16) + ".mp4")
    client.download_clip(manifest["twitch_clip_id"], broadcaster, source, guard=guard, max_bytes=150 * 1024 ** 2)
    digest = sha256(source)
    with state.connect() as db:
        db.execute("INSERT OR REPLACE INTO native_assets VALUES (?,?,?)", (job["digest"], source.name, digest))
    return source, digest


def is_duplicate(db, manifest, candidate, job_id, compare):
    items = db.execute("SELECT id,manifest,candidate FROM jobs WHERE candidate IS NOT NULL AND id<>? "
                       "AND state IN ('campaign_hold','ready','uploading','private','uncertain','published')", (job_id,))
    for item in items:
        prior = json.loads(item["manifest"])
        cut = json.loads(item["candidate"])
        if prior.get("twitch_video_id") == manifest["twitch_video_id"]:
            offset = candidate.get("source_offset", manifest.get("duration_offset", 0))
            prior_offset = cut.get("source_offset", prior.get("duration_offset", 0))
            start, end = candidate["start"] + offset, candidate["end"] + offset
            prior_start, prior_end = cut["start"] + prior_offset, cut["end"] + prior_offset
            if min(end, prior_end) - max(start, prior_start) >= 3:
                return True
        if compare(candidate["fingerprint"], cut["fingerprint"]):
            return True
    return False


def prepare(state, job):
    from engine_media import MediaError, MediaRejected, analyze, near_duplicate, produce

    began = time.monotonic()
    job_id = job["id"]
    manifest = json.loads(job["manifest"])
    try:
        grant = authorize(state.root, manifest)
        if shutil.disk_usage(state.root).free < 3 * GIB:
            raise Blocked("available scratch space is below 3 GiB")
        source, source_digest, offset, chat = get_source(state, job, manifest, grant)
        inbox = state.root / "inbox"
        observed_at = timestamp(manifest.get("captured_at"))
        if observed_at > now() + timedelta(minutes=2):
            raise Rejected("source capture time is in the future")
        windows = manifest.get("windows", [])
        if not isinstance(windows, list) or len(windows) > 30 or any(not isinstance(w, dict) for w in windows):
            raise Rejected("source must provide an array of up to 30 editorial windows")
        config = settings(state.root)
        candidates = analyze(source, chat=chat, observed_at=observed_at,
                             weights=config.get("weights"), windows=windows, top=max(8, len(windows)),
                             minimum=MIN_SECONDS, maximum=MAX_SECONDS)
        state.set("latest_detection", {"job": job_id, "elapsed_sec": round(time.monotonic() - began, 3),
                                      "candidates_70_plus": [{key: candidate[key] for key in
                                                              ("start", "end", "score", "window_index")}
                                                             for candidate in candidates if candidate["score"] >= 70]})
        for candidate in candidates:
            index = candidate["window_index"]
            if index is None or candidate["score"] < config["threshold"]:
                continue
            candidate["source_offset"] = offset
            editorial = dict(windows[index].get("editorial") or {})
            try:
                editorial_policy(editorial, manifest.get("campaign_id"), state.root)
            except Rejected as error:
                log_event("editorial_rejected", job=job_id, window=index, reason=str(error))
                continue
            with state.connect() as db:
                if is_duplicate(db, manifest, candidate, job_id, near_duplicate):
                    continue
            if editorial.get("narration_audio"):
                audio = safe_file(inbox, editorial["narration_audio"], {".wav", ".mp3", ".m4a", ".flac"})
                digest = check_hash(editorial.get("narration_sha256"))
                if not audio.is_file() or audio.stat().st_size > 64 * 1024 ** 2 or sha256(audio) != digest:
                    raise Rejected("narration audio differs from the verified file or exceeds 64 MiB")
                editorial["narration_audio"] = str(audio)
            grant = authorize(state.root, manifest)
            messages = cleared_viewer_messages(state.root, grant, editorial, candidate["start"], candidate["end"])
            editorial["viewer_messages"] = messages
            editorial["paid_campaign"] = bool(manifest.get("campaign_id"))
            source_url = f"https://www.twitch.tv/videos/{manifest['twitch_video_id']}?t={int(offset + candidate['start'])}s"
            output = state.root / "edits" / f"{job_id}.mp4"
            output.parent.mkdir(exist_ok=True)
            try:
                metadata = produce(source, candidate, editorial, output, creator=manifest["creator"], source_url=source_url,
                                   minimum=MIN_SECONDS, maximum=MAX_SECONDS)
            except MediaRejected as error:
                log_event("render_rejected", job=job_id, window=index, reason=str(error))
                continue
            if banned_in(f"{metadata['title']} {metadata['description']}"):
                raise Rejected("upload copy contains rejected topic terms")
            if not MIN_SECONDS <= metadata["duration"] <= MAX_SECONDS:
                continue
            if manifest.get("campaign_id") and metadata.get("paid_campaign_disclosure_shown") is not True:
                log_event("campaign_disclosure_missing", job=job_id, window=index)
                continue
            if metadata.get("viewer_messages_shown", 0) != len(messages):
                log_event("chat_overlay_rejected", job=job_id, window=index)
                continue
            metadata["viewer_messages_included"] = len(messages)
            if sha256(source) != source_digest:
                raise Rejected("source file changed during analysis or rendering")
            metadata["source_sha256"] = source_digest
            add_upload_hashtags(metadata, manifest["creator"], manifest.get("campaign_id"), editorial.get("clip_category"))
            for key in ("hook_style", "clip_category", "narrative_structure", "commentary_format"):
                if isinstance(editorial.get(key), str):
                    metadata[key] = editorial[key][:80]
            metadata["latency_seconds"] = round(time.monotonic() - began, 3)
            output_digest = sha256(output)
            output.with_suffix(".json").write_text(json_text(metadata), encoding="utf-8")
            with state.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT digest FROM jobs WHERE id=?", (job_id,)).fetchone()
                if current is None or current["digest"] != job["digest"]:
                    return
                if is_duplicate(db, manifest, candidate, job_id, near_duplicate):
                    continue
                stage = "campaign_hold" if manifest.get("campaign_id") else "ready"
                reason = "awaiting a supported Content Rewards submission path" if stage == "campaign_hold" else None
                state.update(db, job_id, stage, reason, candidate=json_text(candidate), output_digest=output_digest,
                             metadata=json_text(metadata), attempts=0)
            log_event("edit_ready", job=job_id, score=candidate["score"], seconds=metadata["latency_seconds"], stage=stage)
            return
        raise Rejected("no publishable, nonduplicative editorial window reached the score threshold")
    except Blocked as error:
        state.retry(job, "blocked", str(error), delay=60)
    except TwitchError as error:
        state.retry(job, "blocked", "Twitch source verification unavailable: " + str(error)[:160],
                    delay=error.retry_after or 60)
    except (Rejected, MediaRejected, ValueError, TypeError, KeyError) as error:
        with state.connect() as db:
            state.update(db, job_id, "rejected", str(error)[:240])
    except (MediaError, OSError, SystemExit) as error:
        state.retry(job, "queued", "media tooling or source is unavailable: " + type(error).__name__)


def reserve(state, job, config):
    # Rolling 24-hour limit keeps resets and local clock changes from allowing a burst.
    with state.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cutoff = (now() - timedelta(hours=24)).isoformat()
        used = db.execute("""SELECT COUNT(*) FROM jobs WHERE id<>? AND
                           ((state='published' AND published>=?) OR
                            (state IN ('uploading','private','uncertain') AND reserved_day>=?))""",
                          (job["id"], cutoff, cutoff)).fetchone()[0]
        if used >= config["daily_limit"]:
            raise Blocked("rolling 24-hour publishing limit reached")
        state.update(db, job["id"], job["state"], reserved_day=now().isoformat())


def publish_jobs(state, youtube):
    from engine_youtube import YouTubeError, YouTubeRetryable, YouTubeUploadUncertain

    jobs = state.due(("ready", "uploading", "private"), 40)
    ordering = {"private": 0, "uploading": 1, "ready": 2}
    jobs.sort(key=lambda job: (ordering[job["state"]], -json.loads(job["candidate"])["score"]))
    for job in jobs:
        job_id = job["id"]
        manifest = json.loads(job["manifest"])
        metadata = json.loads(job["metadata"])
        candidate = json.loads(job["candidate"])
        stage = job["state"]
        session, video_id = job["session_url"], job["video_id"]
        try:
            def guard():
                publish_grant(state, job, manifest)
                return True

            guard()
            if manifest.get("twitch_clip_id"):
                client, broadcaster, _ = official_video(manifest)
                native_clip(client, broadcaster, manifest)
            if manifest.get("publish_at") and now() < timestamp(manifest["publish_at"]):
                continue
            config = settings(state.root)
            if candidate["score"] < config["threshold"]:
                raise Blocked("candidate score is below the active publication threshold")
            if not MIN_SECONDS <= metadata.get("duration", 0) <= MAX_SECONDS:
                raise Blocked("only clips of 7–30 seconds may be published")
            output = state.root / "edits" / f"{job_id}.mp4"
            if not output.is_file() or sha256(output) != job["output_digest"]:
                raise Rejected("prepared output is missing or was modified")
            if video_id:
                video = youtube.video(video_id)
                if video.get("status", {}).get("privacyStatus") == "public":
                    published = video.get("snippet", {}).get("publishedAt") or now().isoformat()
                    with state.connect() as db:
                        state.update(db, job_id, "published", published=published, session_url=None)
                    continue
            reserve(state, job, config)
            if not session and not video_id:
                add_upload_hashtags(metadata, manifest["creator"], None, metadata.get("clip_category"))
                if hasattr(youtube, "recent_uploads") and any(
                    post["title"].casefold().strip() == metadata["title"].casefold().strip()
                    for post in youtube.recent_uploads(limit=50)
                ):
                    with state.connect() as db:
                        state.update(db, job_id, "rejected", "an existing channel upload uses this title")
                    continue
                session = youtube.start_upload(output, title=metadata["title"], description=metadata["description"],
                                               tags=metadata.get("tags", ()),
                                               contains_synthetic_media=manifest.get("realistic_synthetic_media") is True,
                                               guard=guard)
                stage = "uploading"
                with state.connect() as db:
                    state.update(db, job_id, stage, session_url=session)
            if not video_id:
                video_id = youtube.resume_upload(session, output, guard=guard)
                stage = "private"
                with state.connect() as db:
                    state.update(db, job_id, stage, video_id=video_id, session_url=None, attempts=0)
            video = youtube.publish(video_id, paid_promotion=False, guard=guard)
            published = video.get("snippet", {}).get("publishedAt") or now().isoformat()
            with state.connect() as db:
                state.update(db, job_id, "published", published=published, attempts=0)
            log_event("published", job=job_id, video_id=video_id)
        except Blocked as error:
            state.retry(job, stage, str(error), delay=60)
        except YouTubeUploadUncertain:
            with state.connect() as db:
                state.update(db, job_id, "uncertain", "YouTube upload outcome must be reconciled before retry")
        except YouTubeRetryable as error:
            state.retry(job, stage, type(error).__name__, delay=error.retry_after)
        except TwitchError as error:
            state.retry(job, stage, "Twitch source verification unavailable", delay=error.retry_after or 120)
        except (Rejected, YouTubeError, ValueError) as error:
            state.retry(job, stage, str(error)[:240], delay=300)


def track_streams(state, client, creators, eventsub, stop, once=False):
    poll_after = 0
    until_refresh = time.monotonic() + 1800
    with ThreadPoolExecutor(max_workers=5) as requests:
        while not stop.is_set() and time.monotonic() < until_refresh:
            poll_after = stream_tick(state, client, creators, eventsub, stop, poll_after, requests, once)
            if once:
                return


def store_chat_signal(db, eventsub, login, stream_id, started):
    if eventsub:
        since = timestamp(started).timestamp()
        samples = eventsub.chat_timeline(login)
        db.executemany("INSERT OR REPLACE INTO chat_signal VALUES (?,?,?,?)",
                       ((stream_id, sample["at"], sample["count"], sample["reactions"])
                        for sample in samples if sample["at"] >= since))


def stream_tick(state, client, creators, eventsub, stop, poll_after, requests, once):
    try:
        streams = client.get_streams(creators)
        observed = now()
        active = {login for login, stream in streams.items() if gta_vi_eligibility(stream)}
        if eventsub:
            eventsub.set_eligible_chat(active)
        with state.connect() as db:
            for login in creators:
                stream = streams.get(login)
                if not stream:
                    prior = db.execute("SELECT id,started FROM streams WHERE creator=? AND ended IS NULL",
                                       (login,)).fetchone()
                    if prior:
                        store_chat_signal(db, eventsub, login, prior["id"], prior["started"])
                db.execute("UPDATE streams SET ended=? WHERE creator=? AND ended IS NULL AND id<>?",
                           (observed.isoformat(), login, stream['id'] if stream else ""))
                if not stream:
                    continue
                data = {key: stream.get(key) for key in ("title", "game_name", "game_id", "viewer_count")}
                seconds = (observed - timestamp(stream["started_at"])).total_seconds()
                chat = eventsub.chat_snapshot(login) if eventsub else None
                data.update(duration_seconds=max(0, int(seconds)), keywords=stream_keywords(stream), chat=chat)
                db.execute("""INSERT INTO streams VALUES (?,?,?,?,NULL,?,?) ON CONFLICT(id) DO UPDATE SET
                            last_seen=excluded.last_seen,eligible=excluded.eligible,data=excluded.data,ended=NULL""",
                           (stream["id"], login, stream["started_at"], observed.isoformat(),
                            int(login in active), json_text(data)))
                if eventsub and login in active:
                    store_chat_signal(db, eventsub, login, stream["id"], stream["started_at"])
                if login in active and chat and chat["messages"] >= 8 and chat["ratio"] >= 3:
                    key = f"chat:{stream['id']}:{int(max(0, seconds) // 45)}"
                    candidate = {"signal": "chat_spike", "creator": login, "stream_id": stream["id"],
                                 "approximate_stream_offset_seconds": int(max(0, seconds)), **chat}
                    db.execute("INSERT OR IGNORE INTO moments VALUES (?,?,?,?,?)",
                               (key, login, observed.isoformat(), 1, json_text(candidate)))
        if time.monotonic() >= poll_after:
            poll_after = time.monotonic() + 90
            game_id = client.get_gta_vi_game_id()
            if game_id:
                pending = {}
                for login in sorted(active):
                    begin = max(timestamp(streams[login]["started_at"]), observed - timedelta(minutes=5))
                    request = requests.submit(client.get_clips, creators[login], begin, observed, max_pages=2)
                    pending[request] = login
                for request in as_completed(pending):
                    login = pending[request]
                    try:
                        clips = request.result()
                    except TwitchError as error:
                        log_event("twitch_clip_error", creator=login, error=type(error).__name__)
                        continue
                    with state.connect() as db:
                        for clip in clips:
                            if clip.get("game_id") != game_id:
                                continue
                            db.execute("INSERT OR REPLACE INTO moments VALUES (?,?,?,?,?)",
                                       (clip["id"], login, observed.isoformat(), 1, json_text(clip)))
        state.set("twitch", {"state": "monitoring", "resolved_creators": creators,
                             "unresolved": sorted(CREATORS.keys() - creators.keys()),
                             "live": list(streams), "eligible_live": sorted(active), "last_check": observed.isoformat()})
        delay = 30
    except TwitchError as error:
        state.set("twitch", {"state": "backoff", "error": str(error)[:240]})
        delay = max(30, error.retry_after or 0)
    if once:
        return poll_after
    until = time.monotonic() + delay
    while not stop.is_set() and time.monotonic() < until:
        if eventsub and eventsub.changed.wait(min(1, until - time.monotonic())):
            eventsub.changed.clear()
            if delay == 30:
                break
        elif not eventsub:
            stop.wait(min(1, max(0, until - time.monotonic())))
    return poll_after


def monitor(state, stop, once):
    while not stop.is_set():
        eventsub = None
        try:
            prune_twitch(state)
            client = TwitchClient.from_env()
            creators = client.resolve_creators()
            if not once:
                eventsub = EventSubMonitor(client, creators)
                eventsub.start()
            track_streams(state, client, creators, eventsub, stop, once)
        except TwitchError as error:
            state.set("twitch", {"state": "unavailable", "error": str(error)[:240]})
        finally:
            if eventsub:
                eventsub.stop()
        if once:
            return
        stop.wait(60)


def prune_twitch(state):
    cutoff = (now() - timedelta(hours=23)).isoformat()
    with state.connect() as db:
        db.execute("DELETE FROM streams WHERE last_seen<?", (cutoff,))
        db.execute("DELETE FROM moments WHERE last_seen<?", (cutoff,))
        db.execute("DELETE FROM chat_signal WHERE at<?", (int(timestamp(cutoff).timestamp()),))


def youtube_tick(state, youtube):
    from engine_youtube import YouTubeAuthRequired, YouTubeError

    with state.connect() as db:
        has_jobs = db.execute("SELECT 1 FROM jobs WHERE state IN ('ready','uploading','private','published') LIMIT 1").fetchone()
    try:
        channel = youtube.channel()
        state.set("youtube", {"state": "connected", "channel_id": CHANNEL_ID,
                              "subscriber_count": channel.get("statistics", {}).get("subscriberCount")})
        if has_jobs:
            publish_jobs(state, youtube)
            metrics.capture(state, youtube)
    except YouTubeAuthRequired as error:
        with state.connect() as db:
            db.execute("DELETE FROM snapshots")
        state.set("youtube", {"state": "unavailable", "error": str(error)[:240]})
    except YouTubeError as error:
        state.set("youtube", {"state": "unavailable", "error": str(error)[:240]})


def run(state, once=False):
    from engine_youtube import YouTube

    stop = threading.Event()
    with state.exclusive_run(), ThreadPoolExecutor(max_workers=2) as pool:
        (state.root / "inbox").mkdir(exist_ok=True)
        (state.root / "evidence").mkdir(exist_ok=True)
        youtube = YouTube()
        monitor_thread = threading.Thread(target=monitor, args=(state, stop, once), name="twitch-monitor", daemon=True)
        monitor_thread.start()
        active = {}
        log_event("service_started", once=once)
        next_youtube = 0
        try:
            while not stop.is_set():
                state.set("heartbeat", {"at": now().isoformat(), "pid": os.getpid()})
                feed(state, active)
                for job_id, future in list(active.items()):
                    if future.done():
                        active.pop(job_id)
                        future.result()
                for job in state.due(("queued", "blocked")):
                    if len(active) >= 2:
                        break
                    if job["id"] not in active:
                        active[job["id"]] = pool.submit(prepare, state, job)
                if time.monotonic() >= next_youtube or once:
                    try:
                        youtube_tick(state, youtube)
                    except OSError as error:
                        state.set("youtube", {"state": "unavailable", "error": type(error).__name__})
                    next_youtube = time.monotonic() + 45
                if once:
                    for future in active.values():
                        future.result()
                    youtube_tick(state, youtube)
                    monitor_thread.join()
                    return
                stop.wait(5)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            monitor_thread.join(10)
            state.set("heartbeat", {"stopped_at": now().isoformat()})
            log_event("service_stopped")


def status(state):
    with state.connect() as db:
        services = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM service")}
        jobs = [dict(row) for row in db.execute("SELECT id,state,reason,video_id,published FROM jobs ORDER BY updated DESC LIMIT 20")]
        streams = [dict(row) for row in db.execute("SELECT creator,eligible,started,last_seen,data FROM streams WHERE ended IS NULL")]
        moments = [json.loads(row[0]) for row in db.execute("SELECT data FROM moments ORDER BY last_seen DESC LIMIT 12")]
    for stream in streams:
        stream["data"] = json.loads(stream["data"])
    return {"channel_id": CHANNEL_ID, "creators": CREATORS,
            "publishing": "paused" if (state.root / "STOP_UPLOADS").exists() else "unattended",
            "service": services, "streams": streams, "recent_twitch_moments": moments, "jobs": jobs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run").add_argument("--once", action="store_true")
    for command in ("status", "kill", "resume", "youtube-revoke", "report"):
        commands.add_parser(command)
    consent = commands.add_parser("youtube-auth")
    consent.add_argument("--accept-privacy", action="store_true",
                         help="accept the API privacy policy in docs/shorts-engine.md")
    record = commands.add_parser("record-revenue")
    record.add_argument("kind", choices=metrics.MONEY)
    record.add_argument("reference")
    record.add_argument("amount")
    record.add_argument("evidence")
    args = parser.parse_args()
    state = State(args.state)
    if args.command == "run":
        LOG.setLevel(logging.INFO)
        LOG.addHandler(RotatingFileHandler(state.root / "engine.log", maxBytes=2 * 1024 ** 2, backupCount=3,
                                           encoding="utf-8"))
        run(state, args.once)
    elif args.command == "status":
        print(json.dumps(status(state), indent=2, ensure_ascii=False))
    elif args.command == "report":
        print(json.dumps(metrics.report(state), indent=2, ensure_ascii=False))
    elif args.command == "record-revenue":
        metrics.record_money(state, args.kind, args.reference, args.amount, args.evidence)
    elif args.command == "youtube-auth":
        from engine_youtube import YouTube, YouTubeError

        if not args.accept_privacy:
            parser.exit(1, "Read the YouTube API privacy policy in docs/shorts-engine.md and run youtube-auth --accept-privacy.\n")
        try:
            youtube = YouTube()
            youtube.authorize()
            youtube.channel()
            print(f"Connected to https://www.youtube.com/channel/{CHANNEL_ID}")
        except YouTubeError as error:
            parser.exit(1, f"{error}\n")
    elif args.command == "youtube-revoke":
        from engine_youtube import YouTube, YouTubeError

        (state.root / "STOP_UPLOADS").touch()
        try:
            YouTube().revoke()
        except YouTubeError as error:
            parser.exit(1, f"{error}\n")
        with state.connect() as db:
            db.execute("DELETE FROM snapshots")
            db.execute("DELETE FROM service WHERE key='youtube'")
            db.execute("UPDATE jobs SET session_url=NULL,state='uncertain',reason='YouTube access revoked' "
                       "WHERE session_url IS NOT NULL")
        print("YouTube access revoked and local API snapshots deleted")
    elif args.command == "kill":
        (state.root / "STOP_UPLOADS").touch()
        print("Future service uploads and promotions are paused")
    elif args.command == "resume":
        (state.root / "STOP_UPLOADS").unlink(missing_ok=True)
        print("Unattended service publishing is enabled")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
