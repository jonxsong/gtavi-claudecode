import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from scripts.engine_twitch import (
    CREATORS,
    EVENTSUB,
    EventSubMonitor,
    TwitchClient,
    TwitchConfigurationError,
    TwitchError,
    _configure,
    gta_vi_eligibility,
)


SAMPLE_CDN_URL = "https://production.assets.clips.twitchcdn.net/clip.mp4?signature=sample"
SAMPLE_MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp41"


class ClipResponse(BytesIO):
    status = 200

    def __init__(self, data=SAMPLE_MP4_HEADER, headers=None):
        super().__init__(data)
        self.headers = {"Content-Type": "video/mp4", "Content-Length": str(len(data))} if headers is None else headers


class TwitchClientTests(unittest.TestCase):
    def setUp(self):
        self.client = TwitchClient("test-app", app_token="test-token")

    def test_whitelist_and_stream_must_match_resolved_id(self):
        streams = [
            {"user_id": "10", "user_login": "kaicenat", "type": "live"},
            {"user_id": "11", "user_login": "xqc", "type": "live"},
            {"user_id": "99", "user_login": "another-channel", "type": "live"},
        ]
        with patch.object(self.client, "_helix", side_effect=[{"data": [
            {"login": "kaicenat", "id": "10"}, {"login": "xqc", "id": "12"},
            {"login": "another-channel", "id": "99"},
        ]}, {"data": streams}]) as helix:
            creators = self.client.resolve_creators()
            self.assertEqual(creators, {"kaicenat": "10", "xqc": "12"})
            self.assertEqual(helix.call_args.args, ("users", [("login", login) for login in CREATORS]))
            self.assertEqual(self.client.get_streams({**creators, "another-channel": "99"}), {"kaicenat": streams[0]})
            self.assertEqual(helix.call_args.args, ("streams", [("user_id", "10"), ("user_id", "12")]))

    def test_metadata_range_survives_helix_minute_truncation(self):
        self.client._creator_ids = {"kaicenat": "10"}
        rows = [{"id": slug, "broadcaster_id": user_id, "created_at": f"2026-11-19T12:34:{second}Z"}
                for slug, user_id, second in (("before", "10", "04"), ("inside", "10", "06"),
                                               ("after", "10", "12"), ("other", "12", "06"))]
        with patch.object(self.client, "_helix", return_value={"data": rows}) as helix:
            clips = self.client.get_clips("10", "2026-11-19T12:34:05Z", "2026-11-19T12:34:10Z")
            self.assertEqual([clip["id"] for clip in clips], ["inside"])
            self.assertIn(("ended_at", "2026-11-19T12:35:00Z"), helix.call_args.args[1])

    def test_download_requires_matching_identity_scope_and_cdn(self):
        self.client._creator_ids = {"kaicenat": "10", "xqc": "12"}
        record = {"id": "clip", "broadcaster_id": "10"}
        with patch.object(self.client, "get_clip", return_value=record), \
                patch.object(self.client, "_validate", return_value={"user_id": "11", "scopes": []}), \
                patch.object(self.client, "_helix") as helix:
            self.assertIsNone(self.client.get_clip_download_url("clip", "12", "editor-token"))
            with self.assertRaises(TwitchConfigurationError):
                self.client.get_clip_download_url("clip", "10", "editor-token")
            helix.assert_not_called()
        info = {"user_id": "11", "scopes": ["editor:manage:clips"]}
        with patch.object(self.client, "get_clip", return_value=record), \
                patch.object(self.client, "_validate", return_value=info), \
                patch.object(self.client, "_helix") as helix:
            helix.return_value = {"data": [{"clip_id": "clip", "landscape_download_url": SAMPLE_CDN_URL}]}
            self.assertEqual(self.client.get_clip_download_url("clip", "10", "editor-token"), SAMPLE_CDN_URL)
            query = ("clips/downloads", [("editor_id", "11"), ("broadcaster_id", "10"), ("clip_id", "clip")])
            helix.assert_called_once_with(*query, token="editor-token")
            helix.return_value = {"data": [{"clip_id": "clip", "landscape_download_url": "https://evil.test/clip"}]}
            with self.assertRaises(TwitchError):
                self.client.get_clip_download_url("clip", "10", "editor-token")

    def test_signed_download_uses_guard_and_excludes_oauth_from_cdn(self):
        self.client._clip_editor_token = "editor-token"
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "clip.mp4"
            with patch.object(self.client, "get_clip_download_url", return_value=SAMPLE_CDN_URL) as resolve, \
                    patch.object(self.client._opener, "open", return_value=ClipResponse()) as cdn:
                with self.assertRaises(TwitchConfigurationError):
                    self.client.download_clip("clip", "10", destination, guard=lambda: False)
                resolve.assert_not_called()
                cdn.assert_not_called()
                guard = lambda: True
                self.assertEqual(self.client.download_clip("clip", "10", destination, guard=guard), destination)
                resolve.assert_called_once_with("clip", "10", "editor-token", guard=guard)
                request = cdn.call_args.args[0]
                self.assertEqual(request.full_url, SAMPLE_CDN_URL)
                self.assertFalse(request.has_header("Authorization"))
                self.assertEqual(destination.read_bytes(), SAMPLE_MP4_HEADER)
                self.assertEqual(list(Path(temp).iterdir()), [destination])

    def test_permission_revoked_during_transfer_leaves_no_partial_file(self):
        self.client._clip_editor_token = "editor-token"
        revoked = False

        class InFlightResponse(ClipResponse):
            def read1(self, size):
                nonlocal revoked
                revoked = True
                return super().read1(min(size, 16))

        def guard():
            if revoked:
                raise PermissionError("test permission revoked")
            return True

        with tempfile.TemporaryDirectory() as temp, \
                patch.object(self.client, "get_clip_download_url", return_value=SAMPLE_CDN_URL), \
                patch.object(self.client._opener, "open", return_value=InFlightResponse()):
            destination = Path(temp) / "clip.mp4"
            with self.assertRaisesRegex(PermissionError, "permission revoked"):
                self.client.download_clip("clip", "10", destination, guard=guard)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_download_does_not_overwrite_a_concurrent_destination(self):
        self.client._clip_editor_token = "editor-token"
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "clip.mp4"

            class WritingResponse(ClipResponse):
                def read1(self, size):
                    destination.write_bytes(b"other file")
                    return super().read1(size)

            with patch.object(self.client, "get_clip_download_url", return_value=SAMPLE_CDN_URL), \
                    patch.object(self.client._opener, "open", return_value=WritingResponse()), \
                    self.assertRaises(FileExistsError):
                self.client.download_clip("clip", "10", destination, guard=lambda: True)
            self.assertEqual(destination.read_bytes(), b"other file")
            self.assertEqual(list(Path(temp).iterdir()), [destination])

    def test_no_media_is_retained_on_malformed_truncated_or_oversized_response(self):
        self.client._clip_editor_token = "editor-token"
        responses = [
            ClipResponse(b"x" * 25),
            ClipResponse(headers={"Content-Type": "application/json"}),
            ClipResponse(headers={"Content-Type": "video/mp4", "Content-Length": "35"}),
            ClipResponse(headers={"Content-Type": "video/mp4", "Content-Length": "3000"}),
            ClipResponse(SAMPLE_MP4_HEADER + b"x" * 120, headers={"Content-Type": "video/mp4"}),
        ]
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(self.client, "get_clip_download_url", return_value=SAMPLE_CDN_URL):
            destination = Path(temp) / "clip.mp4"
            for response in responses:
                with patch.object(self.client._opener, "open", return_value=response), self.assertRaises(TwitchError):
                    self.client.download_clip("clip", "10", destination, guard=lambda: True, max_bytes=100)
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_download_failure_never_reports_signature_or_follows_redirect(self):
        self.client._clip_editor_token = "editor-token"
        redirect = HTTPError(SAMPLE_CDN_URL, 302, "redirect", {"Location": "https://example.test/"}, BytesIO())
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(self.client, "get_clip_download_url", return_value=SAMPLE_CDN_URL), \
                patch.object(self.client._opener, "open", side_effect=redirect) as cdn:
            destination = Path(temp) / "clip.mp4"
            with self.assertRaises(TwitchError) as held:
                self.client.download_clip("clip", "10", destination, guard=lambda: True)
            self.assertNotIn("sample", str(held.exception))
            cdn.assert_called_once()
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_rate_limit_retains_reset_without_printing_query_or_body(self):
        api = "https://api.twitch.tv/helix/clips/downloads?signature=sample"
        response = HTTPError(api, 429, "quiet", {"Retry-After": "30"}, BytesIO(b"token sample"))
        with patch.object(self.client._opener, "open", side_effect=response) as call:
            for _ in range(2):
                with self.assertRaises(TwitchError) as held:
                    self.client._request(api)
                self.assertEqual(held.exception.status, 429)
                self.assertGreater(held.exception.retry_after, 25)
                self.assertNotIn("sample", str(held.exception))
            call.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows user-scoped credential store")
    def test_configuration_validates_before_saving_and_loads_without_environment(self):
        app_environment = {"TWITCH_CLIENT_ID": "test-app", "TWITCH_CLIENT_SECRET": "test-protected-secret"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "twitch-oauth.dpapi"
            with patch("scripts.engine_twitch._credential_path", return_value=path):
                with patch.dict(os.environ, app_environment, clear=True), \
                        patch.object(TwitchClient, "resolve_creators", return_value={"xqc": "10"}):
                    with self.assertRaises(TwitchConfigurationError):
                        _configure(False, [])
                self.assertFalse(path.exists())
                with patch.dict(os.environ, app_environment, clear=True), \
                        patch.object(TwitchClient, "resolve_creators", return_value=dict.fromkeys(CREATORS)):
                    _configure(False, [])
                self.assertNotIn(b"test-protected-secret", path.read_bytes())
                with patch.dict(os.environ, {}, clear=True):
                    saved = TwitchClient.from_env()
                    self.assertEqual(saved.client_id, "test-app")
                    self.assertEqual(saved._secret, "test-protected-secret")

    @unittest.skipUnless(os.name == "nt", "Windows user-scoped credential store")
    def test_clip_editor_scope_is_required_before_replacing_stored_credentials(self):
        credentials = {"TWITCH_CLIENT_ID": "test-app", "TWITCH_CLIENT_SECRET": "test-protected-secret",
                       "TWITCH_CLIP_EDITOR_USER_TOKEN": "test-editor-token"}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, credentials, clear=True):
            path = Path(temp) / "twitch-oauth.dpapi"
            with patch("scripts.engine_twitch._credential_path", return_value=path), \
                    patch.object(TwitchClient, "resolve_creators", return_value=dict.fromkeys(CREATORS)), \
                    patch.object(TwitchClient, "_validate", return_value={"user_id": "10", "scopes": []}) as validate:
                with self.assertRaises(TwitchConfigurationError):
                    _configure(False, [], clip_download=True)
                self.assertFalse(path.exists())
                validate.return_value = {"user_id": "10", "scopes": ["channel:manage:clips"]}
                _configure(False, [], clip_download=True)
                self.assertNotIn(b"test-editor-token", path.read_bytes())
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(TwitchClient.from_env()._clip_editor_token, "test-editor-token")


