#!/usr/bin/env python3
"""GTA 6 Clipper: stream link (or nothing) -> ready-to-post vertical clips.

  discover            find today's biggest GTA 6 streams (YouTube + Twitch)
  prep URL [URL...]   steps 1+2: audio-only download, local transcript, loud moments,
                      chat speed-ups (YouTube replays), 12-minute chunks for the AI to read
  status RUN          show what a run has and what's left
  render RUN          step 4: rank the AI's picks, download only those minutes,
                      find the facecam, stack facecam over gameplay, hook + word captions,
                      post caption + hashtags

Step 3 (picking) is done by the Claude Code session itself (no API keys); see SKILL.md.
Run with the skill's venv:  .venv/bin/python scripts/gta6clip.py <command>
"""
import argparse, concurrent.futures as cf, datetime as dt, json, math, os, re, shutil
import statistics, subprocess, sys, time, urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
RUNS = Path(os.environ.get("GTA6_CLIPS_DIR", Path.home() / "GTA6-Clips"))
CFG = json.loads((SKILL / "config.json").read_text()) if (SKILL / "config.json").exists() else {}
CHUNK_SEC = 12 * 60
YTDLP = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--quiet", "--no-progress"]
GTA_RE = re.compile(r"\bgta\s*(6|vi)\b|grand theft auto\s*(6|vi)\b", re.I)


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cookie_args(c):
    if not c:
        return []
    return ["--cookies", c] if os.path.isfile(c) else ["--cookies-from-browser", c]


def hms(s):
    s = max(0, int(round(s)))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def slug(s, n=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s or "stream").strip("-").lower()
    return s[:n] or "stream"


def ytdlp_json(url, cookies=None, extra=()):
    r = run(YTDLP + cookie_args(cookies) + list(extra) + ["-J", url])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "yt-dlp failed")
    return json.loads(r.stdout)


# ---------------------------------------------------------------- discover
def load_env():
    env = {}
    f = SKILL / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    return {**env, **os.environ}


