"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import re
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at",
                                                                         "publish_at", "progress", "error")}
        jobs.save(job)


def _tomorrow(t=None):
    lt = time.localtime((t or time.time()) + 86400)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day


def _youtube_reset(t=None):
    """Five minutes after YouTube's daily allowance renews: midnight Pacific time, which is 08:00 UTC.
    shortcut: in US summer time it renews an hour earlier (07:00 UTC), so then this waits an hour longer than needed."""
    t = t or time.time()
    at = t - t % 86400 + 8 * 3600 + 300
    return at if at > t else at + 86400


def _when(p):
    """When a post goes (or went) public: a scheduled YouTube post's publish time, else when it was posted or is due."""
    return p.get("publish_at") or p.get("posted_at") or p["due_at"]


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((_when(p) for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(_when(p)) == _day(t)) < cap:
            break
        t = _tomorrow(t)
    return t


class _Tomorrow(Exception):
    """A limit for today is reached: the post waits for tomorrow morning, and it does not count as a failed try."""


def _youtube_today() -> int:
    """YouTube uploads made today from this machine, all channels together (YouTube's allowance is per Google project)."""
    today = _day(time.time())
    return sum(1 for p in _posts if p["platform"] == "youtube" and p["status"] == "posted" and _day(p["posted_at"]) == today)


def enqueue(job, clip, account_ids, now=False, at=None) -> list:
    """Queue a clip for these accounts. `now`: straight away. `at`: at the time you picked (YouTube: uploaded now and
    made public by YouTube then). Neither: the creator's next free slot (gap between posts, daily cap)."""
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            slot = at or (time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"]))
            # Scheduled on YouTube: upload straight away as private; YouTube itself makes it public at the slot.
            ahead = acc["platform"] == "youtube" and (bool(at) or (c["privacy"] == "scheduled" and not now))
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": time.time() if ahead else slot, "publish_at": slot if ahead else None, "progress": 0,
                 "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def hashtags(clip, last: str = "") -> str:
    """The AI's hashtags, then its search tags as hashtags ("epic clutch" -> #EpicClutch), without repeats. At most 15
    in all, `last` included: YouTube ignores every hashtag in a description that has more than 15."""
    seen, out = {last.lower()}, []
    for t in [*clip.get("hashtags", []), *clip.get("tags", [])]:
        words = str(t).split()
        h = re.sub(r"\W", "", "".join(w[:1].upper() + w[1:] for w in words) if len(words) > 1 else str(t))
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append("#" + h)
    return " ".join(out[:14 if last else 15] + (["#" + last] if last else []))


def _caption(clip, creator, last=""):
    return "\n\n".join(x for x in (clip.get("description", ""), hashtags(clip, last), creator.get("post_footer", "")) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        # "scheduled" whose time has come (or on TikTok, which has no scheduling here) simply means public
        # A post with a go-public time was asked to be public then: if that time has already passed, it goes public now.
        privacy = "public" if creator["privacy"] == "scheduled" or p.get("publish_at") else creator["privacy"]
        at = None
        if acc["platform"] == "youtube":
            if _youtube_today() >= config.YT_DAILY_LIMIT:
                raise _Tomorrow(f"Today's YouTube upload limit ({config.YT_DAILY_LIMIT}, set by CBC_YT_DAILY_LIMIT) is reached.")
            if (p.get("publish_at") or 0) > time.time() + 300:
                at = p["publish_at"]
            rid, url = accounts.youtube_upload(acc, path, title, _caption(clip, creator, "Shorts"), clip.get("tags") or clip.get("hashtags", []),
                                               privacy, at, progress=lambda n: (p.update(progress=n), _mirror(p)))
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", privacy)
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None, publish_at=at)
        creators.log(p["creator"], f"Uploaded to {acc['name']}, goes public {time.strftime('%d %b %H:%M', time.localtime(at))}: {title}"
                     if at else f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["error"] = str(e)[-300:]
        if isinstance(e, _Tomorrow):
            p.update(status="scheduled", due_at=_tomorrow(), progress=0, error=str(e) + " Trying again tomorrow morning.")
        elif "quotaExceeded" in str(e) or "uploadLimitExceeded" in str(e):
            # YouTube refused more uploads today: retrying sooner would only fail again, so wait for its renewal
            due = _youtube_reset()
            why = "quotaExceeded" if "quotaExceeded" in str(e) else "uploadLimitExceeded"
            p.update(status="scheduled", due_at=due, progress=0,
                     error=f"YouTube refused more uploads today ({why}). Trying again at {time.strftime('%a %H:%M', time.localtime(due))}, "
                           "when YouTube's daily allowance renews.")
        else:
            p["tries"] += 1
            if p["tries"] < 3 and clip and acc:
                p.update(status="scheduled", due_at=time.time() + 600, progress=0)  # try again in 10 minutes
            else:
                p["status"] = "failed"
                creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
                alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None, progress=0)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["CrazeBingoCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
