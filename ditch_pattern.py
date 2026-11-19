#!/usr/bin/env python3
"""Pattern cards: the show details set over a background built from the wordmark.

The backgrounds are the ones that were signed off. Their recipes are written out in full
below — ground, inks, and the exact size, skew and position of every render of the logo —
rather than being regenerated from a random seed, so they cannot drift if Python's random
number generator ever changes.

Everything is flat colour: a layer covers what is under it, nothing is blended, so every
colour on a card is one that was chosen.
"""
from __future__ import annotations

import re
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

import ditch_poster as dp
from ditch_poster import CFG, Show, hex_rgb, asset

W, H = 1080, 1920

# Instagram's Story furniture. The top band holds the progress bar, avatar and account
# name; the foot holds the reply bar and the viewer list. Keep the type out of both.
SAFE_TOP, SAFE_BOTTOM, SAFE_X = 250, 310, 72

# Roboto Mono is what the cards are set in. The workflow fetches it into assets/; these
# are the stand-ins for a machine that hasn't got it.
MONO_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]

SPECS = {
    "mint": {
        "ground": "#E3F6E9", "inks": ['#13CFA3', '#D782B2', '#A6EBE0', '#ACDAB9'],
        "layers": [
            dict(across=False, w=1644, h=3425, skew=-0.17189, x=-33, y=-693),
        ]},
    "olive": {
        "ground": "#49551A", "inks": ['#596BDE'],
        "layers": [
            dict(across=True, w=1815, h=1185, skew=0.0, x=-386, y=1288),
            dict(across=True, w=1537, h=1278, skew=0.0, x=293, y=-258),
            dict(across=True, w=1723, h=1089, skew=0.0, x=-295, y=-173),
        ]},
    "dark01": {
        "ground": "#122610", "inks": ['#8A753D', '#4F7121', '#DD257D'],
        "layers": [
            dict(across=True, w=1785, h=674, skew=0.13621, x=-337, y=-31),
        ]},
    "dark16": {
        "ground": "#0F410C", "inks": ['#5F3229'],
        "layers": [
            dict(across=True, w=2683, h=1433, skew=0.0, x=83, y=842),
            dict(across=False, w=756, h=3565, skew=0.0, x=-45, y=-862),
            dict(across=True, w=1881, h=1436, skew=0.0, x=221, y=1352),
            dict(across=False, w=1763, h=3958, skew=0.0, x=-276, y=-1098),
            dict(across=False, w=1723, h=2237, skew=0.0, x=78, y=-623),
        ]},
}


def mono(size: int):
    roboto = dp.find_asset("RobotoMono.ttf")
    for path in ([str(roboto)] if roboto.exists() else []) + MONO_PATHS:
        try:
            f = ImageFont.truetype(path, size)
        except OSError:
            continue
        try:
            f.set_variation_by_name("Bold")   # Roboto Mono ships as one variable file
        except (OSError, ValueError, AttributeError):
            pass                              # a static font is already the weight we want
        return f
    return dp.font(size)          # last resort: the Poppins the stamp cards use


def lum(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def background(name: str) -> Image.Image:
    spec = SPECS[name]
    src = asset("wordmark.png").convert("RGBA").split()[-1]
    img = Image.new("RGB", (W, H), hex_rgb(spec["ground"]))
    inks = spec["inks"]
    for i, L in enumerate(spec["layers"]):
        s = src.rotate(90, expand=True) if L["across"] else src
        m = s.resize((max(L["w"], 2), max(L["h"], 2)), Image.LANCZOS)
        k = L["skew"]
        if k:
            m = m.transform((m.width + int(abs(k) * m.height), m.height), Image.AFFINE,
                            (1, k, -k * m.height if k > 0 else 0, 0, 1, 0),
                            resample=Image.BICUBIC)
        c = Image.new("L", (W, H), 0)
        c.paste(m, (L["x"], L["y"]))
        img = Image.composite(Image.new("RGB", (W, H), hex_rgb(inks[i % len(inks)])), img, c)
    return img


def pick(show: Show) -> str:
    """Stable for a show on a given day, different between shows and between days."""
    forced = dp.env("PATTERN", "").lower()
    if forced in SPECS:
        return forced
    local = show.start.astimezone(CFG["tz"])
    names = sorted(SPECS)
    seed = sum(ord(ch) for ch in show.title.lower()) + local.timetuple().tm_yday
    return names[seed % len(names)]


def card(show: Show, kind: str) -> "dp.Path":
    img = background(pick(show))
    d = ImageDraw.Draw(img)
    s = show.start.astimezone(CFG["tz"])
    e = show.end.astimezone(CFG["tz"])

    title, ts = show.title.upper(), 88
    while ts > 40 and d.textlength(title, font=mono(ts)) > W * 0.56:
        ts -= 2

    rows = [("COMING UP" if kind == "coming_up" else "LIVE NOW", 44), (title, ts)]
    if show.host:
        rows.append(("WITH " + show.host.upper(), 44))
    rows.append(("", 28))
    if kind == "live_now":
        rows.append((("ON AIR NOW" if show.source == "live" else f"ON AIR UNTIL {e:%H.%M}"), 44))
    else:
        rows += [(f"{s:%a %-d %b}".upper(), 44), (f"{s:%H.%M}-{e:%H.%M}", 44)]
    rows += [("", 28), ("LINK IN BIO:", 44), ("DITCH.CHANNEL", 44)]

    wm = asset("wordmark.png")
    ww = int(W * 0.38)
    wm = wm.resize((ww, int(wm.height * ww / wm.width)), Image.LANCZOS)
    bh = wm.height + 32 + sum(int(sz * 1.3) for _, sz in rows)
    bw = int(max([wm.width] + [d.textlength(t, font=mono(sz)) for t, sz in rows if t]))

    # Instagram draws its own furniture over a Story: the avatar, account name and
    # progress bar across the top, the reply bar and viewer list across the foot. Nothing
    # we lay out may stray into those bands or it gets covered up.
    top, bot = SAFE_TOP, H - SAFE_BOTTOM - bh
    if bot < top:                      # an unusually tall block: centre it and take the hit
        top = bot = max(0, (H - bh) // 2)

    # Where the block goes is judged on two things: how busy the area is, and how far its
    # tone sits from the type's. A flat mid-tone panel is calm and still swallows black.
    grey = img.convert("L").resize((W // 8, H // 8), Image.BILINEAR)
    best = None
    for cx in (SAFE_X, int(W * 0.33)):
        for k in range(10):
            tx = max(SAFE_X, min(cx, W - SAFE_X - bw))
            ty = int(top + (bot - top) * k / 9)
            px = list(grey.crop((tx // 8, ty // 8, min(W, tx + bw) // 8,
                                 min(H, ty + bh) // 8)).getdata())
            mean = sum(px) / len(px)
            busy = (sum((v - mean) ** 2 for v in px) / len(px)) ** 0.5
            cand = (17, 17, 17) if mean > 128 else (250, 250, 248)
            score = busy * 1.6 - abs(mean - lum(cand))
            if best is None or score < best[0]:
                best = (score, tx, ty, cand)
    _, x, y, ink = best

    img.paste(Image.new("RGB", wm.size, ink), (x, y), wm)
    y += wm.height + 32
    for text, sz in rows:
        if text:
            d.text((x, y), text, font=mono(sz), fill=ink)
        y += int(sz * 1.3)

    dp.OUT.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", show.title.lower()).strip("-")[:40] or "show"
    path = dp.OUT / f"{s:%Y%m%d-%H%M}-{slug}-{kind}-pattern.jpg"
    img.save(path, "JPEG", quality=92)
    return path
