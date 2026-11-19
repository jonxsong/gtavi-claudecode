"""Official Twitch metadata and optional EventSub for the Shorts engine."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


CREATORS = {
    "ishowspeed": "IShowSpeed",
    "kaicenat": "Kai Cenat",
    "jasontheween": "JasonTheWeen",
    "moistcr1tikal": "moistcr1tikal / penguinz0",
    "xqc": "xQc",
}
HELIX = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2"
EVENTSUB = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
LOG = logging.getLogger(__name__)
CLIP_DOWNLOAD_SCOPES = {"editor:manage:clips", "channel:manage:clips"}
CLIP_DOWNLOAD_TYPES = {"video/mp4", "application/mp4", "application/octet-stream", "binary/octet-stream"}
CLIP_DOWNLOAD_CHUNK = 256 * 1024
CHAT_BUFFER_SECONDS = 135
GTA = re.compile(r"\b(?:gta|grand[\s-]+theft[\s-]+auto)[\s-]*(?:vi|6)\b", re.I)
GTA_CATEGORY = re.compile(r"(?:grand[\s-]+theft[\s-]+auto|gta)[\s-]*(?:vi|6)", re.I)
REACTION = re.compile(r"\b(?:reactions?|react(?:s|ing|ed)?|trailers?|breakdown|reveal|announcements?|news|analysis|discuss(?:ion|ing)?|review(?:ing)?|first\s+(?:look|impressions))\b", re.I)
SCHEDULED = re.compile(r"\b(?:later|tomorrow|countdown|waiting\s+for|after\s+this|before\s+gta|in\s+\d+\s*(?:hours?|mins?|minutes?))\b", re.I)
UNSUITABLE = re.compile(r"\b(?:leak(?:s|ed|ing)?|pirat(?:e|ed|ing)|cracked|play\s+early|early[\s-]+access|dev\s*build)\b", re.I)
# Same lightweight chat cues used by the repository's live-mode, with no stored chat text.
HYPE = re.compile(r"\b(?:lmao|kekw|lol|clip|holy|no way|w+|pog(?:gers)?)\b|[💀😂🔥]", re.I)
NEGATIVE = re.compile(r"\b(?:nooo+|sad|fail(?:ed)?|scary|terrible|rip|yikes)\b|[😢😭]", re.I)
NOISE = re.compile(r"\b(?:giveaway|raiding|brb|ad break|sponsor|promo code)\b|discord\.gg|https?://", re.I)


class TwitchError(RuntimeError):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class TwitchConfigurationError(TwitchError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _credential_path():
    appdata = os.getenv("LOCALAPPDATA")
    if os.name != "nt" or not appdata:
        return None
    return Path(appdata) / "gtavi-claudecode" / "twitch-oauth.dpapi"


def _protect_credentials(data, *, protect):
    if __package__:
        from .engine_youtube import YouTubeAuthRequired, _dpapi
    else:
        from engine_youtube import YouTubeAuthRequired, _dpapi

    try:
        return _dpapi(data, protect=protect)
    except YouTubeAuthRequired:
        raise TwitchConfigurationError("Windows could not access the Twitch credentials for this user") from None


def _read_credentials():
    path = _credential_path()
    if path is None:
        return {}
    try:
        saved = json.loads(_protect_credentials(path.read_bytes(), protect=False))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise TwitchConfigurationError("Stored Twitch credentials are unreadable. Run Twitch configure again") from None
    if not isinstance(saved, dict) or any(not isinstance(saved.get(key), str) or not saved[key]
                                         for key in ("client_id", "client_secret")):
        raise TwitchConfigurationError("Stored Twitch credentials are invalid. Run Twitch configure again")
    return saved


def _save_credentials(credentials):
    path = _credential_path()
    if path is None:
        raise TwitchConfigurationError("Twitch credential storage requires Windows and LOCALAPPDATA")
    protected = _protect_credentials(json.dumps(credentials).encode("utf-8"), protect=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        temporary_path.write_bytes(protected)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _normalize(value):
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def is_gta_vi_category(value):
    return bool(GTA_CATEGORY.fullmatch(_normalize(value)))


def gta_vi_eligibility(stream):
    """A title mention alone cannot make unrelated gameplay or a countdown eligible."""
    category = _normalize(stream.get("game_name"))
    title = _normalize(stream.get("title"))
    if UNSUITABLE.search(title):
        return False
    if is_gta_vi_category(category):
        return True
    if category.lower() != "just chatting" or SCHEDULED.search(title):
        return False
    lead = re.split(r"[|/•]", title, maxsplit=1)[0]
    return bool(GTA.search(lead) and REACTION.search(lead))


def stream_keywords(stream):
    matches = set()
    for value in (stream.get("game_name"), stream.get("title"), *(stream.get("tags") or [])):
        normalized = _normalize(value)
        if GTA.search(normalized):
            matches.add("gta vi")
        matches.update(match.group().lower() for match in REACTION.finditer(normalized))
    return sorted(matches) if "gta vi" in matches else []


def _rate_delay(headers):
    values = []
    for name, origin in (("Ratelimit-Reset", time.time()), ("Retry-After", 0)):
        try:
            values.append(int(headers.get(name, "")) - origin)
        except (ValueError, TypeError):
            continue
    return max([1.0, *values])


def _utc_datetime(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Clip query timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _chat_second(value, received):
    if isinstance(value, str):
        try:
            return int(_utc_datetime(value).timestamp())
        except ValueError:
            pass
    return received


def _rfc3339(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _rows(response):
    if not isinstance(response.get("data"), list) or any(not isinstance(row, dict) for row in response["data"]):
        raise TwitchError("Twitch returned an invalid data response")
    return response["data"]


def _clip_fields(row):
    fields = ("id", "url", "broadcaster_id", "broadcaster_name", "video_id", "game_id", "language", "title",
              "view_count", "created_at", "duration", "vod_offset", "is_featured")
    return {field: row.get(field) for field in fields}


def _download_guard(guard):
    if guard() is False:
        raise TwitchConfigurationError("Twitch clip download permission is inactive")


def _valid_clip_download_url(url):
    if not isinstance(url, str) or not url or not url.isascii() or re.search(r"[\x00-\x20\\]", url):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.fragment
                and parsed.port in (None, 443) and host.endswith(".clips.twitchcdn.net"))
    except (TypeError, ValueError):
        return False


def _download_length(response, max_bytes):
    if response.status != 200 or response.headers.get("Content-Range"):
        raise TwitchError("Twitch clip response was not a complete download")
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    content_encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
    if content_type not in CLIP_DOWNLOAD_TYPES or content_encoding != "identity":
        raise TwitchError("Twitch clip response was not MP4 media")
    raw_length = response.headers.get("Content-Length")
    if raw_length is None:
        return None
    if len(raw_length) > 20 or not raw_length.isascii() or not raw_length.isdecimal():
        raise TwitchError("Twitch clip response has an invalid download length")
    length = int(raw_length)
    if not 16 <= length <= max_bytes:
        raise TwitchError("Twitch clip size is outside the download limit")
    return length


def _write_mp4(response, file, expected, max_bytes, deadline, guard):
    prefix = bytearray()
    received = 0
    while True:
        _download_guard(guard)
        if time.monotonic() >= deadline:
            raise TwitchError("Twitch clip download timed out")
        try:
            chunk = response.read1(CLIP_DOWNLOAD_CHUNK)
        except (OSError, HTTPException, ValueError):
            raise TwitchError("Twitch clip transfer was interrupted") from None
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes or (expected is not None and received > expected):
            raise TwitchError("Twitch clip exceeded the declared or maximum size")
        if len(prefix) < 16:
            prefix.extend(chunk[:16 - len(prefix)])
            if len(prefix) == 16 and (prefix[4:8] != b"ftyp" or int.from_bytes(prefix[:4], "big") < 16):
                raise TwitchError("Twitch clip response did not contain an MP4 header")
        file.write(chunk)
    if received < 16 or (expected is not None and received != expected):
        raise TwitchError("Twitch clip response was truncated")


class TwitchClient:
    def __init__(self, client_id, client_secret=None, app_token=None, *, eventsub_token=None, chat_logins=(),
                 clip_editor_token=None):
        if not client_id or not (client_secret or app_token):
            raise TwitchConfigurationError("Run scripts/engine_twitch.py configure or set TWITCH_CLIENT_ID and "
                                           "TWITCH_CLIENT_SECRET in this process")
        self.client_id = client_id
        self.eventsub_token = eventsub_token
        self.chat_logins = chat_logins
        self._clip_editor_token = clip_editor_token
        self._secret = client_secret
        self._app_token = app_token
        self._expires = 0.0
        self._validated = 0.0
        self._token_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._retry_at = {}
        self._creator_ids = {}
        self._game_id = None
        self._opener = build_opener(NoRedirect())

    @classmethod
    def from_env(cls):
        client_id, secret, token = (os.getenv(name) for name in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_TOKEN"))
        eventsub = os.getenv("TWITCH_EVENTSUB_USER_TOKEN")
        clip_editor = os.getenv("TWITCH_CLIP_EDITOR_USER_TOKEN")
        if client_id and (secret or token):
            return cls(client_id, secret, token, eventsub_token=eventsub, clip_editor_token=clip_editor)
        saved = _read_credentials()
        return cls(saved.get("client_id"), saved.get("client_secret"),
                   eventsub_token=eventsub or saved.get("eventsub_token"), chat_logins=saved.get("chat_logins", ()),
                   clip_editor_token=clip_editor or saved.get("clip_editor_token"))

    def _request(self, url, *, headers=None, data=None, bucket="oauth", attempts=3, timeout=10):
        label = urlsplit(url).path
        for attempt in range(attempts):
            with self._rate_lock:
                wait = self._retry_at.get(bucket, 0.0) - time.monotonic()
            if wait > 3 or (wait > 0 and attempts == 1):
                raise TwitchError(f"Twitch API rate limit deferred {label}", 429, wait)
            if wait > 0:
                time.sleep(wait)
            try:
                with self._opener.open(Request(url, headers=headers or {}, data=data), timeout=timeout) as response:
                    if response.headers.get("Ratelimit-Remaining") == "0":
                        with self._rate_lock:
                            self._retry_at[bucket] = time.monotonic() + _rate_delay(response.headers)
                    result = json.load(response)
                    if not isinstance(result, dict):
                        raise TwitchError(f"Twitch returned an invalid response for {label}")
                    return result
            except HTTPError as error:
                status = error.code
                delay = _rate_delay(error.headers) if status == 429 else None
                error.close()
                if delay is not None:
                    with self._rate_lock:
                        self._retry_at[bucket] = time.monotonic() + delay
                failure = TwitchError(f"Twitch returned HTTP {status} for {label}", status, delay)
                if (status < 500 and status != 429) or (delay is not None and delay > 3):
                    raise failure from None
            except (URLError, OSError, UnicodeError, ValueError):
                failure = TwitchError(f"Twitch request failed for {label}")
            if attempt + 1 < attempts:
                time.sleep(min(4, 2 ** attempt) + random.uniform(0, 0.3))
        raise failure from None

    def _validate(self, token, **kwargs):
        info = self._request(f"{OAUTH}/validate", headers={"Authorization": f"OAuth {token}"}, **kwargs)
        if info.get("client_id") != self.client_id:
            raise TwitchConfigurationError("Twitch token belongs to a different application")
        return info

    def _new_app_token(self):
        if not self._secret:
            raise TwitchConfigurationError("TWITCH_TOKEN expired or was rejected. Supply a fresh token or TWITCH_CLIENT_SECRET")
        data = urlencode({"client_id": self.client_id, "client_secret": self._secret,
                          "grant_type": "client_credentials"}).encode("ascii")
        result = self._request(f"{OAUTH}/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, data=data)
        if not isinstance(result.get("access_token"), str) or not result["access_token"]:
            raise TwitchError("Twitch did not issue an app access token")
        return result["access_token"]

    def _token(self):
        with self._token_lock:
            now = time.monotonic()
            if self._validated and now < min(self._validated, self._expires):
                return self._app_token
            token = self._app_token if not self._expires or now < self._expires else None
            if token:
                try:
                    info = self._validate(token)
                except TwitchError as error:
                    if error.status != 401:
                        raise
                    token = None
            if not token:
                token = self._new_app_token()
                info = self._validate(token)
            now = time.monotonic()
            self._app_token = token
            self._expires = now + max(0, int(info["expires_in"]) - 45)
            self._validated = now + 55 * 60
            return token

    def _helix(self, path, query=(), *, token=None, data=None, attempts=3, timeout=10):
        url = f"{HELIX}/{path}"
        if query:
            url += f"?{urlencode(query)}"
        auth = token or self._token()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {auth}", "Client-Id": self.client_id}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                return self._request(url, headers=headers, data=data, bucket="user" if token else "app",
                                     attempts=attempts, timeout=timeout)
            except TwitchError as error:
                if token or attempt or error.status != 401:
                    raise
                with self._token_lock:
                    if self._app_token == auth:
                        self._app_token = None
                        self._validated = 0.0
                auth = self._token()

    def resolve_creators(self):
        rows = _rows(self._helix("users", [("login", login) for login in CREATORS]))
        resolved = {row["login"]: row["id"] for row in rows
                    if row.get("login") in CREATORS and isinstance(row.get("id"), str) and row["id"].isdigit()}
        if not resolved:
            raise TwitchError("Twitch did not resolve any whitelisted channels")
        self._creator_ids = resolved
        return resolved.copy()

    def get_streams(self, creators):
        eligible = {login: user_id for login, user_id in creators.items()
                    if login in CREATORS and self._creator_ids.get(login) == user_id}
        if not eligible:
            return {}
        query = [("user_id", user_id) for user_id in eligible.values()]
        rows = _rows(self._helix("streams", query))
        return {row["user_login"]: row for row in rows if row.get("type") == "live"
                and row.get("user_login") in eligible and eligible[row["user_login"]] == row.get("user_id")}

    def get_gta_vi_game_id(self):
        if not self._game_id:
            records = _rows(self._helix("games", [("name", "Grand Theft Auto VI")]))
            self._game_id = next((row.get("id") for row in records if is_gta_vi_category(row.get("name"))), None)
        return self._game_id

    def get_clips(self, broadcaster_id, started_at, ended_at, *, max_pages=5):
        if broadcaster_id not in self._creator_ids.values():
            raise TwitchConfigurationError("Twitch clip metadata requires a resolved whitelisted broadcaster")
        start, end = _utc_datetime(started_at), _utc_datetime(ended_at)
        if start >= end:
            return []
        # Helix truncates both boundaries to a minute. Widen the query and filter exactly here.
        end_minute = end.replace(second=0, microsecond=0) + timedelta(minutes=1)
        query = [("broadcaster_id", broadcaster_id), ("started_at", _rfc3339(start)),
                 ("ended_at", _rfc3339(end_minute)), ("first", 100)]
        clips, cursors = {}, set()
        after = []
        for _ in range(max_pages):
            response = self._helix("clips", [*query, *after])
            for row in _rows(response):
                if row.get("broadcaster_id") != broadcaster_id or not row.get("id") or not row.get("created_at"):
                    continue
                if start <= _utc_datetime(row["created_at"]) <= end:
                    clips[row["id"]] = _clip_fields(row)
            cursor = (response.get("pagination") or {}).get("cursor")
            if not cursor or cursor in cursors:
                break
            cursors.add(cursor)
            after = [("after", cursor)]
        return list(clips.values())

    def get_clip(self, clip_id):
        rows = _rows(self._helix("clips", [("id", clip_id)]))
        return next((_clip_fields(row) for row in rows if row.get("id") == clip_id
                     and row.get("broadcaster_id") in self._creator_ids.values()), None)

    def get_video(self, video_id):
        rows = _rows(self._helix("videos", [("id", video_id)]))
        return next((row for row in rows if row.get("id") == video_id
                     and row.get("user_login") in self._creator_ids
                     and self._creator_ids[row["user_login"]] == row.get("user_id")), None)

    def _verify_editor_token(self, token):
        if not token:
            raise TwitchConfigurationError("Configure a Twitch broadcaster or editor user access token")
        info = self._validate(token)
        editor_id = info.get("user_id")
        if not editor_id or not CLIP_DOWNLOAD_SCOPES.intersection(info.get("scopes") or ()):
            raise TwitchConfigurationError("Twitch clip downloads require editor:manage:clips or channel:manage:clips")
        return editor_id

    def get_clip_download_url(self, clip_id, broadcaster_id, user_token, *, guard=None):
        """Resolve a temporary Twitch CDN URL after the caller verifies footage rights."""
        if not user_token or broadcaster_id not in self._creator_ids.values():
            raise TwitchConfigurationError("Twitch clip downloads require editor authorization for a whitelisted channel")
        record = self.get_clip(clip_id)
        if not record or record["broadcaster_id"] != broadcaster_id:
            return None
        editor_id = self._verify_editor_token(user_token)
        query = [("editor_id", editor_id), ("broadcaster_id", broadcaster_id), ("clip_id", clip_id)]
        if guard is not None:
            _download_guard(guard)
        for row in _rows(self._helix("clips/downloads", query, token=user_token)):
            url = row.get("landscape_download_url")
            if row.get("clip_id") != clip_id or not url:
                continue
            if _valid_clip_download_url(url):
                return url
            raise TwitchError("Twitch returned an invalid clip download URL")
        return None

    def download_clip(self, clip_id, broadcaster_id, destination, *, guard, max_bytes=128 * 1024 ** 2):
        """Fetch an approved clip via Helix's signed landscape endpoint into an atomic MP4 file."""
        if not callable(guard):
            raise TwitchConfigurationError("Direct Twitch download requires a current footage permission guard")
        _download_guard(guard)
        if type(max_bytes) is not int or max_bytes < 16:
            raise TwitchConfigurationError("Twitch clip byte limit must be an integer of at least 16")
        destination = Path(destination)
        if destination.suffix.lower() != ".mp4" or destination.exists() or destination.is_symlink():
            raise TwitchConfigurationError("Twitch clip destination must be a new MP4 file")
        token = self._clip_editor_token or os.getenv("TWITCH_CLIP_EDITOR_USER_TOKEN")
        if not token:
            saved = _read_credentials()
            if saved.get("client_id") == self.client_id:
                token = saved.get("clip_editor_token")
        if not token:
            raise TwitchConfigurationError("Run Twitch configure --clip-download with a broadcaster or editor token")
        url = self.get_clip_download_url(clip_id, broadcaster_id, token, guard=guard)
        if not url:
            raise TwitchError("Twitch has no authorized landscape download for this clip")
        if not _valid_clip_download_url(url):
            raise TwitchError("Twitch returned an invalid clip download URL")
        _download_guard(guard)
        temporary_path = None
        deadline = time.monotonic() + 90
        request = Request(url, headers={"Accept": "video/mp4, application/octet-stream", "Accept-Encoding": "identity"})
        try:
            try:
                response = self._opener.open(request, timeout=8)
            except HTTPError as error:
                code = error.code
                retry_after = _rate_delay(error.headers) if code == 429 else None
                error.close()
                raise TwitchError(f"Twitch clip download returned HTTP {code}", code, retry_after) from None
            except (OSError, HTTPException, ValueError):
                raise TwitchError("Could not open the Twitch clip download") from None
            with response:
                expected = _download_length(response, max_bytes)
                _download_guard(guard)
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".twitch-", suffix=".part",
                                                 delete=False) as file:
                    temporary_path = Path(file.name)
                    _write_mp4(response, file, expected, max_bytes, deadline, guard)
            _download_guard(guard)
            if os.name == "nt":
                temporary_path.rename(destination)
            else:
                os.link(temporary_path, destination)
            return destination
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _subscribe(self, token, event_type, condition, session_id):
        body = {"type": event_type, "version": "1", "condition": condition,
                "transport": {"method": "websocket", "session_id": session_id}}
        return self._helix("eventsub/subscriptions", token=token, data=json.dumps(body).encode("utf-8"), attempts=1, timeout=5)


