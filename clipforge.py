#!/usr/bin/env python3
"""ClipForge: paste a YouTube link, get ready-to-upload Shorts back.

Pipeline: download the video and its captions with yt-dlp, pick the best
moments (Claude API if a key is available, otherwise a local heuristic),
cut each moment to a vertical 9:16 clip with ffmpeg, and burn big
word-by-word captions rendered with Pillow. Each output file is named
with its suggested title, because YouTube pre-fills the title from the
filename on upload.
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Every file ClipForge writes stays inside these folders, no matter
# where you run it from.
PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADS = PROJECT_DIR / "downloads"   # source videos and caption files
OUTPUT = PROJECT_DIR / "output"         # finished shorts, one folder per video
TMP = PROJECT_DIR / ".tmp"              # scratch space, cleaned after each clip

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

MIN_LEN = 20.0   # seconds
MAX_LEN = 59.0   # keep under a minute so it is unambiguously a Short
TITLE_MAX = 92   # leave room for " #Shorts" under YouTube's 100-char cap


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    title: str
    description: str = ""


def log(msg):
    print(msg, flush=True)


def die(msg):
    print(f"\nError: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- env / key

def load_env():
    """Read KEY=value lines from .env next to this script, if present."""
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- download

def fetch_video(url):
    """Download the video plus English captions. Returns (info, video, subs)."""
    import yt_dlp

    DOWNLOADS.mkdir(exist_ok=True)
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "outtmpl": str(DOWNLOADS / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-orig", "en-US", "en-GB"],
        "subtitlesformat": "json3",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    vid = info["id"]
    video_path = DOWNLOADS / f"{vid}.mp4"
    if not video_path.exists():
        candidates = list(DOWNLOADS.glob(f"{vid}.*"))
        candidates = [c for c in candidates if c.suffix in (".mp4", ".mkv", ".webm")]
        if not candidates:
            die("download finished but no video file was found")
        video_path = candidates[0]

    subs_path = None
    for lang in ("en", "en-orig", "en-US", "en-GB"):
        p = DOWNLOADS / f"{vid}.{lang}.json3"
        if p.exists():
            subs_path = p
            break
    return info, video_path, subs_path


# ---------------------------------------------------------------- captions

def parse_json3(path):
    """Turn YouTube's json3 caption format into a flat word list."""
    data = json.loads(Path(path).read_text())
    words = []
    for ev in data.get("events", []):
        base = ev.get("tStartMs")
        if base is None:
            continue
        dur = ev.get("dDurationMs", 0)
        for seg in ev.get("segs", []) or []:
            # some caption tracks pad with zero-width spaces; drop them
            text = (seg.get("utf8") or "").replace("​", " ").strip()
            if not text:
                continue
            start = (base + seg.get("tOffsetMs", 0)) / 1000.0
            for token in text.split():
                words.append(Word(start=start, end=start, text=token))
        # give the last word of the event a fallback end
        if words and dur:
            words[-1].end = (base + dur) / 1000.0
    words.sort(key=lambda w: w.start)
    # styled caption tracks repeat every word in overlapping events
    # ("Months Months ago, ago,"); keep one copy of near-simultaneous twins
    deduped = []
    for w in words:
        if deduped and w.text == deduped[-1].text and w.start - deduped[-1].start < 0.3:
            continue
        deduped.append(w)
    words = deduped
    for i, w in enumerate(words):
        # json3 only carries start offsets: hold each word until the next
        # one begins, capped so captions don't freeze through long pauses
        nxt = words[i + 1].start if i + 1 < len(words) else w.start + 1.0
        w.end = max(w.start + 0.12, min(nxt, w.start + 1.2))
    return words


def sentences_from_words(words):
    """Group words into rough sentences using punctuation and pauses."""
    groups, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = (words[i + 1].start - w.end) if i + 1 < len(words) else 99
        if w.text[-1:] in ".!?" or gap > 0.7 or len(cur) >= 30:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


# ---------------------------------------------------------------- pick moments

HOOK_WORDS = {
    "secret", "never", "nobody", "insane", "crazy", "mistake", "wrong",
    "best", "worst", "biggest", "actually", "truth", "why", "how",
    "free", "money", "rich", "million", "billion", "hack", "trick",
    "warning", "stop", "before", "instantly", "proof", "exposed",
}