def helix(path, cid, token):
    req = urllib.request.Request(f"https://api.twitch.tv/helix/{path}",
                                 headers={"Client-ID": cid, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def discover_twitch(limit):
    """Official Twitch API. Needs a free Twitch dev app (dev.twitch.tv/console):
    TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET in the skill's .env. Skipped without them."""
    env = load_env()
    cid, sec = env.get("TWITCH_CLIENT_ID"), env.get("TWITCH_CLIENT_SECRET")
    if not (cid and sec):
        log("  twitch: skipped (add TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET to .env to include Twitch)")
        return []
    out = []
    try:
        body = f"client_id={cid}&client_secret={sec}&grant_type=client_credentials".encode()
        with urllib.request.urlopen(urllib.request.Request("https://id.twitch.tv/oauth2/token", data=body), timeout=20) as r:
            tok = json.loads(r.read())["access_token"]
        games = helix("games?name=Grand%20Theft%20Auto%20VI", cid, tok).get("data") or []
        streams = helix(f"streams?game_id={games[0]['id']}&first=20", cid, tok).get("data", []) if games else []
        for st in streams[: limit * 2]:
            vids = helix(f"videos?user_id={st['user_id']}&type=archive&first=1", cid, tok).get("data") or []
            if vids:
                out.append({"platform": "twitch", "streamer": st["user_name"], "title": st["title"],
                            "viewers": st["viewer_count"], "live_now": True, "url": vids[0]["url"]})
    except Exception as e:
        log(f"  twitch: skipped ({e})")
    return out


def discover_youtube(limit):
    """Finished GTA 6 streams from today / this week (clippable now) + what's live right now."""
    out, seen = [], set()
    searches = [("GTA+6+stream", "EgIIAg%3D%3D"), ("GTA+6+live", "EgIIAg%3D%3D"),
                ("GTA+6+stream", "EgIIAw%3D%3D"), ("GTA+6", "EgJAAQ%3D%3D")]
    for q, sp in searches:
        try:
            pl = ytdlp_json(f"https://www.youtube.com/results?search_query={q}&sp={sp}",
                            extra=["--flat-playlist", "--playlist-end", "40"])
        except Exception as e:
            log(f"  youtube search skipped: {e}")
            continue
        for e in pl.get("entries") or []:
            if not e or e.get("id") in seen or not GTA_RE.search(e.get("title") or ""):
                continue
            live = e.get("live_status") == "is_live"
            if not live and (e.get("duration") or 0) < 40 * 60:  # regular videos aren't streams
                continue
            seen.add(e["id"])
            out.append({"platform": "youtube", "streamer": e.get("channel") or e.get("uploader") or "",
                        "title": e.get("title", ""), "live_now": live,
                        "viewers": (e.get("concurrent_view_count") or 0) * 20 if live else (e.get("view_count") or 0),
                        "length": hms(e.get("duration") or 0), "url": f"https://www.youtube.com/watch?v={e['id']}"})
    return out


def cmd_discover(a):
    log("Finding today's biggest GTA 6 streams...")
    with cf.ThreadPoolExecutor(2) as ex:
        tw, yt = ex.submit(discover_twitch, a.limit), ex.submit(discover_youtube, a.limit)
        streams = tw.result() + yt.result()
    # clippable first (finished YouTube replays + Twitch archives), YouTube still-live last
    streams.sort(key=lambda s: (s["platform"] == "youtube" and s["live_now"], -s["viewers"]))
    seen, picked = set(), []
    for s in streams:
        k = s["streamer"].lower()
        if k not in seen:
            seen.add(k)
            picked.append(s)
    picked = picked[: a.limit]
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"discover-{dt.date.today()}.json").write_text(json.dumps(picked, indent=2))
    for i, s in enumerate(picked, 1):
        tag = "LIVE NOW, clip after it ends" if s["platform"] == "youtube" and s["live_now"] else s.get("length", "archive")
        log(f"{i}. [{s['platform']}] {s['streamer']} ({tag}) {s['title'][:80]}\n   {s['url']}")
    if not picked:
        log("Nothing found. Paste stream links instead.")


# ---------------------------------------------------------------- prep (steps 1 + 2)
def loud_moments(wav):
    """Per-second RMS, flag seconds well above the local baseline (yelling / losing it)."""
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav), "-af",
             "asetnsamples=n=16000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"])
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", r.stdout):
        v = m.group(1)
        vals.append(-90.0 if v == "-inf" else float(v))
    events = []
    if not vals:
        return events, vals
    W = 90
    for i, v in enumerate(vals):
        if v < -32:
            continue
        lo, hi = max(0, i - W), min(len(vals), i + W)
        base = statistics.median(vals[lo:hi])
        if v - base >= 9:
            events.append((i, round(v - base, 1)))
    # merge consecutive seconds into events
    merged = []
    for t, d in events:
        if merged and t - merged[-1]["end"] <= 2:
            merged[-1]["end"] = t
            merged[-1]["db"] = max(merged[-1]["db"], d)
        else:
            merged.append({"start": t, "end": t, "db": d})
    return merged, vals


