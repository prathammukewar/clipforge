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

## Better clip picking with Claude

Out of the box ClipForge picks moments with a built-in scoring pass. If you
put an Anthropic API key in a `.env` file next to the script, it asks Claude
to pick the moments and write the titles instead, which is a lot better:

```
ANTHROPIC_API_KEY=sk-ant-...
```

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