def pick_segments_heuristic(words, count):
    """No-API fallback: score sentence windows for hook-iness and density."""
    sents = sentences_from_words(words)
    scored = []
    i = 0
    while i < len(sents):
        j, start = i, sents[i][0].start
        while j < len(sents) and sents[j][-1].end - start < 42:
            j += 1
        end = sents[min(j, len(sents) - 1)][-1].end
        window = [w for s in sents[i:j + 1] for w in s]
        dur = end - start
        if MIN_LEN <= dur <= MAX_LEN and window:
            text = " ".join(w.text for w in window)
            lower = text.lower()
            score = sum(lower.count(h) for h in HOOK_WORDS)
            score += 2 * lower.count("?")
            score += len(re.findall(r"\d", lower)) * 0.3
            score += len(window) / dur  # words per second
            first = " ".join(w.text for w in sents[i][:12])
            scored.append((score, Segment(start, end, first)))
        i += 1
    scored.sort(key=lambda t: -t[0])
    picked = []
    for _, seg in scored:
        if all(seg.end < p.start - 5 or seg.start > p.end + 5 for p in picked):
            picked.append(seg)
        if len(picked) >= count:
            break
    picked.sort(key=lambda s: s.start)
    return picked


CLAUDE_SYSTEM = """You pick the most clippable moments from a video transcript \
for YouTube Shorts. Choose self-contained moments that hook a viewer in the \
first two seconds: strong claims, surprising facts, punchlines, emotional \
peaks, clear payoffs. Each clip must make sense with zero context."""