def chat_spikes(chat_file):
    """YouTube replay chat: messages per 10 s vs the local baseline."""
    if not chat_file or not Path(chat_file).exists():
        return []
    times = []
    with open(chat_file) as f:
        for line in f:
            try:
                ms = json.loads(line)["replayChatItemAction"]["videoOffsetTimeMsec"]
                times.append(int(ms) / 1000)
            except Exception:
                pass
    if len(times) < 50:
        return []
    nb = int(max(times) // 10) + 1
    bins = [0] * nb
    for t in times:
        if t >= 0:
            bins[int(t // 10)] += 1
    out = []
    for i, c in enumerate(bins):
        lo, hi = max(0, i - 30), min(nb, i + 30)
        base = max(1.0, statistics.median(bins[lo:hi]))
        if c >= 8 and c / base >= 2.5:
            out.append({"start": i * 10, "end": i * 10 + 10, "x": round(c / base, 1), "msgs": c})
    return out


def transcribe(wav, out_base, words=False):
    model = CFG.get("whisper_model") or os.path.expanduser("~/.cache/whisper-cpp/ggml-small.bin")
    cmd = ["whisper-cli", "-m", model, "-f", str(wav), "-l", "en", "-np", "-mc", "0", "-oj", "-of", str(out_base)]
    if words:
        cmd += ["-ml", "1", "-sow", "-ojf"]
    r = run(cmd)
    if r.returncode != 0:
        raise RuntimeError("whisper failed: " + r.stderr[-400:])
    data = json.loads(Path(f"{out_base}.json").read_text())
    segs = []
    for s in data.get("transcription", []):
        txt = s.get("text", "").strip()
        if txt:
            segs.append({"start": s["offsets"]["from"] / 1000, "end": s["offsets"]["to"] / 1000, "text": txt})
    return segs


def prep_one(url, cookies, force=False):
    t0 = time.time()
    meta = ytdlp_json(url, cookies, ["--no-playlist"])
    if meta.get("live_status") == "is_live" and meta.get("extractor_key", "").lower().startswith("youtube"):
        return {"url": url, "error": "still live on YouTube: clip it once the replay is up (Twitch archives work while live)"}
    streamer = meta.get("uploader") or meta.get("channel") or "streamer"
    rid = f"{dt.date.today()}-{slug(streamer, 24)}-{slug(str(meta.get('id')), 16)}"
    rd = RUNS / rid
    (rd / "chunks").mkdir(parents=True, exist_ok=True)
    (rd / "picks").mkdir(exist_ok=True)
    info = {"url": url, "webpage_url": meta.get("webpage_url") or url, "id": meta.get("id"),
            "title": meta.get("title"), "streamer": streamer, "platform": meta.get("extractor_key"),
            "duration": meta.get("duration"), "cookies": cookies}
    (rd / "stream.json").write_text(json.dumps(info, indent=2))
    if (rd / "transcript.json").exists() and not force:
        log(f"[{streamer}] already prepped -> {rd}")
        return {"run": str(rd), "streamer": streamer, "cached": True}

    log(f"[{streamer}] 1/2 downloading audio only ({hms(meta.get('duration') or 0)} stream)")
    is_yt = "youtube" in (meta.get("extractor_key") or "").lower()
    with cf.ThreadPoolExecutor(2) as ex:
        fa = ex.submit(run, YTDLP + cookie_args(cookies) + ["--no-playlist", "-f", "bestaudio/audio_only/worst",
                                                             "-o", str(rd / "audio.%(ext)s"), url])
        fc = ex.submit(run, YTDLP + cookie_args(cookies) + ["--no-playlist", "--skip-download", "--write-subs",
                                                             "--sub-langs", "live_chat", "-o", str(rd / "chat"), url]) if is_yt else None
        ra = fa.result()
        if fc:
            fc.result()
    src = next((p for p in rd.glob("audio.*") if p.suffix not in (".part", ".ytdl", ".wav")), None)
    if not src:
        raise RuntimeError(f"audio download failed: {ra.stderr[-300:]}")
    wav = rd / "audio.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", str(wav)])
    src.unlink(missing_ok=True)
    t_dl = time.time()

    log(f"[{streamer}] 2/2 transcribing on this Mac + scanning for loud moments and chat spikes")
    with cf.ThreadPoolExecutor(2) as ex:
        fs = ex.submit(transcribe, wav, rd / "whisper")
        fl = ex.submit(loud_moments, wav)
        segs = fs.result()
        loud, _ = fl.result()
    chat_file = next(iter(rd.glob("chat*.live_chat.json")), None)
    chat = chat_spikes(chat_file)
    wav.unlink(missing_ok=True)
    (rd / "whisper.json").unlink(missing_ok=True)
    (rd / "transcript.json").write_text(json.dumps(segs))
    (rd / "signals.json").write_text(json.dumps({"loud": loud, "chat": chat}, indent=1))

    # ---- 12-minute chunks with signal markers inline
    dur = meta.get("duration") or (segs[-1]["end"] if segs else 0)
    n = max(1, math.ceil(dur / CHUNK_SEC))
    for ci in range(n):
        a, b = ci * CHUNK_SEC, (ci + 1) * CHUNK_SEC
        lines = [f"# {streamer}: {info['title']}", f"Chunk {ci + 1}/{n}  ({hms(a)} to {hms(b)})",
                 "Markers: LOUD(+dB over normal) = streamer got loud. CHAT xN = chat moving N times faster than normal.", ""]
        for s in segs:
            if s["end"] <= a or s["start"] >= b:
                continue
            tags = []
            for e in loud:
                if e["start"] <= s["end"] and e["end"] + 1 >= s["start"]:
                    tags.append(f"LOUD(+{e['db']:.0f}dB)")
                    break
            for e in chat:  # chat reacts a few seconds late
                if e["start"] - 15 <= s["end"] and e["end"] >= s["start"]:
                    tags.append(f"CHAT x{e['x']}")
                    break
            lines.append(f"[{hms(s['start'])}] {s['text']}" + (f"   <<{' '.join(tags)}>>" if tags else ""))
        (rd / "chunks" / f"chunk_{ci + 1:02d}.md").write_text("\n".join(lines) + "\n")
    timing = {"prep_started": t0, "download_sec": round(t_dl - t0), "analyse_sec": round(time.time() - t_dl),
              "chunks": n, "segments": len(segs), "loud_events": len(loud), "chat_spikes": len(chat)}
    (rd / "timing.json").write_text(json.dumps(timing, indent=1))
    log(f"[{streamer}] ready: {n} chunks, {len(loud)} loud moments, {len(chat)} chat spikes "
        f"({round(time.time() - t0)}s) -> {rd}")
    return {"run": str(rd), "streamer": streamer, "chunks": n}


def cmd_prep(a):
    RUNS.mkdir(parents=True, exist_ok=True)
    results = []
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        futs = {ex.submit(prep_one, u, a.cookies, a.force): u for u in a.urls}
        for f in cf.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"url": futs[f], "error": str(e)})
                log(f"FAILED {futs[f]}: {e}")
    log("\nRUNS:")
    for r in results:
        log(f"  {r.get('run') or ''}  {r.get('error') or ''}")


