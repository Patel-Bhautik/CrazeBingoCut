"""One-click Connect for YouTube, Instagram and TikTok, and posting to each. The platforms are replaced by
stand-ins answering the way their documentation says, so nothing leaves this machine."""
import json, os, sys, tempfile, time
from urllib.parse import parse_qs, urlparse
os.environ["CBC_DATA"] = tempfile.mkdtemp(prefix="cbc-connect-")
os.environ.update(DATABASE_URL="", CBC_ACCOUNTS="1", CBC_PUBLIC_URL="https://clips.example.com", CBC_VERIFY_EMAIL="0",
                  GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="", GOOGLE_CLIENT_FILE="", CBC_IG_APP_ID="", CBC_IG_APP_SECRET="",
                  CBC_TIKTOK_CLIENT_KEY="", CBC_TIKTOK_CLIENT_SECRET="")
sys.path.insert(0, ".")
from pathlib import Path
import sqlalchemy as sa
from fastapi.testclient import TestClient
from studio import accounts, auth, config, ctx, db, server
db.init()
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

BASE = "https://clips.example.com"
ORIGIN = {"Origin": BASE}
def person(email, admin=False):
    u, _ = auth._create(email, email.split("@")[0], password_hash="x")
    if admin:
        with db.engine.begin() as c:
            c.execute(db.users.update().where(db.users.c.id == u.id).values(is_admin=True))
    cl = TestClient(server.app, base_url=BASE, follow_redirects=False)
    cl.cookies.set(auth.COOKIE, auth.start_session(u.id))
    return u, cl
boss, a = person("boss@example.com", admin=True)
user, b = person("user@example.com")

# ---- nothing is set up: the buttons say so, and only an admin may set it up
st = b.get("/api/status").json()
check("nothing can be connected before setup", st["connect"], {"youtube": False, "instagram": False, "tiktok": False})
check("a normal user cannot see the setup", b.get("/api/apps").status_code, 404)
check("...or change it", b.put("/api/apps", json={"platform": "tiktok", "client_id": "abcd", "client_secret": "x"}, headers=ORIGIN).status_code, 404)
r = b.get("/oauth/instagram/start")
check("Connect before setup comes back with a reason", (r.status_code, "connect_error" in r.headers.get("location", "")), (303, True))
check("setup from another website is refused", a.put("/api/apps", json={"platform": "tiktok", "client_id": "abcd1234", "client_secret": "s"},
                                                    headers={"Origin": "https://evil.example"}).status_code, 403)
check("a bad id is refused", a.put("/api/apps", json={"platform": "tiktok", "client_id": "a b<", "client_secret": "s"}, headers=ORIGIN).status_code, 400)
for pf, cid in (("youtube", "g-client-123.apps"), ("instagram", "1234567890"), ("tiktok", "awtiktokkey123")):
    r = a.put("/api/apps", json={"platform": pf, "client_id": cid, "client_secret": f"{pf}-secret-value"}, headers=ORIGIN)
    check(f"{pf} app saved", (r.status_code, r.json()[pf]["ready"]), (200, True))
apps = a.get("/api/apps").json()
check("secrets are never sent back", any("secret-value" in json.dumps(v) for v in apps.values()), False)
check("each platform shows the address to register", apps["tiktok"]["redirect"], BASE + "/oauth/tiktok/callback")
r = a.put("/api/apps", json={"platform": "tiktok", "client_id": "", "client_secret": "tiktok-secret-value2"}, headers=ORIGIN)
check("a new secret alone keeps the saved id", accounts.app_keys("tiktok"), ("awtiktokkey123", "tiktok-secret-value2"))
check("everyone can connect now", b.get("/api/status").json()["connect"], {"youtube": True, "instagram": True, "tiktok": True})

# ---- the platforms, as stand-ins
class R:
    def __init__(self, code, data=None, headers=None, text=None):
        self.status_code, self._d, self.headers = code, data, headers or {}
        self.text = text if text is not None else json.dumps(data)
    def json(self):
        if self._d is None:
            raise ValueError
        return self._d
