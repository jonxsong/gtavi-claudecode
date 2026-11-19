"""Performance snapshots and verified cash for the Shorts service."""

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from engine_state import json_text, now, timestamp


MILESTONES = (1, 6, 24, 72, 168)
MONEY = ("youtube", "campaign", "sponsorship", "affiliate", "cost")


def capture(state, youtube):
    from engine_youtube import YouTubeError

    pacific = ZoneInfo("America/Los_Angeles")
    with state.connect() as db:
        cutoff = (now() - timedelta(days=29)).isoformat()
        db.execute("DELETE FROM snapshots WHERE observed<?", (cutoff,))
        entries = db.execute("""SELECT id,video_id,published FROM jobs WHERE state='published' AND published>=? AND
                                (SELECT COUNT(*) FROM snapshots WHERE job_id=jobs.id) < ?""",
                             (cutoff, len(MILESTONES))).fetchall()
        seen = {(row["job_id"], row["hours"]) for row in db.execute("SELECT job_id,hours FROM snapshots")}
    for job in entries:
        published = timestamp(job["published"])
        observed = now()
        due = [hours for hours in MILESTONES if (job["id"], hours) not in seen
               and observed >= published + timedelta(hours=hours)]
        if not due:
            continue
        try:
            video = youtube.video(job["video_id"])
            channel_stats = youtube.channel().get("statistics", {})
        except YouTubeError:
            continue
        results = {"videoStatistics": video.get("statistics", {}),
                   "subscriberCount": channel_stats.get("subscriberCount")}
        if due[-1] >= 72:
            try:
                results["youtubeAnalytics"] = youtube.analytics(job["video_id"], published.astimezone(pacific).date(),
                                                                observed.astimezone(pacific).date())
            except YouTubeError as error:
                # Direct counts are independently observable even when a report is unavailable.
                results["analyticsUnavailable"] = type(error).__name__
        with state.connect() as db:
            for hours in due[:-1]:
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)", (job["id"], hours,
                           observed.isoformat(), json_text({"missed": "service did not sample at this milestone"})))
            db.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)",
                       (job["id"], due[-1], observed.isoformat(), json_text(results)))


def record_money(state, kind, reference, amount, evidence):
    try:
        value = Decimal(amount)
        if not value.is_finite() or value <= 0 or value > Decimal("10000000") or value * 100 % 1:
            raise ValueError("amount must be a positive USD amount with at most two decimals")
    except InvalidOperation as exc:
        raise ValueError("invalid USD amount") from exc
    if not reference.strip() or not evidence.strip():
        raise ValueError("transaction reference and evidence are required")
    with state.connect() as db:
        db.execute("INSERT INTO money VALUES (?,?,?,?,?)",
                   (kind, reference.strip(), now().isoformat(), int(value * 100), evidence.strip()))


def report(state):
    with state.connect() as db:
        totals = dict(db.execute("SELECT kind,SUM(cents) FROM money GROUP BY kind"))
        observations = db.execute("""SELECT j.video_id,j.manifest,j.candidate,j.metadata,s.hours,s.observed,s.data
                                    FROM snapshots s JOIN jobs j ON j.id=s.job_id WHERE s.observed>=?
                                    ORDER BY j.updated DESC,s.hours LIMIT 250""",
                                  ((now() - timedelta(days=29)).isoformat(),)).fetchall()
    snapshots = []
    for item in observations:
        metadata = json.loads(item["metadata"])
        editorial = {key: metadata[key] for key in ("title", "hook_style", "clip_category", "narrative_structure",
                                                  "commentary_format") if key in metadata}
        if item["candidate"]:
            candidate = json.loads(item["candidate"])
            editorial.update(creator=json.loads(item["manifest"])["creator"], score=candidate["score"],
                             duration_seconds=round(candidate["end"] - candidate["start"], 3))
        snapshots.append({"video_id": item["video_id"], "hours": item["hours"], "observed": item["observed"],
                          "local_editorial_data": editorial, **json.loads(item["data"])})
    received = sum(totals.get(kind, 0) for kind in MONEY if kind != "cost")
    costs = totals.get("cost", 0)
    return {"verified_cash_usd": {kind: f"{totals.get(kind, 0) / 100:.2f}" for kind in MONEY},
            "verified_net_cash_usd": f"{(received - costs) / 100:.2f}", "snapshots": snapshots,
            "weights": "fixed; no model training or derived scoring from YouTube API statistics"}
