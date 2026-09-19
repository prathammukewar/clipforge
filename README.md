# ClipForge

Paste a YouTube link, get ready-to-upload Shorts back.

ClipForge downloads the video, reads its captions, picks the strongest
moments, cuts each one into a vertical 1080x1920 clip under a minute, and
burns big word-by-word captions into the video. Each file is named with a
suggested title, and YouTube pre-fills the title from the filename when you
upload, so posting is just drag, drop, publish.

## Use it

```bash
./clipforge
```

It asks for the link and how many shorts you want, then opens the output
folder when it finishes. You can also double click `ClipForge.command` in
Finder, or run it directly:

```bash
./clipforge "https://www.youtube.com/watch?v=VIDEO_ID" 6
```

Results land in `output/<video title>/` along with `UPLOAD-INFO.txt`, which
lists where each clip came from and a suggested description for each one.

## Where things go

Everything stays inside the ClipForge folder no matter where you run it
from. `downloads/` holds the source videos and captions, `output/` holds
the finished shorts, and `.tmp/` is scratch space that gets cleaned up
after each clip. Delete `downloads/` any time to free disk space.

## Better clip picking with Claude

Out of the box ClipForge picks moments with a built-in scoring pass. If you
put an Anthropic API key in a `.env` file next to the script, it asks Claude
to pick the moments and write the titles instead, which is a lot better:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Auto-upload and scheduling

The `upload` command pushes a finished folder of shorts to YouTube
through the official API and schedules them an hour apart:

```bash
./upload "output/Some Video Title"
./upload "output/Some Video Title" --interval 90 --start "2026-09-20 09:00"
```

Titles come from the filenames and descriptions from `UPLOAD-INFO.txt`.
The default API quota covers about 6 uploads a day.

One-time setup: create a project at console.cloud.google.com, enable the
YouTube Data API v3, create an OAuth client of type Desktop app, and save
the downloaded file as `client_secret.json` in this folder. The first
upload opens a browser window to sign in to your YouTube account.

The catch you should know about: YouTube locks API uploads to private
until your API project passes their compliance audit (a one-time form at
support.google.com/youtube/contact/submit_app_audit). Until you are
approved, scheduled videos upload fine but stay private instead of going
public, so either file the audit early or flip each video to public in
YouTube Studio, which still beats uploading by hand.

## Setup (already done on this machine)

Needs ffmpeg (`brew install ffmpeg`) and the Python packages in
`requirements.txt` installed into `.venv`.

## The fine print

Only clip videos you have the rights to: your own uploads, licensed
content, or footage whose owner gave you permission. Reposting someone
else's video as Shorts usually ends in a copyright claim, which sends any
revenue to the owner, and repeated strikes can close your channel. YouTube
also demonetizes channels that reuse content without adding something of
their own, so commentary or editing on top of licensed footage is the safe
route.

The video needs English captions or auto-captions. Almost every talking
video has them. Clips are capped at 59 seconds so YouTube always treats
them as Shorts.
