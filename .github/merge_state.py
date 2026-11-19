#!/usr/bin/env python3
"""Fold this run's post receipts into the ones already on main.

Runs overlap by design: a dispatch replaces the watcher, and cron fires while one is
still going. Each run holds its own copy of state.json, so rebasing one onto the other
hits a conflict, the push fails, and the losing run's receipts are thrown away — which is
exactly how a show ends up posted twice. So keep both sides rather than picking one.

Called with the directory holding this run's copies; the files in the working tree are
main's. Where both know about the same show, this run wins: it is the fresher of the two.
"""
from __future__ import annotations

import json
import pathlib
import sys

mine_dir = pathlib.Path(sys.argv[1])

for name in ("state.json", "state.dry.json"):
    src = mine_dir / name
    if not src.exists():
        continue
    mine = json.loads(src.read_text() or "{}")
    dst = pathlib.Path(name)
    theirs = json.loads(dst.read_text() or "{}") if dst.exists() else {}

    out = dict(theirs)
    for section in ("posted", "known"):
        merged = dict(theirs.get(section) or {})
        merged.update(mine.get(section) or {})
        if merged:
            out[section] = merged
    for key, value in mine.items():           # anything else this run tracks
        out.setdefault(key, value)

    dst.write_text(json.dumps(out, indent=2))  # same shape save_state() writes
    gained = len(out.get("posted", {})) - len(theirs.get("posted", {}))
    print(f"{name}: {len(out.get('posted', {}))} receipts (+{gained} from this run), "
          f"{len(out.get('known', {}))} shows")
