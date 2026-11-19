# Repository audit

The 16 skills are Markdown instructions for Claude Code. This audit checked their scripts and dependencies before the unattended entry point was implemented. The stock plugin remains usable with its original manual publishing workflow. `scripts/shorts_engine.py` runs the separate GTA VIBING workflow.

| Skill | Existing implementation | Engine reuse and limits |
| --- | --- | --- |
| `setup` | Documents dependency installation and a manual workspace template. | Uses the existing Python environment and FFmpeg. The Windows launcher and local state replace the setup questionnaire. |
| `campaign-finder` | Browser research instructions. | The two identified campaign briefs were checked. Campaign submission and rule updates still lack a supported unattended integration. |
| `trend-radar` | `trends.py` ranks results from yt-dlp. | Kept for interactive research. The service only looks at the five Twitch creators. |
| `stream-finder` | `streams.py` searches general streams across platforms. The keyless route uses Twitch web GraphQL. | Uses official Helix with an exact Twitch whitelist. |
| `moment-finder` | `moments.py` extracts RMS, transcribes and ranks peaks using chat and transcript cues. | Reuses audio, transcript and signal concepts with weighted scoring, OpenCV cut signals and duplicate fingerprints. It starts from licensed local input. |
| `live-mode` | `live.py` segments recordings through yt-dlp/FFmpeg and cuts around chat spikes. The legacy paths use anonymous IRC and page replay data. | Uses authorized EventSub signal buckets around source times, records candidates, and imports exact licensed Twitch clips with Twitch's official download endpoint. |
| `twitch-reaction` | `twitch.py` offers unofficial GraphQL and chat features. `facecam.py` estimates stable camera framing. | Reuses facecam framing. The unattended network path uses Helix and EventSub only. |
| `clip-cutter` | `cut.py` builds vertical FFmpeg layouts and ASS captions. | Reuses framing and subtitle tools with original narration, H.264/AAC, loudness controls and output checks. |
| `hook-writer` | Editorial examples and title rules in Markdown. | Generates ranked options from an evidence-backed event and supplied editorial angle. |
| `rule-check` | `check.py` checks basic format and simple optional campaign keys. It cannot infer policy or clearance from a video. | Reuses its banned-topic check and adds format, media, attribution, evidence and strict destination checks. |
| `multilang` | `translate.py` calls translation and speech services to produce localized copies. | Left available for interactive use. The unattended engine produces one edit per source package. |
| `questions-to-posts` | `questions.py` uses Reddit JSON and provided facts or comments. | Outside the Twitch monitoring scope. |
| `secrets-map` | `secrets_map.py` proposes entries from transcripts and tracks manual verification in CSV. | Outside unattended publishing. Transcript guesses are not used as facts. |
| `cheat-codes` | Markdown reference of reported and unconfirmed codes. | Outside the editorial scope. |
| `playbook` | Manual multi-platform daily plan with approval and publication by the user. | Reuses attribution and originality principles. The engine has no per-video approval mode. |
| `learning-loop` | Manual CSV snapshots plus `payouts.py` arithmetic. | Stores real API snapshots and a distinct cash ledger. Reports recurring patterns when there are enough samples. |

The repository originally had no official YouTube upload or Analytics client, no entitlement check, no retryable upload queue, and no persistent Windows service. The new service uses an SQLite queue and official API clients. Outstanding dependencies include one-time Twitch and YouTube credentials and verified video-level commercial rights. The engine accepts only 7–30 second GTA VI moments from the five Twitch creators in the runbook.
