#!/usr/bin/env python3
"""
Ditch Channel -> Instagram show announcer.

Run it every few minutes (cron, GitHub Actions, a Raspberry Pi...). Each run:
  1. Reads the show schedule (Radio.co, or schedule.csv as a fallback / override)
  2. Posts a "Coming up" Story ahead of each show
  3. Posts a "Live now" Story when each show starts
     (and, optionally, when a DJ goes live on Radio.co outside the schedule)
Everything it has already posted is remembered in state.json, so nothing
is posted twice.

Usage:
  python ditch_poster.py            # one pass (what cron / Actions runs)
  python ditch_poster.py --dry-run  # make the cards + captions, post nothing
  python ditch_poster.py --preview  # render sample cards to ./out and exit
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
UA = {"User-Agent": "ditch-poster/1.0 (+https://ditch.channel)"}


# --------------------------------------------------------------------------- config
def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = re.split(r"\s{2,}#", v, maxsplit=1)[0]  # "value    # comment" (keeps "#tag #tag")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(HERE / ".env")


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default  # empty counts as unset


def env_bool(name: str, default: bool) -> bool:
    v = env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


CFG = {
    "station_id": env("RADIOCO_STATION_ID"),
    "schedule_source": env("SCHEDULE_SOURCE", "auto"),  # auto | radioco | csv
    "schedule_csv": HERE / env("SCHEDULE_CSV", "schedule.csv"),
    "tz": ZoneInfo(env("TIMEZONE", "Europe/London")),
    "coming_up_lead": int(env("COMING_UP_MINUTES", "60")),
    "live_grace": int(env("LIVE_GRACE_MINUTES", "20")),
    "coming_up_as": env("COMING_UP_AS", "story"),  # feed | story | both | off
    "live_now_as": env("LIVE_NOW_AS", "story"),    # feed | story | both | off
    "unscheduled_live": env_bool("POST_UNSCHEDULED_LIVE", True),
    "ignore": [s.strip().lower() for s in env("IGNORE_SHOWS", "All").split(",") if s.strip()],
    "min_show_minutes": int(env("MIN_SHOW_MINUTES", "20")),
    "caption_coming_up": env(
        "CAPTION_COMING_UP",
        "Coming up at {time} on Ditch Channel: {show}{host_line}\n\nListen live at ditch.channel {hashtags}",
    ),
    "caption_live_now": env(
        "CAPTION_LIVE_NOW",
        "LIVE NOW on Ditch Channel: {show}{host_line}\n\nTune in at ditch.channel {hashtags}",
    ),
    "hashtags": env("HASHTAGS", "#ditchchannel #radio"),
    "accent": env("ACCENT_COLOUR", "#FCC419"),
    "bg": env("BACKGROUND_COLOUR", "#0B0B0B"),
    "fg": env("TEXT_COLOUR", "#FFFFFF"),
    "station_name": env("STATION_NAME", "DITCH CHANNEL"),
    "station_url": env("STATION_URL", "ditch.channel"),
    # Instagram won't let automated Stories carry a link sticker, so the card points to the bio
    "link_text": env("CARD_LINK_TEXT", "LINK IN BIO · DITCH.CHANNEL"),
    "logo": env("LOGO_PATH", "assets/logo.png"),
    # Instagram
    "ig_user_id": env("IG_USER_ID"),
    "ig_token": env("IG_ACCESS_TOKEN"),
    "graph": env("GRAPH_API_VERSION", "v21.0"),
    # graph.instagram.com for "Instagram API with Instagram Login" tokens (the README route);
    # use graph.facebook.com if your token came from Facebook Login instead
    "graph_host": env("GRAPH_HOST", "graph.instagram.com"),
    # Image hosting (Instagram needs a public URL for every image)
    "host": env("IMAGE_HOST", "github"),  # github | folder
    "gh_repo": env("GITHUB_REPOSITORY"),  # owner/repo, set automatically in Actions
    "gh_token": env("GH_CONTENTS_TOKEN") or env("GITHUB_TOKEN"),
    "gh_branch": env("CARDS_BRANCH", "ig-cards"),
    "folder_path": env("PUBLIC_IMAGE_DIR"),
    "folder_url": env("PUBLIC_IMAGE_BASE_URL"),
    "state_file": HERE / env("STATE_FILE", "state.json"),
}


def find_asset(name: str) -> Path:
    """assets/<name>, or <name> next to this script (web uploads can flatten folders)."""
    for p in (HERE / "assets" / name, HERE / name):
        if p.exists():
            return p
    return HERE / "assets" / name


def log(*a) -> None:
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# --------------------------------------------------------------------------- schedule
@dataclass
class Show:
    title: str
    start: datetime  # aware, UTC
    end: datetime
    host: str = ""
    image_url: str = ""
    source: str = ""
    colour: str = ""

    @property
    def key(self) -> str:
        raw = f"{self.title}|{self.start.isoformat()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


def parse_dt(value: str, tz: ZoneInfo) -> datetime:
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def schedule_from_radioco() -> list[Show]:
    """Radio.co's schedule widget feed. Undocumented, so parsed defensively."""
    sid = CFG["station_id"]
    if not sid:
        raise RuntimeError("RADIOCO_STATION_ID is not set")
    url = f"https://public.radio.co/stations/{sid}/embed/schedule"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    shows = []
    for it in items or []:
        pl = it.get("playlist") or {}
        title = pl.get("name") or it.get("name") or it.get("title") or ""
        start, end = it.get("start"), it.get("end")
        if not (title and start and end):
            continue
        shows.append(Show(
            title=title.strip(),
            start=parse_dt(start, CFG["tz"]),
            end=parse_dt(end, CFG["tz"]),
            host=(pl.get("artist") or "").strip(),
            image_url=pl.get("artwork") or pl.get("artwork_url") or "",
            source="radioco",
            colour=pl.get("colour") or "",
        ))
    return shows


