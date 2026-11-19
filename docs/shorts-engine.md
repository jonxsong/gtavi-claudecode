# GTA VIBING Shorts service

The service watches only [IShowSpeed](https://www.twitch.tv/ishowspeed), [Kai Cenat](https://www.twitch.tv/kaicenat), [JasonTheWeen](https://www.twitch.tv/jasontheween), [moistcr1tikal](https://www.twitch.tv/moistcr1tikal), and [xQc](https://www.twitch.tv/xqc). It accepts 7–30 second GTA VI streamer moments and publishes unattended to [@GTA-Vibing](https://www.youtube.com/@GTA-Vibing), channel `UCAtLKFamARYmLeTcwzA3akw`. The default threshold is 85 and the default rolling 24-hour limit is one public Short. A configured local publication time stays under control of the upload kill switch until promotion.

## First upload through Twitch and Studio

The signed-in Twitch and YouTube sites can handle the first clip before API setup. Once the chosen segment is cleared, use Twitch's Clip button on the live stream or VOD if the creator enables it. Download the landscape file from **Creator Dashboard → Content → Clips → Clips I've Created**, or use a cleared landscape export supplied by the creator. Keep the Twitch source link and the exact start time. [Twitch clip guide](https://help.twitch.tv/s/article/how-to-use-clips), [Twitch clip settings](https://help.twitch.tv/s/article/clips-settings)

Stack the streamer's facecam above a separate gameplay crop and keep both visible together throughout the vertical edit. Establish the premise in the first second, cut pauses, retain the reaction and payoff, and add accurate captions and original context. Use the full 7–30 second range as needed. Include cleared timed chat reactions and the exact campaign hashtag where applicable. Upload as private in [YouTube Studio](https://studio.youtube.com/channel/UCAtLKFamARYmLeTcwzA3akw), check playback and copyright results, then make it public. Submit an eligible campaign Short through the linked Content Rewards account within 30 minutes. [Content Rewards submissions](https://help.contentrewards.com/en/articles/15319814-submit-a-video)

Record the exact commercial clearance for the clip, including third-party gameplay and audio. The sponsor’s campaign brief establishes what to submit. Content Rewards leaves third-party clearance with the clipper, and Rockstar’s public fan-footage policy covers noncommercial use. [Content Rewards terms, section 17](https://contentrewards.com/terms), [Rockstar footage policy](https://support.rockstargames.com/articles/7bNaeoMFTV0iUDGhStTXvz/policy-on-posting-copyrighted-rockstar-games-material)

## Account connection

Run these commands in the repository. One-time Google consent and a Twitch registered application are required. An existing Studio or Twitch browser session does not supply API credentials.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\engine_twitch.py configure
.\.venv\Scripts\python.exe scripts\shorts_engine.py youtube-auth --accept-privacy
```

- Register a Twitch application and use its client ID and secret with `configure`. The interactive secret prompt saves an encrypted credential after Helix resolves all five users. Add `--clip-download` to store a creator/editor user token with `editor:manage:clips` or `channel:manage:clips`. Add `--eventsub --chat-channel LOGIN` for authorized EventSub chat, using a token with `user:read:chat`. Repeat `--chat-channel` for additional permitted channels. Renew user tokens when they expire. Helix reconciles state even when an EventSub token is unavailable. [Twitch application registration](https://dev.twitch.tv/docs/authentication/register-app/), [Twitch Clips API](https://dev.twitch.tv/docs/api/reference/clips)
- Enable YouTube Data API v3 and YouTube Analytics API in a Google Cloud project and create a **Desktop app** OAuth client. Put the downloaded JSON at `%LOCALAPPDATA%\gtavi-claudecode\youtube-client.json` before running `youtube-auth`. Read the privacy terms below before using `--accept-privacy`. Select GTA VIBING in Google’s browser consent flow. The service verifies the exact channel ID and encrypts the offline credential with Windows DPAPI. [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

## Start, stop, and inspect

```powershell
& .\scripts\shorts_service.ps1 -Action Install
& .\scripts\shorts_service.ps1 -Action Start
& .\scripts\shorts_service.ps1 -Action Status
& .\scripts\shorts_service.ps1 -Action Stop
.\.venv\Scripts\python.exe scripts\shorts_engine.py kill
.\.venv\Scripts\python.exe scripts\shorts_engine.py resume
.\.venv\Scripts\python.exe scripts\shorts_engine.py report
Get-Content .\.shorts-engine\engine.log -Tail 30
```

`Install` registers a Windows Task Scheduler job that runs under this user at sign-in and restarts on failure. `kill` stops new upload requests and public promotions, including retries. Existing videos stay in Studio. `resume` clears the switch. Use `run` for foreground operation, or `run --once` for a local smoke check when the scheduled task is stopped. `--state PATH` before the subcommand selects a separate state directory for testing.

`settings.json` in the state directory accepts `threshold` from 85 through 100, `daily_limit` from 1 through 10, and optional scoring `weights`. Install FFmpeg with `ffprobe` and `ffmpeg` in PATH or next to the venv Python. Whisper uses CUDA when installed and falls back to CPU. `python -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` adds the optional NVIDIA libraries inside the virtual environment. `SHORTS_OLLAMA_MODEL` optionally enables a local model for ranking on `127.0.0.1`. The free-space gate requires at least 3 GiB and limits each input file to 2 GiB and 20 minutes. Video files stay on disk for the operator's retention policy. Render scratch is temporary and logs rotate.

## Footage intake

The monitor records candidate timestamps and clip metadata. Jobs can import an existing native Twitch clip through the official download endpoint when the creator/editor token and an exact off-platform license are available. A licensed export delivered locally works too. Put a JSON manifest in `.shorts-engine/inbox/` using an atomic file move after writing is complete. Store supporting local evidence in `.shorts-engine/evidence/`. Put verified grants in `.shorts-engine/rights.json`, an object keyed by grant ID. Each grant needs these fields.

| Grant field | Required value |
| --- | --- |
| `status`, `creator`, `twitch_video_id` | `verified`, exact allowed Twitch login, and the numeric VOD ID as a string |
| `platform`, `channel_id` | `youtube_shorts` and `UCAtLKFamARYmLeTcwzA3akw` |
| `commercial`, `edit`, `upload`, `gta_vi` | `true` only when supported by permission evidence |
| `verified_by`, `verified_at`, `valid_from`, `valid_through` | Verifier and ISO timestamps with offsets. Omit `valid_through` only for a grant without an expiration. |
| `source_sha256` | Lowercase SHA-256 identifying a delivered local export. Optional exact hash pin for a native Twitch clip. |
| `twitch_clip_id`, `download` | For a native download, exact Twitch clip ID and `true` when the verified scope explicitly covers downloading it |
| `evidence` | Keys `creator`, `gameplay`, `audio`, and `source_delivery` with HTTPS references or local evidence filenames |
| `evidence.platform`, `evidence.chat` | Twitch off-platform reuse evidence for direct downloads, and independent clearance for any viewer quotes used on-screen |
| `campaign_id` | Exact known campaign ID when the permission comes from a campaign |

The manifest names `creator`, `twitch_video_id`, `license_id`, and `captured_at`. For a native Twitch import, set `twitch_clip_id`. The service checks the exact clip against its confirmed broadcaster, VOD and GTA VI category before getting the download URL, then uses Twitch's `vod_offset`. For a delivered local file, supply `source` as an inbox-relative path, `source_sha256`, and `duration_offset` as the exported segment's start in the VOD, in seconds. `publish_at` is optional and takes an offset-aware timestamp. `realistic_synthetic_media` must be set if YouTube requires disclosure.

The service approximates chat velocity in file-relative time from the Twitch stream start and VOD offset. Supply `source_started_at` when a more precise recording start is known. Optional `chat` can provide authorized, verified file-relative buckets with `offset`, `count`, and `reactions`. Timing inferred from EventSub delivery is for candidate ranking. For actual on-screen messages, the supplied viewer quote must have a verified timestamp in the source footage.

`windows` contains clips as `start` and `end` in file-relative seconds, each with an `editorial` object. Use `event`, `angle`, and `narration` for verified factual context and original commentary. `narration_audio`, if supplied, is an inbox-relative audio file with `narration_sha256`. Up to three genuine viewer quotes may appear as a timed Live chat overlay. Give each `editorial.viewer_messages` entry an exact file-relative `offset`, the original `text` and `reuse_evidence`. Include `editorial.policy.viewer_messages_safe: true` for screened, independently cleared text. Sender names, contact details and links are excluded. EventSub message bodies are never republished. You can add `hook_style`, `clip_category`, `narrative_structure`, and `commentary_format` for later comparison.

The renderer checks both views across sampled frames. It requires a stable visible face, motion in the facecam, and a readable gameplay crop that excludes the camera. A readable paused game screen or menu can fill the lower pane. The renderer can detect a clearly bounded webcam or accept `editorial.facecam_box` and `editorial.gameplay_box` from a verified source layout. Each optional box is `[x, y, width, height]` in fractions of the source frame. Supplied crops undergo the same validation. A clip with unavailable or unreadable framing is blocked.

`editorial.policy` records positive evidence for `gta_vi_segment`, `accurate_context`, `source_and_audio_cleared`, `original_analysis`, `youtube_community_guidelines`, `youtube_reused_content`, `youtube_ad_suitability`, and `no_major_story_spoilers`. It also records `made_for_kids: false`, `age_restricted: false`, `evidence` pointing to a local record or HTTPS reference and, when relevant, `campaign_requirements_met: true`. Only assert these when substantiated. The engine rechecks the grant, manifest, duration, hash, score and duplicate status before a clip can be uploaded. Changing or revoking a rights record blocks future actions on that job.

Start each Shorts title with the exact creator hashtag, a space, and the clip title. One example is `#KaiCenat The GTA 6 detail Kai missed`. Keep the entire title within 100 characters. The service applies the same rule to queued uploads before publication. Every upload gets description hashtags for the exact creator, `#GTA6`, `#GTAVI` and `#TwitchClips`. When `clip_category` maps to verified gameplay, reaction, discovery or news, the editor adds one matching topic tag. A paid job uses the exact campaign hashtag and includes a paid clipping disclosure. Campaign tags do not replace submitting the video. [YouTube title requirements](https://developers.google.com/youtube/data/reference/rest/v3/videos#snippet.title) · [YouTube hashtag guidance](https://support.google.com/youtube/answer/6390658)

## Custom Shorts covers

Each Short gets a separate portrait cover. The renderer selects a clear reaction from the finished clip, enlarges the streamer's face above the gameplay context, and adds a headline grounded in the edit. The cover is saved next to the MP4 as `<job_id>.cover.jpg`. The uploader waits for YouTube to process the private upload, then sets and verifies the custom portrait cover before publishing. [YouTube Shorts thumbnail API](https://developers.google.com/youtube/data/reference/rest/v3/thumbnails/set)

## Campaigns and revenue

- Kai's $10,000 pool covers the GTA VI marathon beginning November 18. He must remain the primary focus throughout the edit. The caption or overlay must name him, the description must include `#KaiCenat`, and the rules exclude logos and negative or false framing. The member page showed at least 1,000 views per submission and 750,000 qualifying views total. [Kai Cenat campaign](https://contentrewards.com/c/campaigns/1d2bcb00-c904-415f-aa82-f95b96baad86), [Sponsor rule sheet](https://docs.google.com/document/d/1z3h2gDIdr2LFgDrSkXf6rIIA72y62nluKHdWgki5iG0/edit)
- Jason's $9,000 pool names Jason as the primary focus, requires `#JasonTheWeen` and showed a 700,000 qualifying-view threshold. The signed-in brief listed November 30 as the closing date. The sponsor requires at least 50% of the audience from its five listed countries. [Jason campaign](https://contentrewards.com/c/campaigns/0b178e3c-fbd3-4294-8ded-394e3da9c62f), [Reach requirements](https://reachclipping.com/JasonTheWeen)
- Both pools split verified views after the campaign ends. Joining a pool does not establish that all gameplay, music, source exports and other third-party material are cleared for commercial reuse. Paid jobs therefore require both asset-level evidence and a working submission path. Content Rewards requires a connected account and a submission within 30 minutes of posting. Its documented creator workflow is through its app, so the unattended engine holds campaign jobs after local production. [Content Rewards submissions](https://help.contentrewards.com/en/articles/15319814-submit-a-video), [Rockstar footage policy](https://support.rockstargames.com/articles/7bNaeoMFTV0iUDGhStTXvz/policy-on-posting-copyrighted-rockstar-games-material)

After publication, `report` lists snapshots at 1, 6, 24, 72 and 168 hours, along with their actual observation time. YouTube Analytics reports can arrive days later. Unavailable metrics stay unavailable. Use Studio for qualified Shorts views toward YPP, the viewed-versus-swiped-away card, Content ID checks and the channel's Earn status. API statistics are displayed as returned, separate from local editorial data and actual cash. A cash ledger entry uses `record-revenue KIND TRANSACTION_REFERENCE USD_AMOUNT EVIDENCE` where `KIND` is `youtube`, `campaign`, `sponsorship`, `affiliate` or `cost`. Record settled cash received, or an actual cost paid. Net uses only those entries. [YouTube Analytics](https://developers.google.com/youtube/analytics/data_model), [YouTube Partner Program](https://support.google.com/youtube/answer/72851)

## YouTube API privacy and controls

Using the YouTube integration binds you to the [YouTube Terms of Service](https://www.youtube.com/t/terms). Google handles the account sign-in and consent under its [privacy policy](https://policies.google.com/privacy). The local client uses the official YouTube Data and Analytics APIs. It transmits eligible clips, cover images, metadata and visibility changes to YouTube. It reads the selected channel's identifier and subscriber count, its recent uploads, publication status and authorized performance and revenue metrics. The local SQLite database holds job records, upload session state and dated API snapshots. API snapshots are retained for up to 29 days while access is valid. Local program logs redact tokens, and the client does not collect Google passwords or use browser cookies. The service does not share this data with other services. Twitch metadata expires within 24 hours and chat bodies are never stored.

Run `youtube-revoke` to pause future uploads, revoke the stored Google grant and purge local API snapshots without changing videos in Studio. You can also revoke in [Google Account connections](https://security.google.com/settings/security/permissions). Direct questions to the maintainer in a private channel or via the project's [repository](https://github.com/jonxsong/gtavi-claudecode). Keep credentials and private evidence out of public issues.
