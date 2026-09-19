#!/usr/bin/env python3
"""ClipForge uploader: push finished shorts to YouTube on a schedule.

Uploads every .mp4 in a ClipForge output folder through the official
YouTube Data API. Each video is titled from its filename, described from
UPLOAD-INFO.txt, and scheduled to publish at a fixed interval after the
previous one (default: one hour apart).

Needs a client_secret.json from your own Google Cloud project in this
folder. First run opens a browser window to sign in; after that the
saved token in token.json is reused.

Heads up: YouTube locks API uploads from un-audited apps to private,
so until your API project passes YouTube's compliance audit, scheduled
videos upload fine but will not go public on their own.
"""

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
CLIENT_SECRET = PROJECT_DIR / "client_secret.json"
TOKEN = PROJECT_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# each upload costs 1600 of the default 10,000 daily quota units
MAX_PER_DAY = 6


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
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def read_descriptions(folder):
    """Map each mp4 filename to its description from UPLOAD-INFO.txt."""
    info = folder / "UPLOAD-INFO.txt"
    if not info.exists():
        return {}
    descriptions, current = {}, None
    for line in info.read_text().splitlines():
        m = re.match(r"\d+\.\s+(.+\.mp4)\s*$", line.strip())
        if m:
            current = m.group(1)
        elif current and "description:" in line:
            descriptions[current] = line.split("description:", 1)[1].strip()
    return descriptions


def upload_one(youtube, path, description, publish_at):
    from googleapiclient.http import MediaFileUpload

    title = path.stem[:100]
    body = {
        "snippet": {
            "title": title,
            "description": f"{description}\n\n#Shorts",
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024,
                            resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(
        part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"    {int(status.progress() * 100)}%", end="\r")
    return response["id"]


def main():
    parser = argparse.ArgumentParser(
        description="Upload a folder of shorts to YouTube, spaced out on a schedule")
    parser.add_argument("folder", help="an output/<video title> folder from ClipForge")
    parser.add_argument("--interval", type=int, default=60,
                        help="minutes between publish times (default 60)")
    parser.add_argument("--start", default=None,
                        help='first publish time as "YYYY-MM-DD HH:MM" local '
                             "time (default: one interval from now)")
    args = parser.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        die(f"{folder} is not a folder")
    videos = sorted(folder.glob("*.mp4"))
    if not videos:
        die(f"no .mp4 files in {folder}")
    if len(videos) > MAX_PER_DAY:
        print(f"Note: YouTube's default API quota covers about {MAX_PER_DAY} "
              f"uploads per day; found {len(videos)} videos. The extras will "
              "fail with a quota error and can be uploaded tomorrow.")

    if args.start:
        local = datetime.strptime(args.start, "%Y-%m-%d %H:%M").astimezone()
        first = local.astimezone(timezone.utc)
    else:
        first = datetime.now(timezone.utc) + timedelta(minutes=args.interval)
    if first < datetime.now(timezone.utc):
        die("the start time is in the past")

    descriptions = read_descriptions(folder)
    youtube = get_youtube()

    print(f"Uploading {len(videos)} shorts, publishing every "
          f"{args.interval} min starting "
          f"{first.astimezone().strftime('%Y-%m-%d %H:%M %Z')}:\n")
    for i, path in enumerate(videos):
        publish_at = first + timedelta(minutes=args.interval * i)
        local_time = publish_at.astimezone().strftime("%H:%M")
        print(f"  [{i + 1}/{len(videos)}] {path.name}  (publishes {local_time})")
        try:
            vid = upload_one(youtube, path,
                             descriptions.get(path.name, path.stem), publish_at)
            print(f"    done: https://youtube.com/shorts/{vid}")
            # move it aside so a rerun can never upload the same file twice
            done_dir = folder / "uploaded"
            done_dir.mkdir(exist_ok=True)
            path.rename(done_dir / path.name)
        except Exception as e:
            print(f"    FAILED: {e}")
            if "quota" in str(e).lower():
                print("    Daily API quota used up; run again tomorrow "
                      "for the rest.")
                break

    print("\nAll set. Check YouTube Studio > Content to confirm the "
          "scheduled times.")
    print("If videos stay locked private, your API project still needs "
          "YouTube's audit (see README).")


if __name__ == "__main__":
    main()