def schedule_from_csv() -> list[Show]:
    """schedule.csv columns: date,start,end,show,host,image_url,colour
    date = YYYY-MM-DD, or a weekday (Mon..Sun) for weekly shows."""
    path = CFG["schedule_csv"]
    if not path.exists():
        return []
    tz = CFG["tz"]
    today = datetime.now(tz).date()
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    shows = []
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if not row.get("show") or row.get("date", "").startswith("#"):
                continue
            d = row.get("date", "")
            if d[:3].lower() in days:  # weekly: next 7 days
                wd = days.index(d[:3].lower())
                dates = [today + timedelta(days=i) for i in range(-1, 8)
                         if (today + timedelta(days=i)).weekday() == wd]
            else:
                dates = [datetime.strptime(d, "%Y-%m-%d").date()]
            for dt in dates:
                s = datetime.combine(dt, datetime.strptime(row["start"], "%H:%M").time(), tz)
                e = datetime.combine(dt, datetime.strptime(row["end"], "%H:%M").time(), tz)
                if e <= s:
                    e += timedelta(days=1)  # runs past midnight
                shows.append(Show(row["show"], s.astimezone(timezone.utc),
                                  e.astimezone(timezone.utc), row.get("host", ""),
                                  row.get("image_url", ""), "csv", row.get("colour", "")))
    return shows


def get_schedule() -> list[Show]:
    src = CFG["schedule_source"]
    shows: list[Show] = []
    if src in ("auto", "radioco") and CFG["station_id"]:
        try:
            shows = schedule_from_radioco()
            log(f"Radio.co schedule: {len(shows)} slots")
        except Exception as e:  # noqa: BLE001
            log(f"Radio.co schedule unavailable ({e})")
            if src == "radioco":
                raise
    if src == "csv" or (src == "auto"):
        extra = schedule_from_csv()
        if extra:
            log(f"schedule.csv: {len(extra)} slots")
            # CSV wins where it overlaps Radio.co (lets you fix names / add hosts)
            csv_starts = {s.start for s in extra}
            shows = [s for s in shows if s.start not in csv_starts] + extra
    keep = []
    for s in shows:
        if s.title.lower() in CFG["ignore"]:
            continue
        if (s.end - s.start) < timedelta(minutes=CFG["min_show_minutes"]):
            continue
        keep.append(s)
    return sorted(keep, key=lambda s: s.start)