class EventSubMonitor:
    """Wake the monitor on live changes and aggregate explicitly enabled chat channels."""

    def __init__(self, client, creators, chat_logins=None):
        self.client = client
        self.changed = threading.Event()
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._token = os.getenv("TWITCH_EVENTSUB_USER_TOKEN") or getattr(client, "eventsub_token", None)
        self._lookup = {user_id: login for login, user_id in creators.items() if login in CREATORS}
        if chat_logins is None:
            configured = os.getenv("TWITCH_CHAT_CHANNELS")
            chat_logins = configured.split(",") if configured is not None else getattr(client, "chat_logins", ())
        requested = chat_logins
        self._chat_logins = {value.strip().lower() for value in requested} & set(self._lookup.values())
        self._chat_lock = threading.Lock()
        self._eligible_chat = set()
        self._chat_active = set()
        self._chat_since = {}
        self._chat = {}
        self._last_spike = {}
        self._message_ids = OrderedDict()
        self._disabled = set()

    def start(self):
        if not self._token or not self._lookup:
            return False
        if self._thread and self._thread.is_alive():
            return True
        try:
            from websockets.exceptions import WebSocketException
            from websockets.sync.client import connect
        except ImportError:
            LOG.warning("Install websockets to enable Twitch EventSub. Helix monitoring is available")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(connect, WebSocketException),
                                        name="twitch-eventsub", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def set_eligible_chat(self, logins):
        eligible = set(logins) & self._chat_logins
        with self._chat_lock:
            for login in self._eligible_chat - eligible:
                self._chat_since.pop(login, None)
                self._last_spike.pop(login, None)
            for login in eligible - self._eligible_chat:
                self._chat_since[login] = time.time()
            self._eligible_chat = eligible

    def _set_active_chat(self, active):
        with self._chat_lock:
            for login in self._chat_active - active:
                self._chat.pop(login, None)
                self._chat_since.pop(login, None)
                self._last_spike.pop(login, None)
            for login in active - self._chat_active:
                self._chat_since[login] = time.time()
            self._chat_active = active

    def _handle_message(self, metadata, payload):
        event_type = metadata.get("message_type")
        subscription = payload.get("subscription") or {}
        kind = subscription.get("type")
        if event_type == "revocation":
            user_id = (subscription.get("condition") or {}).get("broadcaster_user_id")
            login = self._lookup.get(user_id)
            self._disabled.add((kind, user_id))
            if kind == "channel.chat.message" and login:
                self._set_active_chat(self._chat_active - {login})
            LOG.warning("Twitch revoked %s for %s (%s)", kind, login, subscription.get("status"))
            self.changed.set()
            return
        if event_type != "notification":
            return
        event = payload.get("event") or {}
        user_id = event.get("broadcaster_user_id")
        login = self._lookup.get(user_id)
        if not login:
            return
        if kind in ("stream.online", "stream.offline"):
            if kind == "stream.offline":
                with self._chat_lock:
                    self._eligible_chat.discard(login)
                    self._chat_since.pop(login, None)
                    self._last_spike.pop(login, None)
            self.changed.set()
        elif kind == "channel.chat.message" and event.get("source_broadcaster_user_id") in (None, user_id):
            received = int(time.time())
            self._record_chat(login, event, _chat_second(metadata.get("message_timestamp"), received), received)

    def _record_chat(self, login, event, second, received):
        with self._chat_lock:
            if login not in self._eligible_chat or login not in self._chat_active:
                return
            if not received - CHAT_BUFFER_SECONDS <= second <= received + 5:
                return
            message_id = event.get("message_id")
            if not message_id or message_id in self._message_ids:
                return
            self._message_ids[message_id] = None
            if len(self._message_ids) > 40_000:
                self._message_ids.popitem(last=False)
            message = event.get("message") or {}
            text = message.get("text") or ""
            if NOISE.search(text):
                return
            seconds = self._chat.setdefault(login, OrderedDict())
            while seconds and next(iter(seconds)) < received - CHAT_BUFFER_SECONDS:
                seconds.popitem(last=False)
            new_bucket = second not in seconds
            bucket = seconds.setdefault(second, Counter())
            bucket["messages"] += 1
            bucket["hype"] += bool(HYPE.search(text))
            bucket["negative"] += bool(NEGATIVE.search(text))
            bucket["gta"] += bool(GTA.search(_normalize(text)))
            bucket["emotes"] += sum(1 for part in message.get("fragments", ()) if part.get("type") == "emote")
            if new_bucket and received - self._last_spike.get(login, 0) >= 45:
                snapshot = self._chat_snapshot(login, received)
                if snapshot["messages"] >= 8 and snapshot["ratio"] >= 3:
                    self._last_spike[login] = received
                    self.changed.set()

    def chat_snapshot(self, login):
        with self._chat_lock:
            if login not in self._eligible_chat or login not in self._chat_active:
                return None
            return self._chat_snapshot(login, int(time.time()))

    def chat_timeline(self, login):
        """Return recent aggregate engagement keyed by Twitch event time, with no message text or identity."""
        earliest = int(time.time()) - CHAT_BUFFER_SECONDS
        with self._chat_lock:
            return [{"at": second, "count": bucket["messages"], "reactions": bucket["hype"],
                     "emotes": bucket["emotes"], "gta_messages": bucket["gta"]}
                    for second, bucket in sorted(self._chat.get(login, {}).items()) if second >= earliest]

    def _chat_snapshot(self, login, now):
        recent, previous = Counter(), Counter()
        started = self._chat_since.get(login, now)
        for second, bucket in self._chat.get(login, {}).items():
            if second < int(started):
                continue
            if now - 10 < second <= now:
                recent.update(bucket)
            elif now - 130 < second <= now - 10:
                previous.update(bucket)
        available_baseline = min(120, max(0, now - started - 10))
        rate = recent["messages"] / 10
        baseline = previous["messages"] / available_baseline if available_baseline >= 30 else None
        ratio = rate / max(baseline, 0.1) if baseline is not None else 0.0
        current_sentiment = (recent["hype"] - recent["negative"]) / max(recent["messages"], 1)
        baseline_sentiment = (previous["hype"] - previous["negative"]) / max(previous["messages"], 1)
        return {"messages": recent["messages"], "messages_per_second": rate,
                "baseline_messages_per_second": baseline, "ratio": ratio,
                "hype_messages": recent["hype"], "emotes": recent["emotes"], "gta_messages": recent["gta"],
                "sentiment_change": current_sentiment - baseline_sentiment if baseline is not None else None,
                "window_end": now}

    def _subscribe(self, session_id, user_id, scopes):
        subscriptions = [("channel.chat.message", broadcaster, {"user_id": user_id})
                         for broadcaster, login in self._lookup.items()
                         if login in self._chat_logins and "user:read:chat" in scopes]
        subscriptions.extend((kind, broadcaster, {}) for kind in ("stream.online", "stream.offline")
                             for broadcaster in self._lookup)
        count, active_chat = 0, set()
        for kind, broadcaster, extra in subscriptions:
            if self._stop.is_set():
                break
            if (kind, broadcaster) in self._disabled:
                continue
            condition = {"broadcaster_user_id": broadcaster, **extra}
            try:
                self.client._subscribe(self._token, kind, condition, session_id)
            except TwitchError as error:
                if error.status == 401:
                    raise TwitchConfigurationError("Twitch EventSub user token was rejected", 401) from None
                LOG.warning("Twitch EventSub %s for %s unavailable (%s)", kind, self._lookup[broadcaster], error)
                continue
            count += 1
            if kind == "channel.chat.message":
                active_chat.add(self._lookup[broadcaster])
        self._set_active_chat(active_chat)
        if not count and not self._stop.is_set():
            raise TwitchError("Twitch accepted no subscriptions for this EventSub session")

    @staticmethod
    def _welcome(socket):
        welcome = json.loads(socket.recv(timeout=5))
        if (welcome.get("metadata") or {}).get("message_type") != "session_welcome":
            raise TwitchError("Twitch EventSub did not send a welcome message")
        session = (welcome.get("payload") or {}).get("session") or {}
        return session["id"], float(session.get("keepalive_timeout_seconds") or 30)

    @staticmethod
    def _connect(connect, url):
        parsed = urlsplit(url)
        if parsed.scheme != "wss" or parsed.netloc != "eventsub.wss.twitch.tv":
            raise TwitchError("Twitch returned an invalid EventSub reconnect URL")
        return connect(url, open_timeout=8, close_timeout=2, ping_interval=None)

    def _drain(self, socket):
        from websockets.exceptions import ConnectionClosed

        for _ in range(2000):
            try:
                pending = json.loads(socket.recv(timeout=0))
            except (TimeoutError, ConnectionClosed):
                return
            self._handle_message(pending.get("metadata") or {}, pending.get("payload") or {})

    def _session(self, connect):
        token_info = self.client._validate(self._token, attempts=1)
        user_id = token_info.get("user_id")
        if not user_id:
            raise TwitchConfigurationError("Twitch EventSub requires a user access token")
        scopes = token_info.get("scopes") or []
        if self._chat_logins and "user:read:chat" not in scopes:
            LOG.warning("Twitch EventSub chat requires user:read:chat scope")
        validate_at = time.monotonic() + 55 * 60
        socket = self._connect(connect, EVENTSUB)
        try:
            session_id, keepalive = self._welcome(socket)
            self._subscribe(session_id, user_id, scopes)
            self.connected.set()
            self.changed.set()
            deadline = time.monotonic() + keepalive + 3
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= deadline:
                    raise TwitchError("Twitch EventSub keepalive timed out")
                if now >= validate_at:
                    self.client._validate(self._token, attempts=1)
                    validate_at = time.monotonic() + 55 * 60
                try:
                    message = json.loads(socket.recv(timeout=min(2, deadline - now)))
                except TimeoutError:
                    continue
                metadata, payload = message.get("metadata") or {}, message.get("payload") or {}
                if metadata.get("message_type") == "session_reconnect":
                    replacement = self._connect(connect, payload["session"]["reconnect_url"])
                    try:
                        _, keepalive = self._welcome(replacement)
                    except Exception:
                        replacement.close()
                        raise
                    previous, socket = socket, replacement
                    try:
                        self._drain(previous)
                    finally:
                        previous.close()
                else:
                    self._handle_message(metadata, payload)
                deadline = time.monotonic() + keepalive + 3
        finally:
            socket.close()

    def _run(self, connect, websocket_error):
        delay = 2
        while not self._stop.is_set():
            connected_at = time.monotonic()
            try:
                self._session(connect)
            except TwitchConfigurationError as error:
                LOG.warning("Twitch EventSub authorization issue (%s). Helix monitoring is available", error)
                return
            except TwitchError as error:
                LOG.warning("Twitch EventSub unavailable (%s). Helix monitoring is available", error)
                if error.status == 401:
                    return
                delay = min(60, max(delay, error.retry_after or 0))
            except (websocket_error, OSError, KeyError, ValueError, TypeError, AttributeError) as error:
                LOG.warning("Twitch EventSub disconnected (%s). Helix monitoring is available", type(error).__name__)
            finally:
                self.connected.clear()
                self._set_active_chat(set())
            if time.monotonic() - connected_at >= 60:
                delay = 2
            self._stop.wait(min(60, delay + random.random()))
            delay = min(60, 2 * delay)