# ---------------------------------------------------------------- status
def cmd_status(a):
    rd = Path(a.run)
    chunks = sorted((rd / "chunks").glob("chunk_*.md"))
    picked = {p.stem for p in (rd / "picks").glob("chunk_*.json")}
    todo = [c.name for c in chunks if c.stem not in picked]
    cands = sum(len(json.loads(p.read_text())) for p in (rd / "picks").glob("chunk_*.json"))
    log(json.dumps({"run": str(rd), "chunks": len(chunks), "picked_chunks": len(picked), "candidates": cands,
                    "chunks_left": todo, "clips": sorted(x.name for x in (rd / "clips").glob("*.mp4"))
                    if (rd / "clips").exists() else []}, indent=1))


# ---------------------------------------------------------------- render (step 4)
def select(rd, top, min_score):
    segs = json.loads((rd / "transcript.json").read_text())
    cands = []
    for p in sorted((rd / "picks").glob("chunk_*.json")):
        try:
            for c in json.loads(p.read_text()):
                c["_s"], c["_e"] = parse_ts(c["start"]), parse_ts(c["end"])
                cands.append(c)
        except Exception as e:
            log(f"  bad pick file {p.name}: {e}")
    total = len(cands)
    ok = []
    for c in sorted(cands, key=lambda c: -float(c.get("score", 0))):
        if float(c.get("score", 0)) < min_score:
            continue
        # snap to sentence boundaries so clips don't start/end mid-word
        st = max([s["start"] for s in segs if s["start"] <= c["_s"] + 0.5] or [c["_s"]])
        en = min([s["end"] for s in segs if s["end"] >= c["_e"] - 0.5] or [c["_e"]])
        if en - st > 75:
            en = st + 75
        if en - st < 12:
            continue
        if any(not (en + 20 < o["_s"] or st > o["_e"] + 20) for o in ok):
            continue
        c["_s"], c["_e"] = max(0, st - 0.25), en + 0.4
        ok.append(c)
        if len(ok) >= top:
            break
    return ok, total