def radioco_status() -> dict:
    sid = CFG["station_id"]
    if not sid:
        return {}
    try:
        r = requests.get(f"https://public.radio.co/stations/{sid}/status", headers=UA, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        log(f"Radio.co status unavailable ({e})")
        return {}


# --------------------------------------------------------------------------- cards
def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    name = "Poppins-Bold.ttf" if bold else "Poppins-Medium.ttf"
    p = find_asset(name)
    try:
        return ImageFont.truetype(str(p), size)
    except OSError:
        return ImageFont.load_default(size)


def fit_text(draw, text, max_w, max_lines, start_size, min_size=48):
    """Largest font size where text wraps into max_lines within max_w."""
    for size in range(start_size, min_size - 1, -4):
        f = font(size)
        avg = draw.textlength("abcdefghijklmnopqrstuvwxyz", font=f) / 26
        width_chars = max(4, int(max_w / avg))
        lines = textwrap.wrap(text, width=width_chars, break_long_words=False) or [text]
        if len(lines) <= max_lines and all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f, lines
    f = font(min_size)
    lines = textwrap.wrap(text, width=max(4, int(max_w / (min_size * 0.55))), break_long_words=False)[:max_lines]
    return f, lines


def load_artwork(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        if url.startswith("http"):
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            return Image.open(BytesIO(r.content)).convert("RGB")
        p = (HERE / url) if not os.path.isabs(url) else Path(url)
        return Image.open(p).convert("RGB") if p.exists() else None
    except Exception as e:  # noqa: BLE001
        log(f"artwork failed ({e})")
        return None


def text_on(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever reads better on this colour."""
    r, g, b = (c / 255 for c in rgb)
    return (0, 0, 0) if (0.299 * r + 0.587 * g + 0.114 * b) > 0.55 else (255, 255, 255)


def asset(name: str) -> Image.Image | None:
    p = find_asset(name)
    return Image.open(p).convert("RGBA") if p.exists() else None


def render_card(show: Show, kind: str, fmt: str) -> Path:
    """Ditch stamp card. kind: coming_up | live_now   fmt: feed (1080x1350) | story (1080x1920)

    The show's Radio.co colour fills the background; the black wavy stamp sits on top
    and holds the wordmark, badge, show title, host and time in white."""
    W, H = (1080, 1350) if fmt == "feed" else (1080, 1920)
    colour = hex_rgb(show.colour or CFG["accent"])
    ink = text_on(colour)
    img = Image.new("RGB", (W, H), colour)

    stamp = asset("stamp.png")
    sh = int(H * (0.90 if fmt == "feed" else 0.78))
    if stamp:
        sw = int(stamp.width * sh / stamp.height)
        stamp = stamp.resize((sw, sh), Image.LANCZOS)
        sx, sy = (W - sw) // 2, (H - sh) // 2
        dark = Image.new("RGB", stamp.size, hex_rgb(CFG["bg"]))
        img.paste(dark, (sx, sy), stamp)
    else:  # no stamp file: plain rounded panel
        sw = int(sh * 0.63)
        sx, sy = (W - sw) // 2, (H - sh) // 2
        ImageDraw.Draw(img).rounded_rectangle((sx, sy, sx + sw, sy + sh), 40, fill=hex_rgb(CFG["bg"]))
    draw = ImageDraw.Draw(img)
    fg = hex_rgb(CFG["fg"])

    # safe area inside the waves (measured from the stamp artwork)
    x0, x1 = sx + int(sw * 0.15), sx + int(sw * 0.85)
    top, bottom = sy + int(sh * 0.15), sy + int(sh * 0.88)
    inner_w = x1 - x0
    y = top

    # wordmark
    wm = asset("wordmark.png")
    if wm:
        ww = int(inner_w * 0.62)
        wm = wm.resize((ww, int(wm.height * ww / wm.width)), Image.LANCZOS)
        white = Image.new("RGB", wm.size, fg)
        img.paste(white, (x0, y), wm)
        y += wm.height + int(sh * 0.05)
    else:
        draw.text((x0, y), CFG["station_name"], font=font(44), fill=fg)
        y += 90

    # optional artwork
    art = load_artwork(show.image_url)
    if art and fmt == "story":
        side = inner_w
        img.paste(ImageOps.fit(art, (side, side)), (x0, y))
        y += side + 40

    # badge, in the show's colour
    badge = "LIVE NOW" if kind == "live_now" else "COMING UP"
    bf = font(40 if fmt == "feed" else 44)
    pad = 58 if kind == "live_now" else 28
    bh = int(bf.size * 1.75)
    bw = int(draw.textlength(badge, font=bf)) + pad + 28
    draw.rounded_rectangle((x0, y, x0 + bw, y + bh), radius=bh // 2, fill=colour)
    if kind == "live_now":
        cy = y + bh // 2
        draw.ellipse((x0 + 24, cy - 9, x0 + 42, cy + 9), fill=(225, 35, 35) if ink == (0, 0, 0) else (255, 255, 255))
    draw.text((x0 + pad, y + bh // 2), badge, font=bf, fill=ink, anchor="lm")
    y += bh + 36

    # footer lines are laid out from the bottom up
    local_start = show.start.astimezone(CFG["tz"])
    t_end = show.end.astimezone(CFG["tz"])
    if kind == "live_now":
        when = "ON AIR NOW" if show.source == "live" else f"ON AIR UNTIL {t_end:%H:%M}"
    else:
        when = f"{local_start:%a %-d %b}  {local_start:%H:%M}–{t_end:%H:%M}".upper()
    wf = font(38 if fmt == "feed" else 44)
    uf = font(28 if fmt == "feed" else 32)
    pill_h = int(uf.size * 1.9)
    url_y = bottom - pill_h
    when_y = url_y - int(wf.size * 1.7)
    draw.line((x0, when_y - 28, x1, when_y - 28), fill=fg, width=3)
    draw.text((x0, when_y), when, font=wf, fill=fg)
    # "link in bio" pill in the show's colour
    lt = CFG["link_text"]
    while draw.textlength(lt, font=uf) + 56 > inner_w and uf.size > 18:
        uf = font(uf.size - 2)
    pw = int(draw.textlength(lt, font=uf)) + 56
    draw.rounded_rectangle((x0, url_y, x0 + pw, url_y + pill_h), radius=pill_h // 2, fill=colour)
    draw.text((x0 + 28, url_y + pill_h // 2), lt, font=uf, fill=ink, anchor="lm")

    # title + host fill the middle
    room = when_y - 60 - y
    host_h = 80 if show.host else 0
    max_lines = 4
    start_size = 150 if fmt == "story" else 124
    tf, lines = fit_text(draw, show.title.upper(), inner_w, max_lines, start_size, 44)
    while len(lines) * tf.size * 1.05 + host_h > room and tf.size > 44:
        tf, lines = fit_text(draw, show.title.upper(), inner_w, max_lines, tf.size - 6, 44)
    for line in lines:
        draw.text((x0, y), line, font=tf, fill=fg)
        y += int(tf.size * 1.05)
    if show.host:
        y += 22
        hf, hl = fit_text(draw, f"with {show.host}", inner_w, 1, 46 if fmt == "story" else 40, 28)
        draw.text((x0, y), hl[0], font=font(hf.size, bold=False), fill=fg)

    OUT.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", show.title.lower()).strip("-")[:40] or "show"
    path = OUT / f"{local_start:%Y%m%d-%H%M}-{slug}-{kind}-{fmt}.jpg"
    img.save(path, "JPEG", quality=92)
    return path


def caption(show: Show, kind: str) -> str:
    local = show.start.astimezone(CFG["tz"])
    tmpl = CFG["caption_live_now"] if kind == "live_now" else CFG["caption_coming_up"]
    return tmpl.replace("\\n", "\n").format(
        show=show.title,
        host=show.host,
        host_line=f" with {show.host}" if show.host else "",
        time=f"{local:%H:%M}",
        day=f"{local:%A}",
        end=f"{show.end.astimezone(CFG['tz']):%H:%M}",
        hashtags=CFG["hashtags"],
    ).strip()


# --------------------------------------------------------------------------- hosting
def publish_image(path: Path) -> str:
    """Put the card somewhere public and return its URL (Instagram fetches it)."""
    if CFG["host"] == "folder":
        if not (CFG["folder_path"] and CFG["folder_url"]):
            raise RuntimeError("Set PUBLIC_IMAGE_DIR and PUBLIC_IMAGE_BASE_URL")
        dest = Path(CFG["folder_path"]) / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
        return CFG["folder_url"].rstrip("/") + "/" + path.name

    # github: commit the image to a branch of a public repo, serve via raw URL
    repo, token, branch = CFG["gh_repo"], CFG["gh_token"], CFG["gh_branch"]
    if not (repo and token):
        raise RuntimeError("Set GITHUB_REPOSITORY and GH_CONTENTS_TOKEN (or run in GitHub Actions)")
    api = f"https://api.github.com/repos/{repo}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", **UA}
    # make sure the branch exists
    if requests.get(f"{api}/branches/{branch}", headers=h, timeout=20).status_code == 404:
        default = requests.get(api, headers=h, timeout=20).json()["default_branch"]
        sha = requests.get(f"{api}/git/ref/heads/{default}", headers=h, timeout=20).json()["object"]["sha"]
        requests.post(f"{api}/git/refs", headers=h, timeout=20,
                      json={"ref": f"refs/heads/{branch}", "sha": sha}).raise_for_status()
    remote = f"cards/{path.name}"
    r = requests.put(f"{api}/contents/{remote}", headers=h, timeout=30, json={
        "message": f"card: {path.name}",
        "content": base64.b64encode(path.read_bytes()).decode(),
        "branch": branch,
    })
    if r.status_code not in (200, 201, 422):  # 422 = already exists
        r.raise_for_status()
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{remote}"
    for _ in range(10):  # wait until it's actually served
        if requests.head(url, timeout=15).status_code == 200:
            break
        time.sleep(3)
    return url


# --------------------------------------------------------------------------- instagram
def ig_post(image_url: str, text: str, fmt: str) -> str:
    base = f"https://{CFG['graph_host']}/{CFG['graph']}"
    uid, tok = CFG["ig_user_id"], CFG["ig_token"]
    if not tok:
        raise RuntimeError("Set IG_ACCESS_TOKEN")
    if not uid:  # work it out from the token, so IG_USER_ID is optional
        me = requests.get(f"{base}/me", params={"fields": "user_id,username", "access_token": tok},
                          timeout=30)
        if not me.ok:
            raise RuntimeError(f"IG token check failed: {me.text}")
        uid = CFG["ig_user_id"] = str(me.json().get("user_id") or me.json()["id"])
        log(f"Instagram account: @{me.json().get('username', '?')} ({uid})")
    data = {"image_url": image_url, "access_token": tok}
    if fmt == "story":
        data["media_type"] = "STORIES"
    else:
        data["caption"] = text
    r = requests.post(f"{base}/{uid}/media", data=data, timeout=60)
    if not r.ok:
        raise RuntimeError(f"IG create failed: {r.text}")
    creation = r.json()["id"]
    for _ in range(20):  # wait for Instagram to process the image
        s = requests.get(f"{base}/{creation}", params={"fields": "status_code", "access_token": tok},
                         timeout=30).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"IG processing error: {s}")
        time.sleep(3)
    r = requests.post(f"{base}/{uid}/media_publish",
                      data={"creation_id": creation, "access_token": tok}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"IG publish failed: {r.text}")
    return r.json()["id"]


# --------------------------------------------------------------------------- state
def state_path(dry: bool) -> Path:
    f = CFG["state_file"]
    return f.with_name(f.stem + ".dry.json") if dry else f


def load_state(dry: bool = False) -> dict:
    try:
        return json.loads(state_path(dry).read_text())
    except Exception:  # noqa: BLE001
        return {"posted": {}}


def save_state(state: dict, dry: bool = False) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    state["posted"] = {k: v for k, v in state["posted"].items() if v.get("at", "") > cutoff}
    state_path(dry).write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- main
def formats(setting: str) -> list[str]:
    return {"feed": ["feed"], "story": ["story"], "both": ["feed", "story"]}.get(setting, [])


def announce(show: Show, kind: str, state: dict, dry: bool) -> None:
    setting = CFG["live_now_as"] if kind == "live_now" else CFG["coming_up_as"]
    for fmt in formats(setting):
        key = f"{show.key}:{kind}:{fmt}"
        if key in state["posted"]:
            continue
        card = render_card(show, kind, fmt)
        text = caption(show, kind)
        if dry:
            log(f"[dry-run] {kind} {fmt}: {show.title} -> {card.name}\n    {text!r}")
            state["posted"][key] = {"at": datetime.now(timezone.utc).isoformat(), "dry": True}
            continue
        try:
            url = publish_image(card)
            media_id = ig_post(url, text, fmt)
            log(f"posted {kind} {fmt}: {show.title} (media {media_id})")
            state["posted"][key] = {"at": datetime.now(timezone.utc).isoformat(), "id": media_id}
        except Exception as e:  # noqa: BLE001
            log(f"FAILED {kind} {fmt} for {show.title}: {e}")


def check_token() -> None:
    """Dry-run helper: prove the Instagram token works without posting anything."""
    if not CFG["ig_token"]:
        log("Instagram token: not set (IG_ACCESS_TOKEN)")
        return
    base = f"https://{CFG['graph_host']}/{CFG['graph']}"
    try:
        r = requests.get(f"{base}/me", params={"fields": "user_id,username",
                                              "access_token": CFG["ig_token"]}, timeout=30)
        if r.ok:
            log(f"Instagram token OK: @{r.json().get('username', '?')}")
        else:
            log(f"Instagram token PROBLEM: {r.text[:300]}")
    except Exception as e:  # noqa: BLE001
        log(f"Instagram token check failed ({e})")


def run(dry: bool, now: datetime | None = None, shows: list[Show] | None = None,
        status: dict | None = None) -> None:
    if dry and now is None:
        check_token()
    now = now or datetime.now(timezone.utc)
    state = load_state(dry)
    if shows is None:
        shows = get_schedule()
        nxt = [s for s in shows if s.end > now][:3]
        for s in nxt:
            log(f"next: {s.start.astimezone(CFG['tz']):%a %H:%M} {s.title}")
    lead = timedelta(minutes=CFG["coming_up_lead"])
    grace = timedelta(minutes=CFG["live_grace"])

    on_now = None
    for s in shows:
        if s.start - lead <= now < s.start:
            announce(s, "coming_up", state, dry)
        if s.start <= now < min(s.start + grace, s.end):
            announce(s, "live_now", state, dry)
        if s.start <= now < s.end:
            on_now = s

    # A DJ broadcasting live on Radio.co with nothing scheduled: one post per session
    if CFG["unscheduled_live"]:
        st = radioco_status() if status is None else status
        src = st.get("source") or {}
        if src.get("type") == "live" and on_now is None:
            collab = src.get("collaborator")
            who = collab.get("name", "") if isinstance(collab, dict) else ""
            since = state.get("live_since") or now.isoformat()
            state["live_since"] = since
            begun = datetime.fromisoformat(since)
            live = Show(title=who or "Live DJ set", start=begun, end=begun + timedelta(hours=1),
                        source="live")
            announce(live, "live_now", state, dry)
        elif src and src.get("type") != "live":
            state.pop("live_since", None)  # session over; next live set gets a new post

    save_state(state, dry)


def preview() -> None:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    sample = [
        (Show("The Improvement Room", now + timedelta(hours=1), now + timedelta(hours=2),
              "Chris Bruce", colour="#fcc419"), "coming_up"),
        (Show("The Weekly Digress", now, now + timedelta(hours=1), "Emmet O'Donnell",
              colour="#ff8fab"), "live_now"),
        (Show("Surprise DJ", now, now + timedelta(hours=1), source="live"), "live_now"),
    ]
    for s, kind in sample:
        for fmt in ("feed", "story"):
            print(render_card(s, kind, fmt))
        print(caption(s, kind), "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="render cards, don't post")
    ap.add_argument("--preview", action="store_true", help="render sample cards and exit")
    ap.add_argument("--list", action="store_true", help="print the upcoming schedule and exit")
    a = ap.parse_args()
    if a.preview:
        preview()
    elif a.list:
        for s in get_schedule():
            loc = s.start.astimezone(CFG["tz"])
            print(f"{loc:%a %d %b %H:%M}  {s.title}  {('— ' + s.host) if s.host else ''}  [{s.source}]")
    else:
        run(dry=a.dry_run or env_bool("DRY_RUN", False))
