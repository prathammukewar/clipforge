#!/usr/bin/env python3
"""Stagger the publish times of videos already on your channel.

Finds your most recent uploads, then sets each one to go public at a
fixed interval after the last (default: one hour apart). Uses the
official YouTube Data API, so it only ever touches videos on the channel
you sign in as.

Scheduling only works on a private video: YouTube publishes it at the
given time. A video that is already public has nothing to schedule, so
those are skipped unless you pass --include-public, which makes them
private again and gives them a slot.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CLIENT_SECRET = PROJECT_DIR / "client_secret.json"
TOKEN = PROJECT_DIR / "token-manage.json"

# managing existing videos needs more than the upload-only scope,
# so this keeps its own token file
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

UPDATE_COST = 50        # quota units per videos.update
DAILY_QUOTA = 10000


def die(msg):
    print(f"\nError: {msg}", file=sys.stderr)
    sys.exit(1)


def get_youtube():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    if not CLIENT_SECRET.exists():
        die("client_secret.json is missing. Create an OAuth client for a "
            "desktop app in your Google Cloud project (with the YouTube "
            "Data API enabled) and save the downloaded file here as "
            f"{CLIENT_SECRET}")

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            print("Opening a browser to authorize managing your videos...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def recent_uploads(youtube, limit):
    """Newest uploads first, as {id, title} dicts."""
    channels = youtube.channels().list(part="contentDetails,snippet",
                                       mine=True).execute()
    items = channels.get("items") or []
    if not items:
        die("that account has no YouTube channel")
    channel_title = items[0]["snippet"]["title"]
    playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos, page = [], None
    while len(videos) < limit:
        resp = youtube.playlistItems().list(
            part="contentDetails,snippet", playlistId=playlist,
            maxResults=50, pageToken=page).execute()
        for item in resp.get("items", []):
            videos.append({
                "id": item["contentDetails"]["videoId"],
                "title": item["snippet"]["title"],
            })
        page = resp.get("nextPageToken")
        if not page:
            break
    return channel_title, videos[:limit]


def fetch_status(youtube, ids):
    """Current status block for each video id, so updates preserve it."""
    out = {}
    for i in range(0, len(ids), 50):
        resp = youtube.videos().list(part="status",
                                     id=",".join(ids[i:i + 50])).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item["status"]
    return out


def set_publish_time(youtube, video_id, status, publish_at):
    """Update only the schedule, carrying every other status field over."""
    body = {
        "id": video_id,
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "license": status.get("license", "youtube"),
            "embeddable": status.get("embeddable", True),
            "publicStatsViewable": status.get("publicStatsViewable", True),
            "selfDeclaredMadeForKids": status.get("madeForKids", False),
        },
    }
    youtube.videos().update(part="status", body=body).execute()


def main():
    parser = argparse.ArgumentParser(
        description="Schedule your recent uploads to publish at intervals")
    parser.add_argument("count", nargs="?", type=int, default=24,
                        help="how many recent uploads to schedule (default 24)")
    parser.add_argument("--interval", type=int, default=60,
                        help="minutes between publish times (default 60)")
    parser.add_argument("--start", default=None,
                        help='first publish time as "YYYY-MM-DD HH:MM" local '
                             "time (default: one interval from now)")
    parser.add_argument("--newest-first", action="store_true",
                        help="publish the newest upload first "
                             "(default: oldest of the batch goes first)")
    parser.add_argument("--include-public", action="store_true",
                        help="also reschedule videos that are already public, "
                             "which makes them private until their slot")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    args = parser.parse_args()

    count = max(1, args.count)
    if args.start:
        first = (datetime.strptime(args.start, "%Y-%m-%d %H:%M")
                 .astimezone().astimezone(timezone.utc))
    else:
        first = datetime.now(timezone.utc) + timedelta(minutes=args.interval)
    if first < datetime.now(timezone.utc):
        die("the start time is in the past")

    youtube = get_youtube()
    channel, videos = recent_uploads(youtube, count)
    if not videos:
        die("no uploads found on that channel")
    print(f"\nChannel: {channel}")

    statuses = fetch_status(youtube, [v["id"] for v in videos])
    todo, skipped = [], []
    for v in videos:
        privacy = statuses.get(v["id"], {}).get("privacyStatus", "private")
        if privacy == "public" and not args.include_public:
            skipped.append((v, "already public"))
        else:
            todo.append(v)

    if not args.newest_first:
        todo.reverse()   # oldest of the batch publishes first

    if not todo:
        print("\nNothing to schedule. Every video found is already public; "
              "pass --include-public to reschedule them anyway.")
        return

    print(f"\nScheduling {len(todo)} videos every {args.interval} min, "
          f"starting {first.astimezone().strftime('%a %b %d, %I:%M %p')}:\n")
    plan = []
    for i, v in enumerate(todo):
        when = first + timedelta(minutes=args.interval * i)
        plan.append((v, when))
        print(f"  {when.astimezone().strftime('%a %I:%M %p')}   {v['title'][:60]}")
    for v, why in skipped:
        print(f"  skipped ({why})   {v['title'][:60]}")

    cost = len(plan) * UPDATE_COST
    print(f"\nThis uses about {cost} of your {DAILY_QUOTA} daily quota units.")
    if not args.yes:
        if input("Go ahead? [y/N]: ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return

    print()
    failed = 0
    for i, (v, when) in enumerate(plan, 1):
        label = when.astimezone().strftime("%a %I:%M %p")
        try:
            set_publish_time(youtube, v["id"], statuses.get(v["id"], {}), when)
            print(f"  [{i}/{len(plan)}] {label}  {v['title'][:50]}")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(plan)}] FAILED  {v['title'][:50]}: {e}")
            if "quota" in str(e).lower():
                print("  Daily quota is used up; run again tomorrow "
                      "for the rest.")
                break

    done = len(plan) - failed
    print(f"\nScheduled {done} of {len(plan)}. "
          "Check YouTube Studio > Content to confirm.")


if __name__ == "__main__":
    main()
