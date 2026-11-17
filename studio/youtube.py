"""Connect a YouTube channel from your own computer. Done once per channel:

    python -m studio.youtube connect                      (sign-in off, the usual way on your own computer)
    python -m studio.youtube connect you@example.com      (sign-in on: the CrazeBingoCut account to add it to)

Your browser opens on Google's page. After you allow access, Google sends the answer back to this computer on a
one-off 127.0.0.1 address, and the channel is saved with your other connected apps (Settings, Connected apps). From
then on CrazeBingoCut renews the sign-in by itself. Clips post to the channel from a creator's settings (Auto-post) or
the Post button on a clip.

It needs a "Desktop app" OAuth client from Google Cloud, saved as secrets/youtube_client.json (or wherever
CBC_YT_CLIENT_FILE points): see the README, "Post Shorts to YouTube". Nothing is uploaded by this command."""
import sys
import time

from . import accounts, config, ctx


def connect(uid: str) -> dict:
    """Sign in on Google's page and save the channel for this CrazeBingoCut user. Returns the saved account."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit(accounts.NEED_LIBS)
    if not all(config.youtube_desktop_client()):
        raise SystemExit(f"No Desktop app client found at {config.YT_CLIENT_FILE}.\n"
                         "Download it from Google Cloud (APIs & Services, Credentials, OAuth client ID, Desktop app) "
                         "and save it there. See the README, \"Post Shorts to YouTube\".")
    flow = InstalledAppFlow.from_client_secrets_file(str(config.YT_CLIENT_FILE), accounts.YT_SCOPES.split())
    creds = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, access_type="offline", prompt="consent",
        authorization_prompt_message="Opening your browser to sign in to Google. If it does not open, go to:\n{url}\n",
        success_message="YouTube is connected. You can close this tab and go back to CrazeBingoCut.")
    if not creds.refresh_token:
        raise SystemExit("Google did not give a lasting sign-in. Remove CrazeBingoCut's access in your Google account "
                         "(myaccount.google.com, Security, Third-party connections) and run this again.")
    ch = accounts._yt_service(creds).channels().list(part="snippet", mine=True).execute()
    if not ch.get("items"):
        raise SystemExit("That Google account has no YouTube channel. Create one on YouTube, then run this again.")
    item = ch["items"][0]
    # only the tokens are kept, never the client secret: that stays in the client file
    acc = {"id": f"yt_{item['id']}", "platform": "youtube", "client": "desktop", "name": item["snippet"]["title"],
           "added": time.time(), "refresh_token": creds.refresh_token, "access_token": creds.token,
           "expires": accounts._expiry(creds)}
    with ctx.as_user(uid):
        accounts._put(acc)     # the same channel connected before is replaced, so its queued posts carry on
    return acc


def _user_id(email: str) -> str:
    import sqlalchemy as sa
    from . import auth, db
    db.init()
    if not email:
        if config.ACCOUNTS:
            raise SystemExit("Sign-in is on here. Say which account to add the channel to:\n"
                             "  python -m studio.youtube connect you@example.com")
        return auth.local_user().id
    with db.engine.begin() as c:
        u = c.execute(sa.select(db.users).where(db.users.c.email == email.strip().lower())).first()
    if not u:
        raise SystemExit(f"No CrazeBingoCut account uses {email}. Sign up first, then run this again.")
    return u.id


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "connect":
        raise SystemExit(__doc__)
    got = connect(_user_id(sys.argv[2] if len(sys.argv) > 2 else ""))
    print(f"Connected YouTube channel: {got['name']}. Refresh CrazeBingoCut's page to see it under Connected apps.")