class TwitchEventSubTests(unittest.TestCase):
    def test_gta_titles_do_not_include_other_games_or_countdowns(self):
        examples = [
            ("Grand Theft Auto VI", "First time in Vice City", True),
            ("Grand Theft Auto V", "GTA VI trailer later", False),
            ("Just Chatting", "GTA VI trailer reaction", True),
            ("Just Chatting", "Real talk | GTA VI trailer reaction later", False),
            ("Just Chatting", "GTA 6 trailer reaction tomorrow", False),
            ("Grand Theft Auto VI", "GTA VI leaked dev build", False),
        ]
        for game, title, expected in examples:
            with self.subTest(game=game, title=title):
                self.assertEqual(gta_vi_eligibility({"game_name": game, "title": title}), expected)

    def test_chat_only_aggregates_eligible_verified_channel_messages(self):
        monitor = EventSubMonitor(None, {"kaicenat": "10", "xqc": "20"}, chat_logins=["kaicenat"])
        monitor.set_eligible_chat(["kaicenat", "xqc"])
        monitor._set_active_chat({"kaicenat"})
        metadata = {"message_type": "notification"}
        event = {"broadcaster_user_id": "10", "message_id": "abc", "chatter_user_login": "someone",
                 "message": {"text": "LOL GTA VI", "fragments": [{"type": "emote"}]}}
        message = {"subscription": {"type": "channel.chat.message"}, "event": event}
        for _ in range(2):
            monitor._handle_message(metadata, message)
        monitor._handle_message(metadata, {**message, "event": {**event, "message_id": "def",
                                                                 "source_broadcaster_user_id": "20"}})
        snapshot = monitor.chat_snapshot("kaicenat")
        self.assertEqual(snapshot["messages"], 1)
        self.assertEqual(snapshot["hype_messages"], 1)
        self.assertEqual(snapshot["gta_messages"], 1)
        self.assertEqual(snapshot["emotes"], 1)
        self.assertIsInstance(next(iter(monitor._chat["kaicenat"].values())), Counter)
        self.assertNotIn("someone", str(monitor._chat))
        self.assertIsNone(monitor.chat_snapshot("xqc"))
        monitor._handle_message(metadata, {"subscription": {"type": "stream.offline"}, "event": event})
        self.assertTrue(monitor.changed.is_set())
        self.assertIsNone(monitor.chat_snapshot("kaicenat"))
        self.assertEqual(monitor.chat_timeline("kaicenat")[0]["count"], 1)

    def test_chat_timeline_aligns_by_twitch_timestamp_and_has_no_message_text(self):
        monitor = EventSubMonitor(None, {"kaicenat": "10"}, chat_logins=["kaicenat"])
        received = int(datetime(2026, 11, 19, 12, 30, 30, tzinfo=timezone.utc).timestamp())
        notified = "2026-11-19T12:30:23.912345678Z"
        event = {"broadcaster_user_id": "10", "message_id": "abc", "chatter_user_login": "someone",
                 "message": {"text": "LOL GTA VI sample", "fragments": [{"type": "emote"}]}}
        payload = {"subscription": {"type": "channel.chat.message"}, "event": event}
        with patch("scripts.engine_twitch.time.time", return_value=received):
            monitor.set_eligible_chat(["kaicenat"])
            monitor._set_active_chat({"kaicenat"})
            monitor._handle_message({"message_type": "notification", "message_timestamp": notified}, payload)
            snapshot = monitor.chat_timeline("kaicenat")
            self.assertEqual(snapshot, [{"at": received - 7, "count": 1, "reactions": 1, "emotes": 1, "gta_messages": 1}])
            self.assertNotIn("sample", str(snapshot))
            self.assertNotIn("someone", str(snapshot))

    def test_websocket_reconnect_keeps_subscriptions(self):
        monitor = EventSubMonitor(TwitchClient("test-app", app_token="test-token"), {"xqc": "20"})
        monitor._token = "test-user-token"
        welcome = {"metadata": {"message_type": "session_welcome"}, "payload": {"session": {"id": "sid"}}}
        reconnect_url = "wss://eventsub.wss.twitch.tv/ws?reconnect=sample"
        reconnect = {"metadata": {"message_type": "session_reconnect"},
                     "payload": {"session": {"reconnect_url": reconnect_url}}}
        arrivals = []

        class Socket:
            def __init__(self, name, events):
                self.name = name
                self.events = iter(events)

            def recv(self, timeout):
                event = next(self.events, None)
                if event is not None:
                    arrivals.append(self.name)
                    return json.dumps(event)
                if self.name == "replacement":
                    monitor._stop.set()
                raise TimeoutError

            def close(self):
                arrivals.append(f"closed {self.name}")

        def connect(url, **kwargs):
            self.assertIsNone(kwargs["ping_interval"])
            if url == EVENTSUB:
                return Socket("initial", [welcome, reconnect])
            self.assertEqual(url, reconnect_url)
            return Socket("replacement", [welcome])

        with patch.object(monitor.client, "_validate", return_value={"user_id": "1", "scopes": []}), \
                patch.object(monitor, "_subscribe") as subscribe:
            monitor._session(connect)
            subscribe.assert_called_once_with("sid", "1", [])
        self.assertEqual(arrivals, ["initial", "initial", "replacement", "closed initial", "closed replacement"])
