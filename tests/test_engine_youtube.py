"""Regression checks for channel identity, resumable transfer and publication."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import engine_youtube as youtube


SESSION = youtube.UPLOAD_API + "?uploadType=resumable&upload_id=test"
VIDEO_ID = "QxXVMysHN6s"
CHANNEL = {"id": youtube.CHANNEL_ID, "contentDetails": {"relatedPlaylists": {"uploads": "uploads-id"}}}
THUMBNAIL = {"url": "https://i.ytimg.com/vi/test/portrait.jpg", "width": 608, "height": 1080}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/wAAAABJRU5ErkJggg==")


def response(status=200, payload=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload or {}).encode()
    result.headers.update(headers or {})
    return result


def private_upload(processed=True, paid=True):
    return {
        "id": VIDEO_ID,
        "snippet": {"channelId": youtube.CHANNEL_ID},
        "status": {"privacyStatus": "private", "uploadStatus": "processed" if processed else "uploaded",
                   "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True, "license": "youtube"},
        "processingDetails": {"processingStatus": "succeeded" if processed else "processing"},
        "paidProductPlacementDetails": {"hasPaidProductPlacement": paid},
    }


class YouTubeTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.replies = []
        self.client = youtube.YouTube(transport=self.request)
        self.client._access_token = lambda: "test"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.file = Path(self.temporary.name) / "short.mp4"
        self.file.write_bytes(b"0123456789")

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        self.assertFalse(kwargs["allow_redirects"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def test_wrong_channel_stops_before_creating_upload_session(self):
        self.replies.append(response(payload={"items": [{"id": "UC-other"}]}))
        with self.assertRaises(youtube.YouTubeAuthRequired):
            self.client.start_upload(self.file, title="A title", description="A source")
        self.assertEqual([(method, url) for method, url, _ in self.calls], [
            ("GET", youtube.DATA_API + "/channels"),
        ])

    def test_private_session_includes_paid_promotion_and_resumes_from_acknowledged_range(self):
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(headers={"Location": SESSION}),
            response(308, headers={"Range": "bytes=0-4"}),
            response(201, private_upload()),
        ])
        session = self.client.start_upload(self.file, title="A title", description="A source", paid_promotion=True)
        self.assertEqual(session, SESSION)
        request = self.calls[1][2]
        self.assertEqual(request["json"]["status"]["privacyStatus"], "private")
        self.assertTrue(request["json"]["paidProductPlacementDetails"]["hasPaidProductPlacement"])
        self.assertEqual(self.client.resume_upload(session, self.file), VIDEO_ID)
        self.assertEqual(self.calls[2][2]["headers"]["Content-Range"], "bytes */10")
        self.assertEqual(self.calls[3][2]["headers"]["Content-Range"], "bytes 5-9/10")
        self.assertEqual(self.calls[3][2]["data"], b"56789")

    def test_ambiguous_final_request_is_checked_with_same_session(self):
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(308),
            requests.Timeout(),
            response(201, private_upload()),
        ])
        with self.assertRaises(youtube.YouTubeRetryable):
            self.client.resume_upload(SESSION, self.file)
        self.assertEqual(self.client.resume_upload(SESSION, self.file), VIDEO_ID)
        self.assertEqual([call[2]["data"] for call in self.calls[1:]], [b"", b"0123456789", b""])

    def test_expired_and_untrusted_sessions_cannot_start_a_second_upload(self):
        self.replies.extend([response(payload={"items": [CHANNEL]}), response(404)])
        with self.assertRaises(youtube.YouTubeUploadUncertain):
            self.client.resume_upload(SESSION, self.file)
        with self.assertRaises(youtube.YouTubeUploadUncertain):
            self.client.resume_upload("https://example.invalid/upload?upload_id=1", self.file)
        self.assertEqual(len(self.calls), 2)

    def test_processing_waits_and_publication_keeps_disclosures(self):
        public = private_upload()
        public["status"]["privacyStatus"] = "public"
        public["snippet"]["publishedAt"] = "2026-11-19T20:30:00Z"
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(payload={"items": [private_upload(processed=False)]}),
            response(payload={"items": [private_upload()]}),
            response(payload={"items": [private_upload()]}),
            response(payload={"id": VIDEO_ID, "status": {"privacyStatus": "public"}}),
            response(payload={"items": [public]}),
        ])
        with self.assertRaises(youtube.YouTubeProcessing):
            self.client.publish(VIDEO_ID, paid_promotion=True)
        with self.assertRaisesRegex(youtube.YouTubeError, "paid-promotion"):
            self.client.publish(VIDEO_ID, paid_promotion=False)
        self.assertEqual([call[0] for call in self.calls], ["GET", "GET", "GET"])
        result = self.client.publish(VIDEO_ID, paid_promotion=True)
        status = self.calls[-2][2]["json"]["status"]
        self.assertFalse(status["selfDeclaredMadeForKids"])
        self.assertTrue(status["containsSyntheticMedia"])
        self.assertEqual(result["status"]["privacyStatus"], "public")
        self.assertEqual(result["snippet"]["publishedAt"], "2026-11-19T20:30:00Z")

    def test_promotion_guard_is_checked_after_api_preflight(self):
        self.replies.extend([response(payload={"items": [CHANNEL]}),
                             response(payload={"items": [private_upload()]})])
        allowed = iter([True, False])
        with self.assertRaises(youtube.YouTubeUploadStopped):
            self.client.publish(VIDEO_ID, guard=lambda: next(allowed))
        self.assertEqual([call[0] for call in self.calls], ["GET", "GET"])

    def test_promotion_can_correct_legacy_title_without_erasing_snippet_fields(self):
        private = private_upload()
        private["etag"] = '"original-version"'
        private["snippet"].update(title="Original title", categoryId="20", defaultLanguage="en",
                                  description="Clearance and source", tags=["GTA6", "xQc"])
        title = "#xQc Original title"
        public = {**private, "status": {**private["status"], "privacyStatus": "public"},
                  "snippet": {**private["snippet"], "title": title}}
        self.replies.extend([response(payload={"items": [CHANNEL]}), response(payload={"items": [private]}),
                             response(payload=public), response(payload={"items": [public]})])

        self.assertEqual(self.client.publish(VIDEO_ID, title=title), public)

        method, _, request = self.calls[-2]
        self.assertEqual(method, "PUT")
        self.assertEqual(request["params"], {"part": "status,snippet"})
        self.assertEqual(request["headers"]["If-Match"], '"original-version"')
        self.assertEqual(request["json"]["snippet"], {"title": title, "categoryId": "20", "defaultLanguage": "en",
                                                      "description": "Clearance and source", "tags": ["GTA6", "xQc"]})

    def test_sets_portrait_thumbnail_for_processed_owner_short(self):
        cover = self.file.with_suffix(".png")
        cover.write_bytes(PNG)
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(payload={"items": [private_upload()]}),
            response(payload={"items": [{"shortsAspectRatio": THUMBNAIL}]}),
        ])
        self.assertEqual(self.client.set_shorts_thumbnail(VIDEO_ID, cover), THUMBNAIL)
        method, url, request = self.calls[-1]
        self.assertEqual((method, url), ("POST", youtube.THUMBNAIL_API))
        self.assertEqual(request["params"], {"videoId": VIDEO_ID, "uploadType": "media"})
        self.assertEqual(request["headers"]["Content-Type"], "image/png")
        self.assertEqual(request["data"], PNG)

    def test_thumbnail_skips_unprocessed_short_wrong_owner_and_revoked_guard(self):
        wrong_owner = private_upload()
        wrong_owner["snippet"]["channelId"] = "UC-other"
        cover = self.file.with_suffix(".png")
        cover.write_bytes(PNG)
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(payload={"items": [private_upload(processed=False)]}),
            response(payload={"items": [wrong_owner]}),
            response(payload={"items": [private_upload()]}),
        ])
        with self.assertRaises(youtube.YouTubeProcessing):
            self.client.set_shorts_thumbnail(VIDEO_ID, cover)
        with self.assertRaisesRegex(youtube.YouTubeError, "does not belong"):
            self.client.set_shorts_thumbnail(VIDEO_ID, cover)
        allowed = iter([True, False])
        with self.assertRaises(youtube.YouTubeUploadStopped):
            self.client.set_shorts_thumbnail(VIDEO_ID, cover, guard=lambda: next(allowed))
        self.assertTrue(all(call[0] == "GET" for call in self.calls))

    def test_thumbnail_requires_portrait_confirmation_and_honors_rate_limit(self):
        cover = self.file.with_suffix(".png")
        cover.write_bytes(PNG)
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(payload={"items": [private_upload()]}),
            response(payload={"items": [{"shortsAspectRatio": {**THUMBNAIL, "width": 1280, "height": 720}}]}),
            response(payload={"items": [private_upload()]}),
            response(429, {"error": {"errors": [{"reason": "uploadRateLimitExceeded"}]}}, {"Retry-After": "600"}),
        ])
        with self.assertRaisesRegex(youtube.YouTubeRetryable, "did not confirm"):
            self.client.set_shorts_thumbnail(VIDEO_ID, cover)
        with self.assertRaises(youtube.YouTubeRetryable) as limited:
            self.client.set_shorts_thumbnail(VIDEO_ID, cover)
        self.assertEqual(limited.exception.retry_after, 600)

    def test_analytics_keeps_missing_revenue_distinct_from_zero(self):
        self.replies.extend([
            response(payload={"items": [CHANNEL]}),
            response(payload={"columnHeaders": [{"name": "engagedViews"}], "rows": [[0]]}),
            response(403, {"error": {"status": "PERMISSION_DENIED"}}),
            response(payload={"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}],
                              "rows": [[0.05, 0.8]]}),
        ])
        result = self.client.analytics(VIDEO_ID, "2026-11-18", "2026-11-19")
        self.assertEqual(result["engagedViews"], 0)
        self.assertNotIn("estimatedRevenue", result)
        self.assertIn("estimatedRevenue", result["unavailable"])
        self.assertEqual(result["audienceRetention"][0]["audienceWatchRatio"], 0.8)

    def test_schedule_does_not_reissue_an_already_applied_update(self):
        target = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0)
        uploaded = private_upload()
        uploaded["status"]["publishAt"] = target.isoformat()
        self.replies.extend([response(payload={"items": [CHANNEL]}), response(payload={"items": [uploaded]})])
        self.assertEqual(self.client.publish(VIDEO_ID, publish_at=target), uploaded)
        self.assertTrue(all(call[0] == "GET" for call in self.calls))

    def test_desktop_oauth_accepts_only_matching_state_and_uses_pkce(self):
        replies = []
        oauth_parameters = {}

        def callback():
            url = oauth_parameters["redirect_uri"][0]
            replies.append(requests.get(url, params={"state": "wrong", "code": "bad"}, timeout=3).status_code)
            replies.append(requests.get(url, params={"state": oauth_parameters["state"][0], "code": "good"},
                                        timeout=3).status_code)

        thread = threading.Thread(target=callback, daemon=True)

        def open_browser(url):
            oauth_parameters.update(parse_qs(urlsplit(url).query))
            thread.start()
            return True

        with patch.object(youtube.webbrowser, "open", open_browser):
            code, verifier, _redirect_uri = youtube._browser_authorization("sample-client", timeout=5)
        thread.join(5)
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual((replies, code), ([400, 200], "good"))
        self.assertEqual(oauth_parameters["code_challenge"], [digest])
        self.assertEqual(oauth_parameters["code_challenge_method"], ["S256"])

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI is required")
    def test_refresh_credential_is_encrypted_outside_the_repository(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": self.temporary.name}):
            youtube._store_refresh("sample-client", "sample-refresh-secret")
            self.assertNotIn(b"sample-refresh-secret", youtube.credential_path().read_bytes())
            self.assertEqual(youtube._read_refresh("sample-client"), "sample-refresh-secret")
            with self.assertRaises(youtube.YouTubeAuthRequired):
                youtube._read_refresh("other-client")


if __name__ == "__main__":
    unittest.main()