def find_facecam(video, W, H):
    """Sample frames, find a face that sits in the same small spot (a facecam overlay),
    then find the overlay box edges from static straight lines around it."""
    import cv2
    import numpy as np
    cv2.setLogLevel(0) if hasattr(cv2, "setLogLevel") else None
    cap = cv2.VideoCapture(str(video))
    nfr = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 300
    det = None
    ym = CFG.get("yunet_model")
    if ym and Path(ym).exists():
        det = cv2.FaceDetectorYN.create(ym, "", (W, H), 0.75, 0.3, 50)
    haar = None
    if det is None and hasattr(cv2, "CascadeClassifier"):
        haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faces, grads = [], []
    for i in range(12):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(nfr * (i + 0.5) / 12))
        ok, fr = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        grads.append((np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0)), np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1))))
        found = []
        if det is not None:
            _, res = det.detect(fr)
            if res is not None:
                found = [tuple(map(float, r[:4])) for r in res]
        elif haar is not None:
            found = [tuple(map(float, f)) for f in haar.detectMultiScale(g, 1.1, 6, minSize=(40, 40))]
        faces.append(found)
    cap.release()
    flat = [f for fs in faces for f in fs]
    if not flat:
        return {"mode": "gameplay"}
    # cluster by centre; keep the most persistent cluster
    best, best_n = None, 0
    for f in flat:
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        members = [g for g in flat if abs(g[0] + g[2] / 2 - cx) < W * 0.06 and abs(g[1] + g[3] / 2 - cy) < H * 0.08]
        if len(members) > best_n:
            best, best_n = members, len(members)
    if best_n < max(3, len(faces) * 0.4):
        return {"mode": "gameplay"}
    fx = statistics.median(m[0] for m in best); fy = statistics.median(m[1] for m in best)
    fw = statistics.median(m[2] for m in best); fh = statistics.median(m[3] for m in best)
    if fw > W * 0.2:  # face fills the screen: it's a camera view, not an overlay
        return {"mode": "cam", "face": [fx, fy, fw, fh]}

    gx = np.median(np.stack([g[0] for g in grads]), axis=0)  # vertical edges, static over time
    gy = np.median(np.stack([g[1] for g in grads]), axis=0)  # horizontal edges

    def scan(profile, start, stop):
        rng = range(int(start), int(stop), 1 if stop > start else -1)
        vals = [(profile[i], i) for i in rng if 0 <= i < len(profile)]
        if len(vals) < 5:
            return None
        v, i = max(vals)
        return i if v > 3.5 * (statistics.median(x for x, _ in vals) + 1e-3) and v > 6 else None

    y0, y1 = int(max(0, fy - fh * 0.5)), int(min(H, fy + fh * 1.5))
    x0, x1 = int(max(0, fx - fw * 0.5)), int(min(W, fx + fw * 1.5))
    colp = gx[y0:y1].mean(axis=0)
    rowp = gy[:, x0:x1].mean(axis=1)
    L = scan(colp, fx - fw * 0.15, fx - fw * 2.6)
    R = scan(colp, fx + fw * 1.15, fx + fw * 3.6)
    T = scan(rowp, fy - fh * 0.15, fy - fh * 2.2)
    B = scan(rowp, fy + fh * 1.15, fy + fh * 3.2)
    # fallbacks: typical facecam framing around the face
    cw_guess = fw * 3.4
    ch_guess = cw_guess * 0.75
    L = L if L is not None else fx + fw / 2 - cw_guess / 2
    R = R if R is not None else fx + fw / 2 + cw_guess / 2
    T = T if T is not None else fy + fh * 0.45 - ch_guess * 0.45
    B = B if B is not None else fy + fh * 0.45 + ch_guess * 0.55
    L, R = max(0, L + 2), min(W, R - 2)
    T, B = max(0, T + 2), min(H, B - 2)
    if R - L < fw * 1.4 or B - T < fh * 1.4:
        return {"mode": "gameplay"}
    return {"mode": "facecam", "box": [int(L), int(T), int(R - L), int(B - T)], "face": [fx, fy, fw, fh]}


