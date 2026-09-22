#!/usr/bin/env python3
"""
Extra Ditch Channel posts, alongside the per-show Stories in ditch_poster.py:

  weekly     one card listing this week's shows (premieres + the Saturday omnibus)
  listen back  a Story for each new upload on mixcloud.com/theditch

Both reuse the cards, posting and receipts from ditch_poster.py, so nothing posts twice.
ditch_poster.run() calls extras_pass() at the end of every check.
"""
from __future__ import annotations


import re
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageOps

import ditch_poster as dp
from ditch_poster import CFG, Show, hex_rgb, text_on, font, fit_text, asset

MIXCLOUD_USER = dp.env("MIXCLOUD_USER", "theditch")
WEEKLY_AS = dp.env("WEEKLY_AS", "both")          # feed | story | both | off
WEEKLY_DAY = int(dp.env("WEEKLY_DAY", "0"))      # 0 = Monday
WEEKLY_HOUR = int(dp.env("WEEKLY_HOUR", "10"))
LISTEN_BACK_AS = dp.env("LISTEN_BACK_AS", "story")
MAX_LISTEN_BACK = int(dp.env("MAX_LISTEN_BACK_PER_RUN", "4"))
# video backgrounds, same clip pool as the show Stories. Stories only: the clips are cropped 9:16.
WEEKLY_VIDEO = dp.env_bool("WEEKLY_VIDEO", True)
LISTEN_BACK_VIDEO = dp.env_bool("LISTEN_BACK_VIDEO", True)


# --------------------------------------------------------------------------- weekly line-up
def week_shows(now: datetime) -> tuple[list[Show], datetime]:
    """This week's shows, one row per show: premieres plus the omnibus, repeats dropped."""
    tz = CFG["tz"]
    local = now.astimezone(tz)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    shows = [s for s in dp.get_schedule()
             if start <= s.start.astimezone(tz) < end]
    seen, rows = set(), []
    for s in sorted(shows, key=lambda s: s.start):
        if s.title.lower() in seen:
            continue  # the same show airs again later in the week; list it once
        seen.add(s.title.lower())
        rows.append(s)
    return rows, start