calls, uploads, tt = [], [], {"audited": False, "status": 0}
def post(url, data=None, json=None, headers=None, params=None, timeout=None, content=None):
    calls.append(("POST", url, data or json, headers or {}))
    if url == "https://oauth2.googleapis.com/token":
        return R(200, {"access_token": "yt-access", "refresh_token": "yt-refresh", "expires_in": 3600})
    if url == "https://api.instagram.com/oauth/access_token":
        if data["code"] != "igcode":
            return R(400, {"error_message": "bad code"})
        return R(200, {"data": [{"access_token": "ig-short", "user_id": "17841", "permissions": "instagram_business_basic,instagram_business_content_publish"}]})
    if url.endswith("/oauth/token/"):
        if data.get("grant_type") == "refresh_token":
            return R(200, {"access_token": "tt-access-2", "refresh_token": "tt-refresh-2", "expires_in": 7200, "open_id": "oid", "scope": "user.info.basic,video.publish"})
        return R(200, {"access_token": "tt-access", "refresh_token": "tt-refresh", "expires_in": 7200, "open_id": "oid", "scope": "user.info.basic,video.publish"})
    if url.endswith("/creator_info/query/"):
        return R(200, {"data": {"creator_username": "streamerclips", "creator_nickname": "Streamer Clips",
                                "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]}, "error": {"code": "ok"}})
    if url.endswith("/post/publish/video/init/"):
        lv = json["post_info"]["privacy_level"]
        tt["init"] = json
        if lv != "SELF_ONLY" and not tt["audited"]:
            return R(403, {"error": {"code": "unaudited_client_can_only_post_to_private_accounts", "message": "private only"}})
        return R(200, {"data": {"publish_id": "v_pub_1", "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=1"}, "error": {"code": "ok"}})
    if url.endswith("/post/publish/status/fetch/"):
        tt["status"] += 1
        if tt["status"] < 2:
            return R(200, {"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})
        return R(200, {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300] if tt["audited"] else []}, "error": {"code": "ok"}})
    if url.endswith("/17841/media"):
        return R(200, {"id": "container1"})
    if url.endswith("/17841/media_publish"):
        return R(200, {"id": "media1"})
    return R(404, {})
def get(url, params=None, headers=None, timeout=None):
    calls.append(("GET", url, params, headers or {}))
    if url.endswith("/youtube/v3/channels"):
        return R(200, {"items": [{"id": "UC123", "snippet": {"title": "My Channel"}}]})
    if url == "https://graph.instagram.com/access_token":
        return R(200, {"access_token": "ig-long", "token_type": "bearer", "expires_in": 5183944})
    if url == "https://graph.instagram.com/me":
        return R(200, {"user_id": "17841", "username": "clipper", "account_type": "MEDIA_CREATOR"})
    if url.endswith("/container1"):
        return R(200, {"status_code": "FINISHED"})
    if url.endswith("/media1"):
        return R(200, {"permalink": "https://www.instagram.com/reel/abc/"})
    return R(404, {})
def put(url, content=None, headers=None, timeout=None):
    uploads.append((url, len(content), headers))
    return R(201, None, text="")
accounts.httpx.post, accounts.httpx.get, accounts.httpx.put = post, get, put
accounts.time.sleep = lambda s: None

# ---- connecting: the button goes to the platform's own page, and back
def connect(cl, pf, code="code1"):
    r = cl.get(f"/oauth/{pf}/start")
    loc = urlparse(r.headers["location"])
    state = parse_qs(loc.query)["state"][0]
    back = cl.get(f"/oauth/{pf}/callback", params={"code": code, "state": state})
    return loc, back
loc, back = connect(b, "youtube")
check("YouTube: Google's page, asking to upload", (loc.netloc, "youtube.upload" in parse_qs(loc.query)["scope"][0]), ("accounts.google.com", True))
check("YouTube: back on Connected apps, connected", (back.status_code, back.headers["location"].startswith("/app/settings/apps?connected=youtube")), (303, True))
loc, back = connect(b, "instagram", "igcode#_")
q = parse_qs(loc.query)
check("Instagram: Instagram's own page, with publishing permission", (loc.netloc, q["client_id"][0], "instagram_business_content_publish" in q["scope"][0]),
      ("www.instagram.com", "1234567890", True))
check("Instagram: connected, with the #_ Instagram adds to the code removed", "connected=instagram" in back.headers["location"])
loc, back = connect(b, "tiktok")
q = parse_qs(loc.query)
check("TikTok: TikTok's own page, asking to post", (loc.netloc, q["client_key"][0], q["scope"][0]), ("www.tiktok.com", "awtiktokkey123", "user.info.basic,video.publish"))
check("TikTok: connected", "connected=tiktok" in back.headers["location"])
accs = {x["platform"]: x for x in b.get("/api/accounts").json()}
check("all three accounts are listed with their names", (accs["youtube"]["name"], accs["instagram"]["name"], accs["tiktok"]["name"]),
      ("My Channel", "@clipper", "@streamerclips"))
check("tokens never reach the page", any(t in b.get("/api/accounts").text for t in ("ig-long", "tt-access", "yt-refresh")), False)
check("someone else's accounts are not mine", a.get("/api/accounts").json(), [])
r = b.get("/oauth/tiktok/callback", params={"code": "x", "state": "made-up"})
check("a made-up return is refused", "connect_error" in r.headers["location"])
r = b.get("/oauth/tiktok/callback", params={"error": "access_denied"})
check("cancelling on the platform says so", "cancelled" in r.headers["location"])
loc = urlparse(a.get("/oauth/tiktok/start").headers["location"])
r = b.get("/oauth/tiktok/callback", params={"code": "c", "state": parse_qs(loc.query)["state"][0]})
check("one person's sign-in cannot be finished by another", "connect_error" in r.headers["location"])
check("an unknown platform does not exist", b.get("/oauth/myspace/start").status_code, 404)

# ---- the one-hour links Instagram fetches clips from
with ctx.as_user(user.id):
    jd = config.user_dir(user.id) / "jobs" / "job-12345678" / "clips"
jd.mkdir(parents=True)
(jd.parent / "job.json").write_text(json.dumps({"id": "job-12345678", "user": user.id, "dir": str(jd.parent), "clips": [], "status": "done",
                                                "creator": None, "created": time.time(), "options": {}, "source": {"type": "file"}}), encoding="utf-8")
(jd / "clip01.mp4").write_bytes(b"\x00" * 6_000)
from studio import jobs
jobs.load_user(user.id)
with ctx.as_user(user.id):
    link = accounts.clip_link("job-12345678", "clip01.mp4")
anon = TestClient(server.app, base_url=BASE)
path = urlparse(link).path
check("a signed link serves the clip to anyone, no sign-in", (anon.get(path).status_code, len(anon.get(path).content)), (200, 6000))
bad = path.replace(".mp4", "x.mp4")
check("a changed link does not", anon.get(bad).status_code, 404)
check("a made-up link does not", anon.get("/pub/abc.def.mp4").status_code, 404)
with ctx.as_user(user.id):
    old = accounts.clip_link("job-12345678", "clip01.mp4", seconds=-5)
check("an expired link does not", anon.get(urlparse(old).path).status_code, 404)

# ---- posting
with ctx.as_user(user.id):
    ig, ttacc = accounts.get(accs["instagram"]["id"]), accounts.get(accs["tiktok"]["id"])
    calls.clear()
    rid, url = accounts.instagram_upload(ig, jd / "clip01.mp4", "My caption", "job-12345678")
    made = next(c for c in calls if c[1].endswith("/17841/media"))
    check("Instagram: a Reel from the signed link", (made[2]["media_type"], made[2]["video_url"].startswith(BASE + "/pub/")), ("REELS", True))
    check("Instagram: published, with its link", (rid, url), ("media1", "https://www.instagram.com/reel/abc/"))

    uploads.clear(); tt.update(audited=False, status=0)
    rid, url = accounts.tiktok_upload(ttacc, jd / "clip01.mp4", "My caption #fyp", "public")
    check("TikTok: an app not yet reviewed posts privately instead of failing", (tt["init"]["post_info"]["privacy_level"], url), ("SELF_ONLY", ""))
    check("TikTok: the whole file in one piece, with the right range", (len(uploads), uploads[0][2]["Content-Range"]), (1, "bytes 0-5999/6000"))
    check("TikTok: the account says why posts are private", "private" in accounts.get(ttacc["id"]).get("detail", ""))
    tt.update(audited=True, status=0)
    rid, url = accounts.tiktok_upload(ttacc, jd / "clip01.mp4", "My caption", "public")
    check("TikTok: once reviewed, public, with the video's link", (tt["init"]["post_info"]["privacy_level"], url),
          ("PUBLIC_TO_EVERYONE", "https://www.tiktok.com/@streamerclips/video/7300"))
    check("TikTok: big files go in pieces, the last taking the rest", accounts._tt_chunks(150_000_123), (10_000_000, 15))
    acc = accounts.get(ttacc["id"]); acc["expires"] = 0; accounts._put(acc)
    accounts._tt_token(accounts.get(ttacc["id"]))
    check("TikTok: an old sign-in is renewed, and the new refresh token kept", accounts.get(ttacc["id"])["refresh_token"], "tt-refresh-2")

# ---- YouTube Shorts: what YouTube is told, the queue's rules, and its limits (no Google libraries needed)
from studio import creators, posting
meta = accounts._yt_meta("A <b>title</b>", "Text", ["clutch play", "fps,game", ""], "weird")
check("YouTube: an unknown visibility becomes private", meta["status"]["privacyStatus"], "private")
check("YouTube: tags cleaned, empty ones dropped", (meta["snippet"]["tags"], meta["snippet"]["title"]), (["clutch play", "fpsgame"], "A btitle/b"))
check("YouTube: public when asked", accounts._yt_meta("t", "d", [], "public")["status"]["privacyStatus"], "public")
meta = accounts._yt_meta("t", "d", [], "public", publish_at=1893456000)
check("YouTube: scheduled = uploaded private, made public by YouTube at the time",
      (meta["status"]["privacyStatus"], meta["status"]["publishAt"]), ("private", "2030-01-01T00:00:00Z"))
check("YouTube: hundreds of tags are cut to fit", len(",".join(accounts._yt_meta("t", "d", ["x" * 30] * 40, "private")["snippet"]["tags"])) <= 450)
check("posts are private by default", (creators.DEFAULTS["privacy"], creators._clean({"privacy": "bogus"})["privacy"],
                                       creators._clean({"privacy": "scheduled"})["privacy"]), ("private", "private", "scheduled"))

ytid = accs["youtube"]["id"]
with ctx.as_user(user.id):
    cr = creators.create({"name": "Shorts Tester", "privacy": "scheduled", "post_gap_min": 60, "daily_cap": 5, "accounts": [ytid]})
    jd2 = config.user_dir(user.id) / "jobs" / "job-shorts01" / "clips"
jd2.mkdir(parents=True)
mk = lambda cid: {"id": cid, "status": "done", "file": f"{cid}.mp4", "title": f"Big play {cid}", "description": "What happens.",
                  "hashtags": ["gaming"], "tags": ["epic clutch"], "score": 90}
(jd2.parent / "job.json").write_text(json.dumps({"id": "job-shorts01", "user": user.id, "dir": str(jd2.parent), "status": "done",
                                                 "clips": [mk("clip01"), mk("clip02"), mk("clip03")], "creator": cr["id"], "created": time.time(),
                                                 "options": {}, "source": {"type": "file"}}), encoding="utf-8")
jobs.load_user(user.id)
sent, real_upload = [], accounts.youtube_upload
def fake_upload(acc, path, title, desc, tags, privacy, publish_at=None, progress=None):
    sent.append({"title": title, "desc": desc, "tags": tags, "privacy": privacy, "at": publish_at})
    if fake_upload.fail:
        raise RuntimeError(fake_upload.fail)
    progress(50)
    return "ytvid1", "https://www.youtube.com/shorts/ytvid1"
fake_upload.fail = None
accounts.youtube_upload = fake_upload
with ctx.as_user(user.id):
    job = jobs.get("job-shorts01")
    now = time.time()
    p1, = posting.enqueue(job, job["clips"][0], [ytid])
    p2, = posting.enqueue(job, job["clips"][1], [ytid])
    check("scheduled: uploads go now, going public one gap apart",
          (p1["due_at"] <= now + 5, p2["due_at"] <= now + 5, p2["publish_at"] - p1["publish_at"] >= 3590), (True, True, True))
    check("the same clip is never queued twice for one channel", posting.enqueue(job, job["clips"][0], [ytid]), [])
    posting._post(p2)
    check("scheduled: uploaded with its go-public time, the AI's search tags, and #Shorts",
          (p2["status"], sent[-1]["at"] == p2["publish_at"], sent[-1]["tags"], sent[-1]["desc"].endswith("#Shorts")),
          ("posted", True, ["epic clutch"], True))
    check("the description carries the AI's hashtags and search tags as hashtags, then #Shorts",
          sent[-1]["desc"], "What happens.\n\n#gaming #EpicClutch #Shorts")
    many = posting.hashtags({"hashtags": ["Gaming", "fps", "shorts"], "tags": ["gaming", "big clutch"] + [f"tag {i}" for i in range(30)]}, "Shorts")
    check("at most 15 hashtags, no repeats in any case, #Shorts kept last (YouTube ignores them all past 15)",
          (len(many.split()), many.split()[:3], many.split()[-1], many.lower().count("#gaming"), many.lower().count("#shorts")),
          (15, ["#Gaming", "#fps", "#BigClutch"], "#Shorts", 1, 1))
    check("upload progress reaches the clip card", (p2["progress"], jobs.get("job-shorts01")["clips"][1]["posts"][p2["id"]]["progress"]), (50, 50))
    fake_upload.fail = '<HttpError 403 "The request cannot be completed because you have exceeded your quota." "reason": "quotaExceeded">'
    posting._post(p1)
    check("YouTube refuses more uploads today: waits until its allowance renews, not counted as a failed try, reason shown",
          (p1["status"], p1["tries"], p1["due_at"] == posting._youtube_reset(), "quotaExceeded" in p1["error"]), ("scheduled", 0, True, True))
    check("YouTube's allowance renews just after midnight Pacific (08:00 UTC), within the next day",
          [time.strftime("%H:%M", time.gmtime(posting._youtube_reset(x))) for x in (1700000000, 1700000000 + 3 * 3600)]
          + [0 < posting._youtube_reset() - time.time() <= 86400], ["08:05", "08:05", True])
    fake_upload.fail = "Uploading to YouTube failed (500)"
    posting._post(p1)
    check("other errors: tried again in 10 minutes", (p1["status"], p1["tries"], p1["due_at"] < time.time() + 700), ("scheduled", 1, True))
    fake_upload.fail, n, limit = None, len(sent), config.YT_DAILY_LIMIT
    config.YT_DAILY_LIMIT = 1                     # one upload already made today (p2)
    posting._post(p1)
    check("CBC_YT_DAILY_LIMIT reached: nothing is sent, the post waits for tomorrow",
          (len(sent) == n, p1["status"], p1["tries"], "limit" in p1["error"]), (True, "scheduled", 1, True))
    config.YT_DAILY_LIMIT = limit

# ---- a time picked in the clip dialog's Schedule box, and many clips posted at once (the page calls /api/posts per clip)
ttid, soon = accs["tiktok"]["id"], time.time() + 7200
pick = lambda **kw: b.post("/api/posts", json={"job": "job-shorts01", "clip": "clip03", "accounts": [ytid], **kw}, headers=ORIGIN)
check("a picked time must be a time at least 5 minutes ahead", [pick(at=x).status_code for x in (time.time() - 60, "tomorrow", time.time() + 120)], [400, 400, 400])
r = b.post("/api/posts", json={"job": "job-shorts01", "clip": "clip03", "accounts": [ytid, ttid], "at": soon}, headers=ORIGIN)
with ctx.as_user(user.id):
    mine = {p["platform"]: p for p in posting._mine() if p["clip"] == "clip03"}
check("picked time on YouTube: uploaded now, and YouTube makes it public at that time",
      (r.status_code, mine["youtube"]["due_at"] <= time.time() + 5, mine["youtube"]["publish_at"]), (200, True, soon))
check("picked time on TikTok: posted at that time", (mine["tiktok"]["due_at"], mine["tiktok"]["publish_at"]), (soon, None))
check("the same clip to the same account again is refused", pick(now=True).status_code, 400)
with ctx.as_user(user.id):
    creators.update(cr["id"], {"privacy": "private"})
    posting._post(mine["youtube"])
check("picked time works whatever the creator's visibility setting: sent with its go-public time",
      (mine["youtube"]["status"], sent[-1]["at"], sent[-1]["privacy"]), ("posted", soon, "public"))
codes = [b.post("/api/posts", json={"job": "job-shorts01", "clip": c, "accounts": [ttid], "now": True}, headers=ORIGIN).status_code
         for c in ("clip01", "clip02", "clip03")]
check("Post now for many clips: each new one is queued, one already queued there is skipped", codes, [200, 200, 400])
check("the page is told the daily YouTube limit", b.get("/api/status").json()["yt_daily_limit"], config.YT_DAILY_LIMIT)
accounts.youtube_upload = real_upload

# ---- YouTube through Google's own libraries: upload in pieces, Desktop sign-in, renewing it
try:
    import google_auth_oauthlib.flow, googleapiclient.discovery
    from googleapiclient.http import HttpMockSequence
    libs = True
except ImportError:
    libs = False
check("Google's YouTube libraries are installed (pip install -r requirements.txt)", libs)
if libs:
    from types import SimpleNamespace as NS
    from studio import youtube
    real_yt, real_service = accounts._yt, accounts._yt_service
    accounts._yt = lambda acc: googleapiclient.discovery.build("youtube", "v3", static_discovery=True, http=HttpMockSequence([
        ({"status": "200", "location": "https://upload.example/session1"}, ""),
        ({"status": "200"}, json.dumps({"id": "ytvid9"}))]))
    seen = []
    with ctx.as_user(user.id):
        got = accounts.youtube_upload(accounts.get(ytid), jd / "clip01.mp4", "Title", "Text", ["tag"], "private", progress=seen.append)
    check("YouTube upload: resumable, through the YouTube Data API, with the Shorts link", got, ("ytvid9", "https://www.youtube.com/shorts/ytvid9"))
    check("YouTube upload: progress ends at 100%", seen[-1:], [100])
    yt_http = real_service(NS(token="t", expiry=None))._http.http
    check("YouTube upload: a 308 'piece received' is not followed as a redirect (big clips go in several pieces)",
          (308 in yt_http.redirect_codes, yt_http.timeout), (False, 120))

    client = Path(os.environ["CBC_DATA"]) / "youtube_client.json"
    client.write_text(json.dumps({"installed": {"client_id": "desk-123.apps.googleusercontent.com", "client_secret": "desk-secret-value",
                                                "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                                                "redirect_uris": ["http://localhost"]}}), encoding="utf-8")
    config.YT_CLIENT_FILE = client
    config._kept.pop("youtube_desktop", None)
    asked = {}
    class Flow:
        def run_local_server(self, **kw):
            asked.update(kw)
            return NS(refresh_token="desk-refresh", token="desk-access", expiry=None)
    google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file = lambda f, scopes: Flow()
    channel = NS(channels=lambda: NS(list=lambda **kw: NS(execute=lambda: {"items": [{"id": "UC999", "snippet": {"title": "Desk Channel"}}]})))
    accounts._yt_service = lambda creds: channel
    acc = youtube.connect(user.id)
    del google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file   # the inherited one is back
    accounts._yt_service = real_service
    check("Desktop sign-in: the browser flow answers on a one-off 127.0.0.1 port, asking for a lasting sign-in",
          (asked["host"], asked["port"], asked["access_type"], asked["open_browser"]), ("127.0.0.1", 0, "offline", True))
    with ctx.as_user(user.id):
        saved = accounts.get("yt_UC999")
        stored = (config.user_dir(user.id) / "accounts" / "accounts.json").read_text(encoding="utf-8")
    check("Desktop sign-in: the channel is saved with the other connected apps", (saved["name"], saved["client"], saved["refresh_token"]),
          ("Desk Channel", "desktop", "desk-refresh"))
    check("Desktop sign-in: the client secret is never stored with the account", "desk-secret-value" in stored, False)
    check("Desktop sign-in: tokens never reach the page", "desk-refresh" in b.get("/api/accounts").text, False)

    from google.oauth2.credentials import Credentials
    from datetime import datetime, timedelta, timezone
    used, real_refresh = [], Credentials.refresh
    def fake_refresh(self, request):
        used.append(self.client_id)
        self.token, self.expiry = "desk-access-2", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    Credentials.refresh = fake_refresh
    with ctx.as_user(user.id):
        acc = accounts.get("yt_UC999"); acc["expires"] = 0; accounts._put(acc)
        accounts._yt_creds(accounts.get("yt_UC999"))
        renewed = accounts.get("yt_UC999")
        accounts._yt_creds(renewed)
        web = accounts.get(ytid); web["expires"] = 0; accounts._put(web)     # connected earlier with the Connect button
        accounts._yt_creds(accounts.get(ytid))
    check("an expired sign-in is renewed with the Desktop client, and the new token kept",
          (used[0], renewed["access_token"], renewed["expires"] > time.time() + 3000), ("desk-123.apps.googleusercontent.com", "desk-access-2", True))
    check("a channel connected with the Connect button still renews with the web client", used[1:], ["g-client-123.apps"])
    from google.auth.exceptions import RefreshError
    def revoked(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")
    Credentials.refresh = revoked
    with ctx.as_user(user.id):
        acc = accounts.get("yt_UC999"); acc["expires"] = 0; accounts._put(acc)
        try:
            accounts._yt_creds(accounts.get("yt_UC999")); said = ""
        except RuntimeError as e:
            said = str(e)
    Credentials.refresh = real_refresh
    check("a revoked sign-in says plainly to connect again", "studio.youtube connect" in said)

    # YouTube's real "quota used up" answer, through the real upload code, reaches the queue's wait-for-tomorrow rule
    (jd2 / "clip01.mp4").write_bytes(b"\x00" * 6_000)
    quota = json.dumps({"error": {"code": 403, "message": "The request cannot be completed because you have exceeded your quota.",
                                  "errors": [{"message": "quota", "domain": "youtube.quota", "reason": "quotaExceeded"}]}})
    accounts._yt = lambda acc: googleapiclient.discovery.build("youtube", "v3", static_discovery=True,
                                                              http=HttpMockSequence([({"status": "403"}, quota)]))
    with ctx.as_user(user.id):
        tries = p1["tries"]
        posting._post(p1)
    check("YouTube's real quota error: the post waits for tomorrow, not counted as a failed try",
          (p1["status"], p1["tries"] == tries, p1["due_at"] == posting._youtube_reset()), ("scheduled", True, True))
    accounts._yt = real_yt

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil; db.engine.dispose(); shutil.rmtree(os.environ["CBC_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