def font(size):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def wrap(draw, text, fnt, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def caption_frames(words, hook, seam_y, cap_y, dur, outdir):
    """Build one transparent 1080x1920 PNG per caption state + an ffconcat list."""
    from PIL import Image, ImageDraw
    outdir.mkdir(exist_ok=True)
    hf, cf_ = font(50), font(84)

    def base():
        im = Image.new("RGBA", (1080, 1920), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if hook:
            lines = wrap(d, hook.upper(), hf, 900)[:3]
            lh = 62
            bh = lh * len(lines) + 34
            top = seam_y - bh // 2
            bw = max(d.textlength(l, font=hf) for l in lines) + 60
            d.rounded_rectangle([540 - bw / 2, top, 540 + bw / 2, top + bh], 18, fill=(255, 255, 255, 255))
            for i, l in enumerate(lines):
                d.text((540, top + 17 + i * lh + lh / 2), l, font=hf, fill=(0, 0, 0), anchor="mm")
        return im

    # group words into 1-3 word chunks
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= 3 or w["s"] - cur[-1]["e"] > 0.45 or re.search(r"[.?!,]$", cur[-1]["t"])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    states = []  # (t0, t1, group, active_idx)
    for g in groups:
        for i, w in enumerate(g):
            t1 = g[i + 1]["s"] if i + 1 < len(g) else w["e"]
            states.append((w["s"], max(t1, w["s"] + 0.08), g, i))
    timeline, t, k = [], 0.0, 0
    blank = outdir / "blank.png"
    base().save(blank)
    for t0, t1, g, ai in states:
        if t0 > t + 0.02:
            timeline.append((blank, t0 - t))
        im = base()
        d = ImageDraw.Draw(im)
        text = [w["t"].upper() for w in g]
        widths = [d.textlength(x, font=cf_) for x in text]
        space = d.textlength(" ", font=cf_)
        total = sum(widths) + space * (len(text) - 1)
        scale_font = cf_
        if total > 1000:
            scale_font = font(int(84 * 1000 / total))
            widths = [d.textlength(x, font=scale_font) for x in text]
            space = d.textlength(" ", font=scale_font)
            total = sum(widths) + space * (len(text) - 1)
        x = 540 - total / 2
        for j, (word, wd) in enumerate(zip(text, widths)):
            d.text((x, cap_y), word, font=scale_font, anchor="lm",
                   fill=(255, 221, 0) if j == ai else (255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0))
            x += wd + space
        p = outdir / f"c{k:04d}.png"
        im.save(p)
        k += 1
        start = max(t, t0)
        timeline.append((p, max(0.04, t1 - start)))
        t = start + max(0.04, t1 - start)
    if t < dur:
        timeline.append((blank, dur - t))
    lst = outdir / "list.ffconcat"
    body = ["ffconcat version 1.0"]
    for p, d_ in timeline:
        body += [f"file '{p.name}'", f"duration {d_:.3f}"]
    body.append(f"file '{timeline[-1][0].name}'")
    lst.write_text("\n".join(body) + "\n")
    return lst


def clip_words(src, ss, dur, work):
    wav = work / "w.wav"
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
         "-ac", "1", "-ar", "16000", str(wav)])
    segs = transcribe(wav, work / "words", words=True)
    words = []
    for s in segs:
        t = s["text"].strip()
        if not t or re.fullmatch(r"\[.*\]|\(.*\)", t):
            continue
        if re.fullmatch(r"[^\w]+", t) and words:
            words[-1]["t"] += t
            continue
        words.append({"t": t, "s": s["start"], "e": s["end"]})
    wav.unlink(missing_ok=True)
    return words


