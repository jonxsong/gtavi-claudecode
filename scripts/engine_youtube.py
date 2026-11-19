"""Official YouTube APIs for the fixed GTA VIBING destination."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import requests


CHANNEL_HANDLE = "@GTA-Vibing"
CHANNEL_ID = "UCAtLKFamARYmLeTcwzA3akw"
DATA_API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_API = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
TOKEN_API = "https://oauth2.googleapis.com/token"
REVOKE_API = "https://oauth2.googleapis.com/revoke"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE_PREFIX = "https://www.googleapis.com/auth/"
REQUIRED_SCOPES = tuple(SCOPE_PREFIX + scope for scope in ("youtube", "youtube.readonly"))
SCOPES = (*REQUIRED_SCOPES, SCOPE_PREFIX + "yt-analytics-monetary.readonly")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHUNK_SIZE = 8 * 1024 * 1024  # Non-final chunks are multiples of 256 KiB.
THUMBNAIL_LIMIT = 50_000_000
THUMBNAIL_TYPES = {".jpg": ("image/jpeg", b"\xff\xd8\xff"), ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
                   ".png": ("image/png", b"\x89PNG\r\n\x1a\n")}
ACTIVITY_METRICS = (
    "views", "engagedViews", "averageViewDuration", "averageViewPercentage",
    "shares", "likes", "comments", "subscribersGained", "subscribersLost",
)
RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded", "uploadRateLimitExceeded"}


class YouTubeError(RuntimeError):
    def __init__(self, message, *, status=None, reason=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


class YouTubeAuthRequired(YouTubeError):
    """One-time OAuth configuration or renewed Google consent is required."""


class YouTubeRetryable(YouTubeError):
    """Try again later, preserving the previous operation's state."""


class YouTubeProcessing(YouTubeRetryable):
    """YouTube has not finished processing or indexing the private upload."""


class YouTubeUploadUncertain(YouTubeError):
    """Reconcile the previous attempt before creating any new upload."""