def _prompt_secret(prompt):
    import getpass
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt).strip()
    except (getpass.GetPassWarning, EOFError):
        raise TwitchConfigurationError("Use an interactive terminal for hidden credential entry") from None


def _configure(eventsub, channels, clip_download=False):
    if _credential_path() is None:
        raise TwitchConfigurationError("Twitch credential storage requires Windows and LOCALAPPDATA")
    if channels and not eventsub:
        raise TwitchConfigurationError("Chat configuration requires --eventsub and an authorized user token")
    try:
        previous = _read_credentials() if eventsub or clip_download else {}
    except TwitchConfigurationError:
        previous = {}
    client_id = (os.getenv("TWITCH_CLIENT_ID") or previous.get("client_id") or input("Twitch app client ID: ")).strip()
    if client_id != previous.get("client_id"):
        previous = {}
    secret = os.getenv("TWITCH_CLIENT_SECRET") or previous.get("client_secret") or _prompt_secret("Twitch app client secret: ")
    client = TwitchClient(client_id, secret)
    if set(client.resolve_creators()) != set(CREATORS):
        raise TwitchConfigurationError("Helix must resolve all five Twitch channels before configuration is saved")
    saved = {**previous, "client_id": client_id, "client_secret": secret}
    if eventsub:
        token = os.getenv("TWITCH_EVENTSUB_USER_TOKEN") or _prompt_secret("Twitch EventSub user access token: ")
        if not token:
            raise TwitchConfigurationError("Twitch EventSub requires a user access token")
        info = client._validate(token)
        if not info.get("user_id"):
            raise TwitchConfigurationError("Twitch EventSub requires a user access token")
        if channels and "user:read:chat" not in (info.get("scopes") or []):
            raise TwitchConfigurationError("Twitch chat configuration requires the user:read:chat scope")
        saved.update(eventsub_token=token, chat_logins=sorted(set(channels)))
    if clip_download:
        token = os.getenv("TWITCH_CLIP_EDITOR_USER_TOKEN") or _prompt_secret("Twitch broadcaster or editor user access token: ")
        client._verify_editor_token(token)
        saved["clip_editor_token"] = token
    _save_credentials(saved)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Store authenticated Twitch configuration for this Windows user.")
    parser.add_argument("command", choices=("configure",))
    parser.add_argument("--eventsub", action="store_true", help="also store an authorized user access token")
    parser.add_argument("--clip-download", action="store_true", help="store an authorized broadcaster or editor user token")
    parser.add_argument("--chat-channel", action="append", choices=CREATORS, default=[],
                        help="collect aggregate chat signals from this authorized channel")
    args = parser.parse_args()
    try:
        _configure(args.eventsub, args.chat_channel, args.clip_download)
    except TwitchError as error:
        parser.exit(1, f"{error}\n")
    except (OSError, EOFError):
        parser.exit(1, "Could not configure Twitch credentials for this Windows user.\n")
    print("Verified all five Twitch channels and stored Twitch credentials for this Windows user.")


if __name__ == "__main__":
    main()