def pick_segments_claude(words, video_title, count):
    """Ask Claude to pick the best moments and write titles for them."""
    import anthropic

    sents = sentences_from_words(words)
    lines = []
    for s in sents:
        lines.append(f"[{s[0].start:.1f}-{s[-1].end:.1f}] " + " ".join(w.text for w in s))
    transcript = "\n".join(lines)

    prompt = f"""Video title: {video_title}

Transcript with [start-end] second markers per sentence:

{transcript}

Pick the {count} best moments for YouTube Shorts. Rules:
- each clip 20 to 58 seconds long, using the sentence markers for start/end
- start exactly where a sentence starts, end exactly where a sentence ends
- clips must not overlap
- title: a curiosity-driven YouTube title, max {TITLE_MAX} chars, no emoji, \
no quotes, no colons or slashes
- description: 1-2 sentences for the video description

Reply with ONLY a JSON array, no other text:
[{{"start": 123.4, "end": 168.9, "title": "...", "description": "..."}}]"""

    client = anthropic.Anthropic()
    with client.beta.messages.stream(
        model="claude-opus-5",
        max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        raise RuntimeError("model declined the request")

    text = "".join(b.text for b in msg.content if b.type == "text")
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise RuntimeError("no JSON found in model reply")
    return validate_segments(json.loads(match.group(0)), words, count)


def validate_segments(raw, words, count=None):
    """Clamp raw {start,end,title,description} dicts into usable Segments."""
    total = words[-1].end
    segs = []
    for item in raw:
        start = max(0.0, float(item["start"]))
        end = min(total, float(item["end"]))
        if end - start > MAX_LEN:
            end = start + MAX_LEN
        if end - start < MIN_LEN:
            continue
        segs.append(Segment(start, end, str(item["title"]).strip(),
                            str(item.get("description", "")).strip()))
    segs.sort(key=lambda s: s.start)
    return segs[:count] if count else segs


def snap_to_words(seg, words):
    """Align the segment to actual word boundaries, with a little padding."""
    inside = [w for w in words if w.end > seg.start and w.start < seg.end]
    if not inside:
        return None
    seg.start = max(0.0, inside[0].start - 0.25)
    seg.end = inside[-1].end + 0.35
    if seg.end - seg.start > MAX_LEN:
        # drop trailing words until we fit
        while inside and inside[-1].end + 0.35 - seg.start > MAX_LEN:
            inside.pop()
        if not inside:
            return None
        seg.end = inside[-1].end + 0.35
    if seg.end - seg.start < MIN_LEN:
        return None
    return seg


# ---------------------------------------------------------------- captions render

def caption_chunks(words, seg):
    """Group the segment's words into short bursts shown one at a time."""
    inside = [w for w in words if w.start >= seg.start - 0.05 and w.start < seg.end]
    chunks, cur = [], []
    for i, w in enumerate(inside):
        cur.append(w)
        dur = cur[-1].end - cur[0].start
        gap = (inside[i + 1].start - w.end) if i + 1 < len(inside) else 99
        if len(cur) >= 3 or dur > 1.3 or w.text[-1:] in ".!?," or gap > 0.5:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    out = []
    for c in chunks:
        start = max(0.0, c[0].start - seg.start)
        end = min(seg.end - seg.start, c[-1].end - seg.start + 0.1)
        text = " ".join(w.text for w in c).upper()
        text = re.sub(r"[\[\]]", "", text)
        if text.strip():
            out.append((start, max(end, start + 0.25), text))
    # never let a burst linger into the next one: two captions on screen
    # at once render on top of each other
    for i in range(len(out) - 1):
        start, end, text = out[i]
        nxt_start = out[i + 1][0]
        if end > nxt_start - 0.001:
            out[i] = (start, max(nxt_start - 0.001, start + 0.1), text)
    return out


def load_font(size):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def render_caption_png(text, path, width=1080):
    """Render one caption burst as a transparent PNG with heavy outline."""
    from PIL import Image, ImageDraw

    size = 92
    font = load_font(size)
    pad = 40
    dummy = Image.new("RGBA", (8, 8))
    draw = ImageDraw.Draw(dummy)
    while size > 40:
        box = draw.textbbox((0, 0), text, font=font, stroke_width=size // 10)
        if box[2] - box[0] <= width - 2 * pad:
            break
        size -= 6
        font = load_font(size)
    box = draw.textbbox((0, 0), text, font=font, stroke_width=size // 10)
    w, h = box[2] - box[0], box[3] - box[1]
    img = Image.new("RGBA", (width, h + 2 * pad), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = (width - w) // 2 - box[0]
    y = pad - box[1]
    d.text((x + 5, y + 6), text, font=font, fill=(0, 0, 0, 160),
           stroke_width=size // 10, stroke_fill=(0, 0, 0, 160))  # soft shadow
    d.text((x, y), text, font=font, fill=(255, 255, 255, 255),
           stroke_width=size // 10, stroke_fill=(10, 10, 10, 255))
    img.save(path)
    return img.height


# ---------------------------------------------------------------- ffmpeg

def probe_size(video):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def cut_short(video, seg, chunks, out_path, src_w, src_h):
    """One ffmpeg pass: trim, crop to 9:16, scale, overlay caption PNGs."""
    TMP.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="clip_", dir=TMP))
    try:
        inputs = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                  "-ss", f"{seg.start:.3f}", "-t", f"{seg.end - seg.start:.3f}",
                  "-i", str(video)]
        overlays = []
        for i, (cs, ce, text) in enumerate(chunks):
            png = tmp / f"cap{i:03d}.png"
            render_caption_png(text, png)
            inputs += ["-i", str(png)]
            overlays.append((cs, ce))

        if src_w / src_h > 9 / 16:
            base = "crop=ih*9/16:ih,scale=1080:1920"
        else:
            base = "crop=iw:iw*16/9,scale=1080:1920"
        chain = [f"[0:v]{base},setsar=1[v0]"]
        prev = "v0"
        for i, (cs, ce) in enumerate(overlays):
            nxt = f"v{i + 1}"
            chain.append(
                f"[{prev}][{i + 1}:v]overlay=(W-w)/2:H*0.66-h/2:"
                f"enable='between(t,{cs:.3f},{ce:.3f})'[{nxt}]")
            prev = nxt
        cmd = inputs + [
            "-filter_complex", ";".join(chain), "-map", f"[{prev}]",
            "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(out_path)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg failed:\n{res.stderr[-2000:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- output

def safe_filename(title):
    """Keep the title readable: it becomes the YouTube title on upload."""
    title = re.sub(r"[<>]", "", title)          # YouTube titles reject these
    title = re.sub(r"[/\\:|\n\r]", "-", title)  # illegal or risky in filenames
    return title[:TITLE_MAX].strip(" .-") or "Untitled clip"


def make_shorts(url, count, segments_file=None, transcript_only=False):
    log("Downloading video and captions...")
    info, video_path, subs_path = fetch_video(url)
    title = info.get("title", "video")
    log(f'  "{title}" ({int(info.get("duration") or 0) // 60} min)')

    if not subs_path:
        die("this video has no English captions or auto-captions; "
            "ClipForge needs them for the on-screen text")

    words = parse_json3(subs_path)
    if len(words) < 80:
        die("captions are too sparse to build shorts from")
    log(f"  transcript: {len(words)} words")

    if transcript_only:
        sents = sentences_from_words(words)
        lines = [f"[{s[0].start:.1f}-{s[-1].end:.1f}] "
                 + " ".join(w.text for w in s) for s in sents]
        path = DOWNLOADS / f"{info['id']}.transcript.txt"
        path.write_text(f"# {title}\n" + "\n".join(lines))
        log(f"Transcript written to {path}")
        return

    log("Picking the best moments...")
    segments = []
    if segments_file:
        raw = json.loads(Path(segments_file).read_text())
        segments = validate_segments(raw, words)
        log(f"  using {len(segments)} preset segments")
    elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        try:
            segments = pick_segments_claude(words, title, count)
            log(f"  Claude picked {len(segments)} moments")
        except Exception as e:
            log(f"  Claude unavailable ({e}); using built-in picker")
    if not segments:
        segments = pick_segments_heuristic(words, count)
        log(f"  built-in picker chose {len(segments)} moments")
    if not segments:
        die("could not find any usable 20-59 second moments")

    segments = [s for s in (snap_to_words(s, words) for s in segments) if s]

    out_dir = OUTPUT / safe_filename(title)[:60]
    out_dir.mkdir(parents=True, exist_ok=True)
    src_w, src_h = probe_size(video_path)

    used_names = set()
    upload_notes = [f"Source video: {title}", f"URL: {url}", ""]
    for n, seg in enumerate(segments, 1):
        name = safe_filename(seg.title)
        while name in used_names:
            name += " (alt)"
        used_names.add(name)
        out_path = out_dir / f"{name} #Shorts.mp4"
        chunks = caption_chunks(words, seg)
        mins, secs = divmod(int(seg.start), 60)
        log(f"  [{n}/{len(segments)}] {seg.title[:60]}  "
            f"(from {mins}:{secs:02d}, {seg.end - seg.start:.0f}s)")
        cut_short(video_path, seg, chunks, out_path, src_w, src_h)
        upload_notes += [
            f"{n}. {out_path.name}",
            f"   from {mins}:{secs:02d} in the source",
            f"   description: {seg.description or seg.title}",
            "",
        ]

    (out_dir / "UPLOAD-INFO.txt").write_text("\n".join(upload_notes))
    log(f"\nDone. {len(segments)} shorts in {out_dir}")
    log("Each filename becomes the video title when you upload it.")
    subprocess.run(["open", str(out_dir)], check=False)


# ---------------------------------------------------------------- cli

def main():
    load_env()
    parser = argparse.ArgumentParser(description="Turn a YouTube video into Shorts")
    parser.add_argument("url", nargs="?", help="YouTube video link")
    parser.add_argument("count", nargs="?", type=int, default=4,
                        help="how many shorts to make (default 4)")
    parser.add_argument("--transcript-only", action="store_true",
                        help="download and write the transcript, then stop")
    parser.add_argument("--segments", metavar="FILE",
                        help="JSON file of {start,end,title,description} "
                             "segments to use instead of automatic picking")
    args = parser.parse_args()

    url = args.url
    count = max(1, min(args.count, 40))
    if not url and (args.transcript_only or args.segments):
        die("give the YouTube link on the command line with these options")
    if not url:
        print("ClipForge: paste a YouTube link, get Shorts back.\n")
        url = input("YouTube link: ").strip()
        if not url:
            die("no link given")
        raw = input("How many shorts? [4]: ").strip()
        if raw.isdigit():
            count = max(1, min(int(raw), 40))
    if not re.match(r"https?://", url):
        url = "https://" + url

    make_shorts(url, count, segments_file=args.segments,
                transcript_only=args.transcript_only)


if __name__ == "__main__":
    main()