class YouTubeUploadStopped(YouTubeError):
    """The local publication guard stopped a request."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise YouTubeAuthRequired("YouTube credential storage requires Windows DPAPI.")
    storage = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = _DataBlob(len(data), storage)
    outgoing = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    transform = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    transform.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    transform.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not transform(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise YouTubeAuthRequired("Windows could not open the YouTube credential for this user.")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel32.LocalFree(outgoing.data)


def credential_path() -> Path:
    appdata = os.getenv("LOCALAPPDATA")
    if not appdata:
        raise YouTubeAuthRequired("The Windows LOCALAPPDATA directory is unavailable.")
    return Path(appdata) / "gtavi-claudecode" / "youtube-oauth.dpapi"


def _store_refresh(client_id, refresh_token):
    path = credential_path()
    encrypted = _dpapi(json.dumps({"client_id": client_id, "refresh_token": refresh_token}).encode(),
                       protect=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(encrypted)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_refresh(client_id):
    try:
        saved = json.loads(_dpapi(credential_path().read_bytes(), protect=False))
    except (FileNotFoundError, ValueError):
        raise YouTubeAuthRequired("Connect the Google account and select GTA VIBING through YouTube OAuth.") from None
    if not isinstance(saved, dict) or saved.get("client_id") != client_id or not saved.get("refresh_token"):
        raise YouTubeAuthRequired("The saved YouTube authorization does not match this Desktop OAuth client.")
    return saved["refresh_token"]


def _client_settings():
    client_file = os.getenv("YOUTUBE_OAUTH_CLIENT_FILE")
    client_id = os.getenv("YOUTUBE_CLIENT_ID")
    secret = os.getenv("YOUTUBE_CLIENT_SECRET")
    if not client_file and not client_id and os.getenv("LOCALAPPDATA"):
        default_path = credential_path().with_name("youtube-client.json")
        if default_path.is_file():
            client_file = default_path
    if client_file:
        try:
            installed = json.loads(Path(client_file).read_text(encoding="utf-8-sig"))["installed"]
            client_id, secret = installed["client_id"], installed["client_secret"]
        except (OSError, KeyError, TypeError, ValueError):
            raise YouTubeAuthRequired("The YouTube client file must be a Google Desktop OAuth JSON file.") from None
    if (not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com")
            or not isinstance(secret, str) or not secret):
        raise YouTubeAuthRequired("Configure a Google Desktop OAuth client for the YouTube APIs.")
    return client_id, secret


def _browser_authorization(client_id, *, timeout=300):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    result = {}

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *_args):
            pass  # Callback URLs may contain authorization codes.

        def do_GET(self):
            path = urlsplit(self.path)
            values = parse_qs(path.query)
            returned_state = values.get("state", [""])[0]
            if path.path != "/" or not secrets.compare_digest(returned_state.encode(), state.encode()):
                self.send_error(400, "Invalid OAuth callback")
                return
            result["code"] = values.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to the terminal. It will verify the selected YouTube channel.")

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        redirect = f"http://127.0.0.1:{server.server_port}"
        url = AUTH_URL + "?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent select_account",
        })
        if not webbrowser.open(url):
            raise YouTubeAuthRequired("Windows could not open a browser for YouTube OAuth.")
        server.timeout = 1
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise YouTubeAuthRequired("Google consent was denied or did not complete in time.")
    return result["code"], verifier, redirect


def _response_json(response):
    try:
        data = response.json()
    except ValueError:
        raise YouTubeError("Google did not return a valid API response.") from None
    if not isinstance(data, dict):
        raise YouTubeError("Google returned an unexpected API response.")
    return data


def _error_reason(response):
    try:
        error = _response_json(response).get("error")
    except YouTubeError:
        return None
    if isinstance(error, dict):
        details = error.get("errors") or []
        if isinstance(details, list) and details and isinstance(details[0], dict):
            error = details[0].get("reason")
        else:
            error = error.get("status")
    return error if isinstance(error, str) and re.fullmatch(r"[A-Za-z_]{1,60}", error) else None


def _retry_after(response):
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    if value.isdecimal():
        return int(value)
    try:
        return max(0, int(parsedate_to_datetime(value).timestamp() - time.time()))
    except (OverflowError, TypeError, ValueError):
        return None


def _run_guard(guard):
    if guard is not None and guard() is False:
        raise YouTubeUploadStopped("YouTube request stopped by the local publication guard.")


def _validate_session(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise YouTubeUploadUncertain("Invalid YouTube resumable upload endpoint.") from None
    if (parsed.scheme != "https" or parsed.netloc != "www.googleapis.com"
            or parsed.path != "/upload/youtube/v3/videos" or parsed.fragment
            or len(parse_qs(parsed.query).get("upload_id", [])) != 1):
        raise YouTubeUploadUncertain("Invalid YouTube resumable upload endpoint.")
    return url


def _upload_offset(response, total):
    received = response.headers.get("Range")
    if not received:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", received)
    if not match or int(match[1]) >= total:
        raise YouTubeUploadUncertain("YouTube returned an invalid resumable upload range.")
    return int(match[1]) + 1


def _timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Use a timezone-aware publication time.")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _require_processed_upload(video):
    status = video.get("status", {})
    processed = video.get("processingDetails", {}).get("processingStatus")
    upload_status = status.get("uploadStatus")
    if (upload_status in {"failed", "rejected", "deleted"} or processed in {"failed", "terminated"}
            or status.get("rejectionReason") or status.get("failureReason")):
        raise YouTubeError("YouTube rejected the upload or failed processing it.")
    if processed != "succeeded" or upload_status != "processed":
        raise YouTubeProcessing("YouTube has not finished processing the upload.")


class YouTube:
    def __init__(self, *, transport=None):
        self._send = transport or requests.request
        self._access = None
        self._expires = 0.0
        self._verified = None
        self._credentials_lock = threading.RLock()

    def _request(self, method, url, *, auth=True, bearer=None, verify=True, attempts=3,
                 expect=(200,), headers=None, guard=None, **kwargs):
        for attempt in range(attempts):
            token = bearer or (self._access_token() if auth else None)
            request_headers = dict(headers or {})
            if token:
                if verify:
                    self._assert_channel(token)
                request_headers["Authorization"] = "Bearer " + token
            _run_guard(guard)
            try:
                response = self._send(method, url, headers=request_headers, timeout=(10, 35),
                                      allow_redirects=False, **kwargs)
            except requests.RequestException:
                failure = YouTubeRetryable("Google API network request failed.")
            else:
                status = response.status_code
                if status in expect:
                    return response
                reason = _error_reason(response)
                message = f"Google API returned HTTP {status}" + (f" ({reason})." if reason else ".")
                delay = _retry_after(response)
                if status == 401 and auth and bearer is None:
                    with self._credentials_lock:
                        if self._access == token:
                            self._expires = 0.0
                    failure = YouTubeRetryable(message, status=status, reason=reason)
                elif reason in QUOTA_REASONS:
                    raise YouTubeRetryable(message, status=status, reason=reason, retry_after=delay or 3600)
                elif status == 429 or 500 <= status < 600 or reason in RETRY_REASONS:
                    failure = YouTubeRetryable(message, status=status, reason=reason, retry_after=delay)
                else:
                    raise YouTubeError(message, status=status, reason=reason)
            delay = failure.retry_after if failure.retry_after is not None else 2 ** attempt
            if attempt + 1 >= attempts or delay > 15:
                raise failure
            time.sleep(delay)

    def _token_response(self, data, *, attempts=3):
        try:
            return _response_json(self._request("POST", TOKEN_API, auth=False, data=data, attempts=attempts))
        except YouTubeError as error:
            if error.reason in {"invalid_client", "invalid_grant", "unauthorized_client"}:
                raise YouTubeAuthRequired("Google rejected the Desktop OAuth client or its consent; reconnect it.") from None
            raise

    def _set_access(self, token):
        if not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise YouTubeAuthRequired("Google did not grant a YouTube access token.")
        self._access = token["access_token"]
        self._expires = time.monotonic() + max(1, int(token.get("expires_in", 3600)) - 60)
        return self._access

    def _access_token(self):
        with self._credentials_lock:
            if self._access and self._expires > time.monotonic():
                return self._access
            client_id, secret = _client_settings()
            refresh = _read_refresh(client_id)
            result = self._token_response({"client_id": client_id, "client_secret": secret,
                                           "grant_type": "refresh_token", "refresh_token": refresh})
            if result.get("refresh_token") and result["refresh_token"] != refresh:
                _store_refresh(client_id, result["refresh_token"])
            return self._set_access(result)

    def _assert_channel(self, token, *, fresh=False):
        with self._credentials_lock:
            if not fresh and self._verified is not None and self._verified[0] == token:
                return self._verified[1]
            try:
                payload = _response_json(self._request(
                    "GET", DATA_API + "/channels", auth=False, bearer=token, verify=False,
                    params={"part": "snippet,statistics,status,contentDetails", "mine": "true"},
                ))
            except YouTubeError as error:
                if error.status == 401:
                    if self._access == token:
                        self._expires = 0.0
                    raise YouTubeRetryable("Google did not accept the current YouTube access token.") from None
                raise
            channels = payload.get("items", [])
            if len(channels) != 1 or payload.get("nextPageToken") or channels[0].get("id") != CHANNEL_ID:
                raise YouTubeAuthRequired(f"Google OAuth must select {CHANNEL_HANDLE} ({CHANNEL_ID}).")
            self._verified = (token, channels[0])
            return channels[0]

    def authorize(self, *, timeout=300):
        """Open the Google consent screen, check the channel and store only a refresh token."""
        credential_path()
        if os.name != "nt":
            raise YouTubeAuthRequired("YouTube credential storage requires Windows DPAPI.")
        client_id, secret = _client_settings()
        code, verifier, redirect = _browser_authorization(client_id, timeout=timeout)
        result = self._token_response({
            "client_id": client_id, "client_secret": secret, "grant_type": "authorization_code",
            "code": code, "code_verifier": verifier, "redirect_uri": redirect,
        }, attempts=1)
        if not result.get("refresh_token") or not result.get("access_token"):
            raise YouTubeAuthRequired("Google did not grant an offline YouTube credential.")
        if not set(REQUIRED_SCOPES).issubset(set(result.get("scope", "").split())):
            raise YouTubeAuthRequired("Google did not grant the required YouTube scopes.")
        channel = self._assert_channel(result["access_token"], fresh=True)
        _store_refresh(client_id, result["refresh_token"])
        with self._credentials_lock:
            self._set_access(result)
        return channel

    def channel(self):
        """Return authenticated GTA VIBING metadata and statistics."""
        return self._assert_channel(self._access_token(), fresh=True)

    def revoke(self):
        """Revoke the locally saved offline grant and clear the cached access token."""
        path = credential_path()
        if not path.is_file():
            return
        try:
            record = json.loads(_dpapi(path.read_bytes(), protect=False))
            token = record["refresh_token"]
        except (KeyError, OSError, ValueError, TypeError):
            raise YouTubeAuthRequired("The saved authorization could not be read. Revoke it in Google Account settings.") from None
        try:
            self._request("POST", REVOKE_API, auth=False, data={"token": token})
        except YouTubeError as error:
            if error.reason not in {"invalid_token", "invalid_grant"}:
                raise
        with self._credentials_lock:
            path.unlink(missing_ok=True)
            self._access = self._verified = None
            self._expires = 0

    def recent_uploads(self, limit=50):
        """Find videos already uploaded to GTA VIBING via its uploads playlist."""
        if not 1 <= limit <= 50:
            raise ValueError("The recent upload check can read 1–50 videos at a time.")
        channel = self._assert_channel(self._access_token())
        playlist_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not playlist_id:
            raise YouTubeError("YouTube did not identify the channel's uploads playlist.")
        result = _response_json(self._request("GET", DATA_API + "/playlistItems", params={
            "part": "snippet,status,contentDetails", "playlistId": playlist_id, "maxResults": limit,
        }))
        uploads = []
        for item in result.get("items", []):
            snippet = item.get("snippet", {})
            video_id = item.get("contentDetails", {}).get("videoId") or snippet.get("resourceId", {}).get("videoId", "")
            if isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id):
                uploads.append({"video_id": video_id, "title": snippet.get("title") or "",
                                "description": snippet.get("description") or "",
                                "privacy": item.get("status", {}).get("privacyStatus")})
        return uploads

    def start_upload(self, path, *, title, description, made_for_kids=False, tags=(),
                     paid_promotion=False, contains_synthetic_media=False, guard=None):
        """Create a private resumable session. Persist its URL before sending footage."""
        _run_guard(guard)
        if not title.strip() or len(title) > 100 or len(description.encode()) > 5000:
            raise YouTubeError("Use a title of at most 100 characters and a description of at most 5,000 bytes.")
        if any(char in title or char in description for char in "<>"):
            raise YouTubeError("YouTube titles and descriptions cannot contain angle brackets.")
        path = Path(path)
        total = path.stat().st_size
        if path.suffix.lower() != ".mp4" or total <= 0:
            raise YouTubeError("YouTube upload requires a nonempty MP4 file.")
        response = self._request(
            "POST", UPLOAD_API, guard=guard, expect=(200, 201),
            params={"uploadType": "resumable", "part": "snippet,status,paidProductPlacementDetails"},
            headers={"X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/mp4"},
            json={
                "snippet": {"title": title, "description": description, "categoryId": "20", "tags": list(tags)},
                "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": made_for_kids,
                           "containsSyntheticMedia": contains_synthetic_media},
                "paidProductPlacementDetails": {"hasPaidProductPlacement": paid_promotion},
            },
        )
        return _validate_session(response.headers.get("Location", ""))

    def _upload_id(self, response):
        if response.status_code in {404, 410}:
            raise YouTubeUploadUncertain("YouTube upload session expired. Check for an existing private video.")
        if response.status_code == 308:
            return None
        try:
            item = _response_json(response)
        except YouTubeError:
            raise YouTubeUploadUncertain("YouTube accepted the upload but did not return its video ID.") from None
        video_id = item.get("id", "")
        if (not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id)
                or item.get("snippet", {}).get("channelId") != CHANNEL_ID
                or item.get("status", {}).get("privacyStatus") != "private"):
            raise YouTubeUploadUncertain("YouTube did not confirm a private upload to GTA VIBING.")
        return video_id

    def resume_upload(self, session_url, path, *, guard=None):
        """Resume from YouTube's acknowledged offset. A failed chunk is never blindly resent."""
        _run_guard(guard)
        session_url = _validate_session(session_url)
        with Path(path).open("rb") as footage:
            total = os.fstat(footage.fileno()).st_size
            if not total:
                raise YouTubeError("The upload artifact is empty.")
            expected = (200, 201, 308, 404, 410)
            response = self._request("PUT", session_url, expect=expected,
                                     headers={"Content-Length": "0", "Content-Range": f"bytes */{total}"},
                                     data=b"", guard=guard)
            while True:
                completed = self._upload_id(response)
                if completed:
                    return completed
                delay = _retry_after(response)
                if delay:
                    raise YouTubeRetryable("YouTube requested a delay before continuing the upload.", retry_after=delay)
                offset = _upload_offset(response, total)
                if offset >= total:
                    raise YouTubeRetryable("YouTube received all bytes and has not returned the video ID.")
                footage.seek(offset)
                count = min(CHUNK_SIZE, total - offset)
                chunk = footage.read(count)
                if len(chunk) != count:
                    raise YouTubeUploadUncertain("The upload artifact changed during transfer.")
                response = self._request(
                    "PUT", session_url, expect=expected, attempts=1, data=chunk, guard=guard,
                    headers={"Content-Length": str(count), "Content-Type": "video/mp4",
                             "Content-Range": f"bytes {offset}-{offset + count - 1}/{total}"},
                )
                if response.status_code == 308 and _upload_offset(response, total) <= offset:
                    raise YouTubeRetryable("YouTube has not acknowledged progress on the upload.")

    def video(self, video_id):
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise YouTubeError("Invalid YouTube video ID.")
        payload = _response_json(self._request("GET", DATA_API + "/videos", params={
            "part": "snippet,status,processingDetails,statistics,contentDetails,paidProductPlacementDetails",
            "id": video_id,
        }))
        items = payload.get("items", [])
        if not items:
            raise YouTubeProcessing("YouTube has not indexed the private upload.")
        if (len(items) != 1 or items[0].get("id") != video_id
                or items[0].get("snippet", {}).get("channelId") != CHANNEL_ID):
            raise YouTubeError("The video does not belong to GTA VIBING.")
        return items[0]

    def set_shorts_thumbnail(self, video_id, path, *, guard=None):
        """Set the cover on a processed Short and return its confirmed portrait thumbnail."""
        _run_guard(guard)
        _require_processed_upload(self.video(video_id))
        path = Path(path)
        format_info = THUMBNAIL_TYPES.get(path.suffix.lower())
        if format_info is None:
            raise YouTubeError("YouTube Shorts thumbnails must be JPEG or PNG images.")
        mime_type, signature = format_info
        with path.open("rb") as image:
            data = image.read(THUMBNAIL_LIMIT + 1)
        if not data.startswith(signature) or len(data) > THUMBNAIL_LIMIT:
            raise YouTubeError("Use a valid JPEG or PNG thumbnail that is at most 50 MB.")
        response = self._request(
            "POST", THUMBNAIL_API, params={"videoId": video_id, "uploadType": "media"},
            data=data, headers={"Content-Type": mime_type}, guard=guard, attempts=1,
        )
        try:
            items = _response_json(response).get("items")
        except YouTubeError:
            raise YouTubeRetryable("YouTube did not return a thumbnail confirmation.") from None
        if not isinstance(items, list):
            raise YouTubeRetryable("YouTube did not return a thumbnail confirmation.")
        for item in items:
            thumbnail = item.get("shortsAspectRatio") if isinstance(item, dict) else None
            if not isinstance(thumbnail, dict):
                continue
            url = thumbnail.get("url", "")
            if not isinstance(url, str) or not url.startswith("https://"):
                continue
            width, height = thumbnail.get("width"), thumbnail.get("height")
            if width is not None and height is not None:
                if (type(width) is not int or type(height) is not int or min(width, height) <= 0
                        or abs(width - height * 9 / 16) > 1):
                    continue
            return thumbnail
        raise YouTubeRetryable("YouTube did not confirm the 9:16 Shorts thumbnail.")

    def publish(self, video_id, *, publish_at=None, paid_promotion=None, guard=None):
        """Promote a successfully processed private video to public or schedule it."""
        _run_guard(guard)
        target = _timestamp(publish_at) if publish_at is not None else None
        current = self.video(video_id)
        status = current.get("status", {})
        privacy = status.get("privacyStatus")
        scheduled = _timestamp(status["publishAt"]) if status.get("publishAt") else None
        now = datetime.now(timezone.utc)
        placement = current.get("paidProductPlacementDetails", {}).get("hasPaidProductPlacement")
        if paid_promotion is not None and placement is not paid_promotion:
            raise YouTubeError("The private upload's YouTube paid-promotion setting does not match its disclosure.")
        if privacy == "public" and (target is None or target <= now):
            return current  # A previous attempt may have updated YouTube before local state was saved.
        if privacy != "private":
            raise YouTubeError("YouTube publication requires an existing private upload.")
        if scheduled is not None:
            if target == scheduled:
                return current
            raise YouTubeError("The private video already has a different publication schedule.")
        if target is not None and target <= now:
            raise YouTubeError("The scheduled publication time must still be in the future.")
        _require_processed_upload(current)
        if current.get("contentDetails", {}).get("contentRating", {}).get("ytRating") == "ytAgeRestricted":
            raise YouTubeError("YouTube has age-restricted this private upload.")
        body = {key: status[key] for key in ("license", "embeddable", "publicStatsViewable",
                                             "selfDeclaredMadeForKids", "containsSyntheticMedia") if key in status}
        body["privacyStatus"] = "private" if target else "public"
        if target:
            body["publishAt"] = target.isoformat().replace("+00:00", "Z")

        def publication_guard():
            _run_guard(guard)
            if target is not None and target <= datetime.now(timezone.utc):
                raise YouTubeError("The scheduled publication time must still be in the future.")

        result = _response_json(self._request(
            "PUT", DATA_API + "/videos", params={"part": "status"},
            json={"id": video_id, "status": body}, guard=publication_guard, attempts=1,
        ))
        changed = result.get("status", {})
        try:
            confirmed_time = _timestamp(changed["publishAt"]) if changed.get("publishAt") else None
        except ValueError:
            confirmed_time = None
        if (result.get("id") != video_id or changed.get("privacyStatus") != body["privacyStatus"]
                or confirmed_time != target):
            raise YouTubeRetryable("YouTube has not confirmed the requested publication state.")
        if target is None:
            public = self.video(video_id)
            if public.get("status", {}).get("privacyStatus") != "public":
                raise YouTubeRetryable("The updated video is not publicly visible yet.")
            return public
        return {**current, "status": changed}

    def _report(self, params, metrics, *, dimensions=None):
        query = {**params, "metrics": ",".join(metrics)}
        if dimensions:
            query["dimensions"] = dimensions
        result = _response_json(self._request("GET", ANALYTICS_API, params=query))
        headings = [column["name"] for column in result.get("columnHeaders", [])]
        return [dict(zip(headings, row)) for row in (result.get("rows") or [])]

    def analytics(self, video_id, start_date, end_date):
        """Get requested rolling Analytics totals. The API can lag by 48–72 hours."""
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise YouTubeError("Invalid YouTube video ID.")
        start = date.fromisoformat(str(start_date))
        end = date.fromisoformat(str(end_date))
        if end < start:
            raise ValueError("The analytics date range is reversed.")
        params = {"ids": f"channel=={CHANNEL_ID}", "filters": f"video=={video_id}",
                  "startDate": start.isoformat(), "endDate": end.isoformat()}
        unavailable = {"viewedVersusSwipedAway": "not available in documented API reports"}
        output = {"analyticsRequestedStartDate": params["startDate"],
                  "analyticsRequestedEndDate": params["endDate"], "unavailable": unavailable}
        activity = self._report(params, ACTIVITY_METRICS)
        output.update(activity[0] if activity else {})
        for metric in ACTIVITY_METRICS:
            if metric not in output:
                unavailable[metric] = "no reported data"
        for label, metrics, dimension in (
            ("estimatedRevenue", ("estimatedRevenue",), None),
            ("audienceRetention", ("audienceWatchRatio",), "elapsedVideoTimeRatio"),
        ):
            try:
                rows = self._report({**params, "currency": "USD"} if dimension is None else params,
                                    metrics, dimensions=dimension)
            except YouTubeRetryable:
                raise
            except YouTubeError as error:
                if error.status not in {400, 403, 404}:
                    raise
                unavailable[label] = f"report unavailable (HTTP {error.status})"
                continue
            if not rows or (dimension is None and label not in rows[0]):
                unavailable[label] = "no reported data"
            elif dimension:
                output[label] = rows
            else:
                output[label] = rows[0][label]
                output["estimatedRevenueCurrency"] = "USD"
        return output