def render_one(rd, info, c, n, cookies, keep, local_src=None, src_start=None):
    """local_src/src_start: a file already on disk that starts at stream time src_start
    (the live clipper cuts from its buffer instead of downloading)."""
    clips = rd / "clips"
    clips.mkdir(exist_ok=True)
    work = rd / f"work_{n:02d}"
    work.mkdir(exist_ok=True)
    pad = 3.0
    a, b = max(0, c["_s"] - pad), c["_e"] + 1
    if local_src is not None:
        a = src_start
    src = Path(local_src) if local_src is not None else next(work.glob("src.*"), None)
    if not src:
        r = run(YTDLP + cookie_args(cookies) + ["--no-playlist", "-f",
                "bv*[height<=1080][fps<=60]+ba/b[height<=1080]/bv*+ba/b", "--download-sections", f"*{a:.1f}-{b:.1f}",
                "--merge-output-format", "mp4", "-o", str(work / "src.%(ext)s"), info["webpage_url"]])
        src = next((p for p in work.glob("src.*") if p.suffix in (".mp4", ".mkv", ".webm")), None)
        if not src:
            raise RuntimeError("section download failed: " + r.stderr[-300:])
    pr = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=width,height:format=duration", "-of", "json", str(src)]).stdout)
    W, H = pr["streams"][0]["width"], pr["streams"][0]["height"]
    sdur = float(pr["format"]["duration"])
    ss = min(c["_s"] - a, max(0, sdur - 5))
    dur = min(c["_e"] - c["_s"], sdur - ss)

    fc = find_facecam(src, W, H)
    forced = c.get("layout")  # per-pick override when the detector gets it wrong (scene switches mid-clip)
    if forced == "gameplay" or (forced == "cam" and "face" not in fc):
        fc = {"mode": "gameplay"}
    elif forced == "cam":
        fc = {"mode": "cam", "face": fc["face"]}
    words = clip_words(src, ss, dur, work)
    if fc["mode"] == "facecam":
        L, T, cw, ch = fc["box"]
        top_h = int(min(960, max(560, 1080 * ch / cw)))
        top_h -= top_h % 2
        bot_h = 1920 - top_h
        gw, gh = H * 1080 / bot_h, H
        if gw > W:
            gw, gh = W, W * bot_h / 1080
        gx, gy = (W - gw) / 2, (H - gh) / 2
        vf = (f"[0:v]fps=30,split[a][b];"
              f"[a]crop={cw}:{ch}:{L}:{T},scale=1080:{top_h}:force_original_aspect_ratio=increase,crop=1080:{top_h}[top];"
              f"[b]crop={int(gw)}:{int(gh)}:{int(gx)}:{int(gy)},scale=1080:{bot_h}[bot];[top][bot]vstack[base]")
        seam, cap_y = top_h, top_h + int(bot_h * 0.58)
    elif fc["mode"] == "cam":
        fx, fy, fw, fh = fc["face"]
        cw = min(W, H * 9 / 16)
        cx = min(max(0, fx + fw / 2 - cw / 2), W - cw)
        vf = f"[0:v]fps=30,crop={int(cw)}:{H}:{int(cx)}:0,scale=1080:1920[base]"
        seam, cap_y = 300, 1380
    else:  # no facecam: full frame in the middle over a blurred fill
        vf = ("[0:v]fps=30,split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              "boxblur=30:3,eq=brightness=-0.15[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2[base]")
        seam, cap_y = 560, 1420
    lst = caption_frames(words, c.get("hook") or c.get("title"), seam, cap_y, dur, work / "cap")
    out = clips / f"clip_{n:02d}_{slug(c.get('title') or c.get('hook'), 40)}.mp4"
    r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-f", "concat", "-safe", "0", "-i", str(lst), "-filter_complex",
             vf + ";[1:v]format=rgba[ov];[base][ov]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p[v];"
             "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]",
             "-map", "[v]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    if r.returncode != 0:
        raise RuntimeError("render failed: " + r.stderr[-500:])
    tags = " ".join(c.get("hashtags") or ["#GTA6", "#GTAVI", "#gta6clips"])
    post = (f"TITLE: {c.get('title', '')}\n\nCAPTION:\n{c.get('caption', '')}\n\n{tags}\n\n"
            f"Source: {info['streamer']}, {info['webpage_url']} at {hms(c['_s'])}\n"
            f"Category: {c.get('category')}  Score: {c.get('score')}  Layout: {fc['mode']}\n")
    out.with_suffix(".txt").write_text(post)
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
        if local_src is not None:
            Path(local_src).unlink(missing_ok=True)
    return out, fc["mode"]


def cmd_render(a):
    rd = Path(a.run)
    info = json.loads((rd / "stream.json").read_text())
    t0 = time.time()
    picks, total = select(rd, a.top, a.min_score)
    log(f"[{info['streamer']}] {total} possible moments -> keeping best {len(picks)}")
    made = []
    for n, c in enumerate(picks, 1):
        try:
            out, mode = render_one(rd, info, c, n, a.cookies or info.get("cookies"), a.keep)
            made.append({"file": str(out), "title": c.get("title"), "score": c.get("score"), "layout": mode,
                         "at": hms(c["_s"]), "len": round(c["_e"] - c["_s"])})
            log(f"  clip {n}: {c.get('title')}  ({hms(c['_s'])}, {round(c['_e'] - c['_s'])}s, {mode}) -> {out.name}")
        except Exception as e:
            log(f"  clip {n} FAILED: {e}")
    tm = json.loads((rd / "timing.json").read_text()) if (rd / "timing.json").exists() else {}
    tm["render_sec"] = round(time.time() - t0)
    tm["total_sec"] = round(time.time() - tm.get("prep_started", t0))
    (rd / "timing.json").write_text(json.dumps(tm, indent=1))
    summary = {"stream": info["title"], "streamer": info["streamer"], "length": hms(info.get("duration") or 0),
               "moments_found": total, "clips": made, "total_time": hms(tm["total_sec"])}
    (rd / "clips" / "summary.json").write_text(json.dumps(summary, indent=1))
    log(f"\n{info['streamer']}: {summary['length']} stream -> {total} moments -> {len(made)} clips "
        f"(total {summary['total_time']}) -> {rd / 'clips'}")


# ---------------------------------------------------------------- main
def main():
    if not (SKILL / ".ready").exists():
        sys.exit("Not set up yet. Run: bash scripts/setup.sh")
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("discover"); d.add_argument("--limit", type=int, default=7)
    pr = sp.add_parser("prep"); pr.add_argument("urls", nargs="+")
    pr.add_argument("--cookies", help="chrome / safari / firefox, or a cookies.txt path (age-restricted YouTube, Kick)")
    pr.add_argument("--jobs", type=int, default=2, help="streams processed at once (2 is safe on a 16 GB Mac)")
    pr.add_argument("--force", action="store_true")
    s = sp.add_parser("status"); s.add_argument("run")
    r = sp.add_parser("render"); r.add_argument("run")
    r.add_argument("--top", type=int, default=3); r.add_argument("--min-score", type=float, default=7.0)
    r.add_argument("--cookies"); r.add_argument("--keep", action="store_true", help="keep downloaded source sections")
    a = p.parse_args()
    {"discover": cmd_discover, "prep": cmd_prep, "status": cmd_status, "render": cmd_render}[a.cmd](a)


if __name__ == "__main__":
    main()