def render_weekly(rows: list[Show], week_start: datetime, fmt: str = "feed",
                  transparent: bool = False) -> "dp.Path":
    W, H = (1080, 1350) if fmt == "feed" else (1080, 1920)
    colour = hex_rgb(CFG["accent"])
    # transparent = just the stamp and its text, to sit over a video background
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0)) if transparent else Image.new("RGB", (W, H), colour)
    stamp = asset("stamp.png")
    sh = int(H * (0.92 if fmt == "feed" else 0.80))
    if stamp:
        sw = int(stamp.width * sh / stamp.height)
        stamp = stamp.resize((sw, sh), Image.LANCZOS)
        sx, sy = (W - sw) // 2, (H - sh) // 2
        img.paste(Image.new("RGB", stamp.size, hex_rgb(CFG["bg"])), (sx, sy), stamp)
    else:
        sw = int(sh * 0.63); sx, sy = (W - sw) // 2, (H - sh) // 2
    draw = ImageDraw.Draw(img)
    fg = hex_rgb(CFG["fg"])
    x0, x1 = sx + int(sw * 0.15), sx + int(sw * 0.85)
    inner = x1 - x0
    y = sy + int(sh * 0.14)

    wm = asset("wordmark.png")
    if wm:
        ww = int(inner * 0.5)
        wm = wm.resize((ww, int(wm.height * ww / wm.width)), Image.LANCZOS)
        img.paste(Image.new("RGB", wm.size, fg), (x0, y), wm)
        y += wm.height + int(sh * 0.035)

    bf = font(38 if fmt == "feed" else 44)
    label = f"THIS WEEK · {week_start:%-d %b}"
    bw = int(draw.textlength(label, font=bf)) + 56
    bh = int(bf.size * 1.75)
    draw.rounded_rectangle((x0, y, x0 + bw, y + bh), radius=bh // 2, fill=colour)
    draw.text((x0 + 28, y + bh // 2), label, font=bf, fill=text_on(colour), anchor="lm")
    y += bh + int(sh * 0.045)

    # one line per show: day and time, then the name
    room = (sy + int(sh * 0.86)) - y
    step = room // max(1, len(rows))
    tf = font(min(54, int(step * 0.46)))
    df = font(min(34, int(step * 0.30)), bold=False)
    for s in rows:
        local = s.start.astimezone(CFG["tz"])
        when = f"{local:%a}".upper() + f"  {local:%H:%M}"
        draw.text((x0, y), when, font=df, fill=hex_rgb(s.colour or CFG["accent"]))
        name, _ = fit_text(draw, s.title.upper(), inner, 1, tf.size, 30)
        draw.text((x0, y + int(df.size * 1.25)), s.title.upper(), font=name, fill=fg)
        y += step

    ff = font(30 if fmt == "feed" else 34, bold=False)
    draw.text((x0, sy + int(sh * 0.90)), "EVERY SHOW, ALL WEEK · DITCH.CHANNEL", font=ff, fill=fg)

    dp.OUT.mkdir(exist_ok=True)
    stem = f"{week_start:%Y%m%d}-week-{fmt}"
    if transparent:
        path = dp.OUT / f"{stem}-overlay.png"
        img.save(path, "PNG")
    else:
        path = dp.OUT / f"{stem}.jpg"
        img.save(path, "JPEG", quality=92)
    return path


def weekly_asset(rows: list[Show], week_start: datetime, fmt: str) -> "dp.Path":
    """The weekly card as a video when clips are available and it's going out as a Story."""
    if fmt == "story" and WEEKLY_VIDEO and CFG["video"]:
        vid = dp.video_from_overlay(render_weekly(rows, week_start, "story", transparent=True))
        if vid:
            return vid
    return render_weekly(rows, week_start, fmt)


def weekly_caption(rows: list[Show], week_start: datetime) -> str:
    lines = [f"{s.start.astimezone(CFG['tz']):%a %H:%M}  {s.title}" + (f" — {s.host}" if s.host else "")
             for s in rows]
    return ("This week on Ditch Channel\n\n" + "\n".join(lines) +
            f"\n\nListen live at ditch.channel {CFG['hashtags']}")


# --------------------------------------------------------------------------- mixcloud
def new_uploads(since_hours: int = 72) -> list[dict]:
    url = f"https://api.mixcloud.com/{MIXCLOUD_USER}/cloudcasts/?limit=20"
    r = requests.get(url, headers=dp.UA, timeout=30)
    r.raise_for_status()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    out = []
    for c in r.json().get("data", []):
        made = dp.parse_dt(c.get("created_time", ""), CFG["tz"])
        if made >= cutoff:
            out.append({"name": c.get("name", ""), "key": c.get("key", ""), "made": made,
                        "art": (c.get("pictures") or {}).get("extra_large")
                        or (c.get("pictures") or {}).get("large", "")})
    return sorted(out, key=lambda c: c["made"])


def known_names(state: dict) -> tuple[set[str], set[str]]:
    """Show titles and host names we have seen on the Radio.co schedule."""
    titles, hosts = set(), set()
    for v in (state.get("known") or {}).values():
        if v.get("title"):
            titles.add(v["title"].strip().lower())
        if v.get("host"):
            hosts.add(v["host"].strip().lower())
    return titles, hosts


def split_name(name: str, state: dict | None = None) -> tuple[str, str]:
    """Split a Mixcloud title into the show and the line under it.

    Mixcloud titles aren't consistent: 'Chris Bruce - The Improvement Room Episode 2' puts the
    host first, 'Mostly Listening 22 - Haruomi Hosono' puts the show first. So rather than guess
    by position, match each side against the shows and hosts from the Radio.co schedule. If
    neither side is recognised, use the whole title: plain, but never wrong."""
    name = name.strip()
    if " - " not in name:
        return name, ""
    left, right = (p.strip() for p in name.split(" - ", 1))
    titles, hosts = known_names(state or {})

    def is_show(part: str) -> bool:
        p = part.lower()
        return any(t in p for t in titles)

    if is_show(left) and not is_show(right):
        return left, right          # 'Mostly Listening 22 - Haruomi Hosono'
    if is_show(right) and not is_show(left):
        return right, left          # 'Chris Bruce - The Improvement Room Episode 2'
    if left.lower() in hosts:
        return right, left
    if right.lower() in hosts:
        return left, right
    return name, ""


def render_listen_back(title: str, host: str, art_url: str, fmt: str = "story",
                       transparent: bool = False) -> "dp.Path":
    W, H = (1080, 1920) if fmt == "story" else (1080, 1350)
    colour = hex_rgb(CFG["accent"])
    art = dp.load_artwork(art_url)
    if transparent:  # just the stamp and its text: the clip is the background
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    elif art:  # the mix artwork fills the background, dimmed so the stamp still reads
        back = ImageOps.fit(art, (W, H)).filter(ImageFilter.GaussianBlur(30))
        img = Image.blend(back, Image.new("RGB", (W, H), hex_rgb(CFG["bg"])), 0.35)
    else:
        img = Image.new("RGB", (W, H), colour)
    stamp = asset("stamp.png")
    sh = int(H * (0.78 if fmt == "story" else 0.90))
    if stamp:
        sw = int(stamp.width * sh / stamp.height)
        stamp = stamp.resize((sw, sh), Image.LANCZOS)
        sx, sy = (W - sw) // 2, (H - sh) // 2
        img.paste(Image.new("RGB", stamp.size, hex_rgb(CFG["bg"])), (sx, sy), stamp)
    else:
        sw = int(sh * 0.63); sx, sy = (W - sw) // 2, (H - sh) // 2
    draw = ImageDraw.Draw(img)
    fg = hex_rgb(CFG["fg"])
    x0, x1 = sx + int(sw * 0.15), sx + int(sw * 0.85)
    inner = x1 - x0
    y = sy + int(sh * 0.15)

    wm = asset("wordmark.png")
    if wm:
        ww = int(inner * 0.6)
        wm = wm.resize((ww, int(wm.height * ww / wm.width)), Image.LANCZOS)
        img.paste(Image.new("RGB", wm.size, fg), (x0, y), wm)
        y += wm.height + int(sh * 0.05)

    bf = font(44)
    bw = int(draw.textlength("LISTEN BACK", font=bf)) + 56
    bh = int(bf.size * 1.75)
    draw.rounded_rectangle((x0, y, x0 + bw, y + bh), radius=bh // 2, fill=colour)
    draw.text((x0 + 28, y + bh // 2), "LISTEN BACK", font=bf, fill=text_on(colour), anchor="lm")
    y += bh + 40

    if art:  # a sharp square of the mix artwork
        side = int(inner * 0.55)
        img.paste(ImageOps.fit(art, (side, side)), (x0, y))
        y += side + 40

    tf, lines = fit_text(draw, title.upper(), inner, 3, 110, 44)
    for line in lines:
        draw.text((x0, y), line, font=tf, fill=fg)
        y += int(tf.size * 1.05)
    if host:  # already worded by the caller: "with Chris Bruce", or an episode subtitle
        draw.text((x0, y + 16), host, font=font(44, bold=False), fill=fg)

    bottom = sy + int(sh * 0.88)
    uf = font(30)
    ph = int(uf.size * 1.9)
    label = dp.env("MIXCLOUD_LINK_TEXT", "FULL SHOW ON MIXCLOUD · LINK IN BIO")
    while draw.textlength(label, font=uf) + 56 > inner and uf.size > 18:
        uf = font(uf.size - 2)
    pw = int(draw.textlength(label, font=uf)) + 56
    draw.rounded_rectangle((x0, bottom - ph, x0 + pw, bottom), radius=ph // 2, fill=colour)
    draw.text((x0 + 28, bottom - ph // 2), label, font=uf, fill=text_on(colour), anchor="lm")

    dp.OUT.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "mix"
    stem = f"listenback-{slug}-{fmt}"
    if transparent:
        path = dp.OUT / f"{stem}-overlay.png"
        img.save(path, "PNG")
    else:
        path = dp.OUT / f"{stem}.jpg"
        img.save(path, "JPEG", quality=92)
    return path


def listen_back_asset(title: str, host: str, art_url: str, fmt: str = "story") -> "dp.Path":
    """The listen-back card as a video when clips are available and it's going out as a Story."""
    if fmt == "story" and LISTEN_BACK_VIDEO and CFG["video"]:
        vid = dp.video_from_overlay(
            render_listen_back(title, host, art_url, "story", transparent=True))
        if vid:
            return vid
    return render_listen_back(title, host, art_url, fmt)


# --------------------------------------------------------------------------- posting
def send(key: str, card: "dp.Path", text: str, fmt: str, state: dict, dry: bool) -> None:
    """Post one ready-made card, with the same dedupe and receipts as the show Stories."""
    if key in state["posted"]:
        return
    now = datetime.now(timezone.utc).isoformat()
    if dry:
        dp.log(f"[dry-run] {key} -> {card.name}\n    {text!r}")
        state["posted"][key] = {"at": now, "dry": True}
        return
    if dp.already_published(key):
        dp.log(f"already posted earlier: {key}")
        state["posted"][key] = {"at": now, "seen": True}
        return
    try:
        media_id = dp.ig_post(dp.publish_image(card), text, fmt)
        dp.log(f"posted {key} (media {media_id})")
        state["posted"][key] = {"at": now, "id": media_id}
        dp.write_receipt(key, media_id)
    except Exception as e:  # noqa: BLE001  one failure must not stop the other posts
        dp.log(f"FAILED {key}: {e}")


def do_weekly(now: datetime, state: dict, dry: bool) -> None:
    """Monday morning: one line-up post for the week ahead."""
    fmts = dp.formats(WEEKLY_AS)
    if not fmts:
        return
    local = now.astimezone(CFG["tz"])
    if local.weekday() != WEEKLY_DAY or local.hour < WEEKLY_HOUR:
        return
    rows, week_start = week_shows(now)
    if not rows:
        dp.log("weekly: no shows in the schedule this week, skipping")
        return
    text = weekly_caption(rows, week_start)
    for fmt in fmts:
        key = f"weekly:{week_start:%Y%m%d}:{fmt}"
        if key in state["posted"]:
            continue
        send(key, weekly_asset(rows, week_start, fmt), text, fmt, state, dry)


def do_listen_back(state: dict, dry: bool) -> None:
    """New uploads on Mixcloud (we archive on Sundays): a Story each, a few per pass."""
    fmts = dp.formats(LISTEN_BACK_AS)
    if not fmts:
        return
    try:
        uploads = new_uploads()
    except Exception as e:  # noqa: BLE001
        dp.log(f"mixcloud check failed ({e})")
        return
    sent = 0
    for c in uploads:
        if sent >= MAX_LISTEN_BACK:
            dp.log(f"listen back: {len(uploads) - sent} more to go, next pass")
            return
        title, extra = split_name(c["name"], state)
        _, hosts = known_names(state)
        host = f"with {extra}" if extra.lower() in hosts else extra
        slug = re.sub(r"[^a-z0-9]+", "-", c["key"].lower()).strip("-")
        posted_any = False
        for fmt in fmts:
            key = f"mixcloud:{slug}:{fmt}"
            if key in state["posted"]:
                continue
            send(key, listen_back_asset(title, host, c["art"], fmt),
                 listen_back_caption(title, host), fmt, state, dry)
            posted_any = True
        if posted_any:
            sent += 1


def listen_back_caption(title: str, host: str) -> str:
    who = f", {host}," if host else ""
    return (f"{title}{who} is up on Mixcloud now.\n\n"
            f"Every Ditch show archived at mixcloud.com/{MIXCLOUD_USER} {CFG['hashtags']}")


def extras_pass(state: dict, dry: bool, now: datetime | None = None) -> None:
    """Called at the end of every ditch_poster pass."""
    now = now or datetime.now(timezone.utc)
    do_weekly(now, state, dry)
    do_listen_back(state, dry)
