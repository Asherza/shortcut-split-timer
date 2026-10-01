#!/usr/bin/env python3
"""Change a Shotcut project's timeline to just the best N laps plus a buffer.

Uses the same plain markers as split_timer.py / check_pb.py. Each clip is one attempt (use
"Split at Playhead" when a run fails): the first marker on a clip is the start of lap 1, each
later marker ends a lap. For every clip the fastest N consecutive laps (N = --laps, default 3)
are found. Then, by default (--keep best), the single fastest run in the whole project wins and
the timeline becomes just that: its best laps with --pre seconds before the start and --post
seconds after the finish (default 5 each), and every other clip is removed. With --keep all
every run is trimmed instead and all clips stay. The source video files are never touched.

The buffer stays inside the clip as it is on the timeline (footage past a split may show a
crash); --extend lets it borrow from the original media, never past the media itself. If a
clip has less footage than requested on a side, the report says so.

What gets updated so the project stays consistent:
  * the clip's in/out points and the timeline positions of anything that remains;
  * the markers: only the best-laps markers are kept, moved to their new positions;
  * fade in/out filters and other whole-clip filters (re-fitted to the new length; a panel
    from split_timer.py follows the clip, so run split_timer.py before or after this);
  * the project length, and any other-track clips (e.g. music) that now run past the end
    are shortened to fit.

    python trim_best.py <project.mlt> [--laps N] [--pre SEC] [--post SEC] [--keep best|all]
                        [--extend] [--out FILE] [--save]

By default it only PREVIEWS: nothing is written. Add --save to change the project (after a
backup). With --out FILE --save it writes to FILE instead and leaves your project alone.

Close the project in Shotcut first; saving from Shotcut later overwrites this. A backup of the
project goes into a `.trim-backups` folder next to it. A run with fewer than N laps is left
untrimmed.
"""

import argparse
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from split_timer import (DEFAULT_LAPS, MarkerError, best_window, get_prop, lap_times, paint,
                         project_fps, read_markers, runs_from_markers, seconds_to_tc,
                         tc_to_frames, tc_to_seconds)


def to_frames(tc, fps):
    return tc_to_frames(tc, fps)


def to_tc(frames, fps):
    return seconds_to_tc(frames / fps)


def refit_fade(f, new_len, fps):
    """Re-fit a Shotcut fade in/out filter (animated `level`) to a clip of `new_len` frames.
    Returns True when handled, False when the filter is not a simple fade we understand."""
    level = get_prop(f, "level")
    if not level or "=" not in level:
        return False
    a_in = int(get_prop(f, "shotcut:animIn") or 0)
    a_out = int(get_prop(f, "shotcut:animOut") or 0)
    values = [p.split("=")[1] for p in level.split(";")]
    end = (new_len - 1) / fps
    if a_out and not a_in and len(values) == 2:
        pairs = [(end - a_out / fps, values[0]), (end, values[1])]
    elif a_in and not a_out and len(values) == 2:
        pairs = [(0.0, values[0]), (a_in / fps, values[1])]
    elif a_in and a_out and len(values) == 4:
        pairs = [(0.0, values[0]), (a_in / fps, values[1]),
                 (end - a_out / fps, values[2]), (end, values[3])]
    else:
        return False
    for p in f.findall("property"):
        if p.get("name") == "level":
            p.text = ";".join(f"{seconds_to_tc(max(t, 0.0))}={v}" for t, v in pairs)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path)
    ap.add_argument("--laps", type=int, default=DEFAULT_LAPS, help=f"laps to keep (default {DEFAULT_LAPS})")
    ap.add_argument("--pre", type=float, default=5.0, help="seconds to keep before the start (default 5)")
    ap.add_argument("--post", type=float, default=5.0, help="seconds to keep after the finish (default 5)")
    ap.add_argument("--keep", choices=("best", "all"), default="best",
                    help="best (default): the timeline becomes just the single best run in the project; "
                         "all: trim every run but keep every clip")
    ap.add_argument("--extend", action="store_true",
                    help="let the buffer reach past where the clip currently ends, into the original "
                         "media (off by default, since that footage may show a crash)")
    ap.add_argument("--save", action="store_true",
                    help="write the changes (default is a preview that writes nothing)")
    ap.add_argument("--out", type=Path,
                    help="with --save, write to this file instead of editing the project")
    ap.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)   # old spelling: now the default
    args = ap.parse_args()
    project = args.project.resolve()

    try:
        tree = ET.parse(project)
        root = tree.getroot()
        fps = project_fps(root)
        marker_list = read_markers(root, fps)
        runs, warnings = runs_from_markers(root, marker_list, fps)
        if not runs:
            raise MarkerError("no runs found; put at least two markers on each clip to trim")
    except MarkerError as e:
        sys.exit(paint(f"{project.name}: {e}", "bred"))
    if not os.environ.get("RACE_FACADE"):        # race.py already showed these in its check step
        for w in warnings:
            print(paint(f"  Warning: {w}", "warn"))

    tractor = next(t for t in root.iter("tractor") if get_prop(t, "shotcut") == "1")
    playlists = {p.get("id"): p for p in root.findall("playlist")}
    tracks = [playlists[t.get("producer")] for t in tractor.findall("track")
              if t.get("producer") in playlists]
    video = [p for p in tracks if get_prop(p, "shotcut:video") == "1"]
    if not video:
        sys.exit(paint(f"{project.name}: no video track found", "bred"))
    v1 = video[0]
    if len(video) > 1:
        print(paint("  Warning: more than one video track; only the first (V1) is trimmed", "warn"))

    pre_f, post_f = round(args.pre * fps), round(args.post * fps)

    # Old layout of V1, and what each run wants to keep (in source frames).
    layout, pos = [], 0
    for item in v1:
        if item.tag == "blank":
            n = to_frames(item.get("length"), fps)
            layout.append({"item": item, "kind": "blank", "pos": pos, "len": n})
            pos += n
        elif item.tag == "entry":
            a, b = to_frames(item.get("in"), fps), to_frames(item.get("out"), fps)
            layout.append({"item": item, "kind": "entry", "pos": pos, "len": b - a + 1,
                           "a": a, "b": b, "producer": item.get("producer")})
            pos += b - a + 1
    old_total = pos

    plans = {}                                   # id(entry item) -> plan
    for run in runs:
        chain, off, clip_in, clip_out = run["clip"]
        entry = next((e for e in layout if e["kind"] == "entry" and e["producer"] == chain.get("id")
                      and e["a"] - e["pos"] == off), None)
        name = get_prop(chain, "resource") or chain.get("id")
        if entry is None:
            print(paint(f"  Skipping {name}: it is not on the first video track", "warn"))
            continue
        all_laps = lap_times(run["bounds"], fps)
        j = best_window(all_laps, args.laps)
        if j is None:
            print(paint(f"  {name}: only {len(all_laps)} lap(s), fewer than {args.laps}; left untrimmed",
                        "dim"))
            continue
        keep = run["bounds"][j:j + args.laps + 1]
        s_src, e_src = keep[0] + off, keep[-1] + off
        # By default the buffer stays inside the clip as it is on the timeline; --extend lets it
        # reach into the original media, but never past the media itself.
        lo_lim, hi_lim = clip_in, clip_out
        if args.extend:
            lo_lim = 0
            if chain.get("out"):
                hi_lim = max(hi_lim, to_frames(chain.get("out"), fps))
            if get_prop(chain, "length"):
                hi_lim = max(hi_lim, to_frames(get_prop(chain, "length"), fps) - 1)
        plans[id(entry["item"])] = {
            "entry": entry, "name": name, "keep": set(keep), "off": off,
            "new_in": max(lo_lim, s_src - pre_f), "new_out": min(hi_lim, e_src + post_f),
            "window": (j + 1, j + args.laps, len(all_laps)), "chain": chain,
            "total": sum(all_laps[j:j + args.laps])}

    if not plans:
        sys.exit(paint(f"{project.name}: nothing to trim", "bred"))

    # keep=best: the single fastest run wins and everything else leaves the timeline.
    if args.keep == "best":
        winner = min(plans.values(), key=lambda p: p["total"])
        for other in plans.values():
            if other is not winner:
                print(paint(f"  {other['name']}: best {args.laps} laps {other['total']:.2f}s "
                            f"(not the fastest, removed)", "dim"))
        plans = {id(winner["entry"]["item"]): winner}
        for e in layout:
            e["removed"] = e is not winner["entry"]

    # New layout: trimmed entries shrink, removed ones vanish, everything after slides left.
    new_pos = 0
    for e in layout:
        e["new_pos"] = new_pos
        p = plans.get(id(e["item"]))
        if e.get("removed"):
            e["new_len"] = 0
        elif p:
            e["new_a"], e["new_b"] = p["new_in"], p["new_out"]
            e["new_len"] = p["new_out"] - p["new_in"] + 1
        else:
            e["new_len"] = e["len"]
        new_pos += e["new_len"]
    new_total = new_pos

    print(paint(f"{project.name}", "bcyan")
          + paint(f"  fps={fps:g}  keeping best {args.laps} laps + up to {args.pre:g}s before / "
                  f"{args.post:g}s after  ({'only the best run' if args.keep == 'best' else 'every run'})",
                  "dim"))
    for p in plans.values():
        e = p["entry"]
        lo, hi, n = p["window"]
        before = (min(p["keep"]) + p["off"] - p["new_in"]) / fps
        after = (p["new_out"] - (max(p["keep"]) + p["off"])) / fps
        print(paint(f"  {p['name']}", "bold") + f": laps {lo}-{hi} of {n} kept = "
              + paint(f"{p['total']:.2f}s", "gold") + f"; clip {e['len'] / fps:.1f}s -> "
              + paint(f"{e['new_len'] / fps:.1f}s", "bgreen")
              + f" ({before:.1f}s before, {after:.1f}s after)")
        if before < args.pre - 0.05 or after < args.post - 0.05:
            print(paint("    (less buffer than requested: the clip has no more footage on that side; "
                        "use --extend to borrow from the original media)", "warn"))
    print(f"  Project length: {old_total / fps:.1f}s -> " + paint(f"{new_total / fps:.1f}s", "bgreen"))

    # Markers: keep window markers on trimmed clips (moved), shift the rest.
    holder = next((x for x in tractor.findall("properties") if x.get("name") == "shotcut:markers"), None)
    kept_markers, dropped = [], 0
    if holder is not None:
        for m in list(holder):
            start = to_frames(get_prop(m, "start"), fps)
            end = to_frames(get_prop(m, "end") or get_prop(m, "start"), fps)
            owner = next((e for e in layout if e["kind"] == "entry"
                          and e["pos"] <= start < e["pos"] + e["len"]), None)
            if owner is None:                    # over a gap: shift with the rest of the timeline
                owner = next((e for e in reversed(layout) if e["pos"] <= start), None)
            plan = plans.get(id(owner["item"])) if owner else None
            if owner is not None and owner.get("removed"):
                dropped += 1
                continue
            if plan:
                if start not in plan["keep"]:
                    dropped += 1
                    continue
                shift = plan["entry"]["new_pos"] - (plan["new_in"] - plan["off"])
                new_start, new_end = start + shift, end + shift
            else:
                delta = (owner["new_pos"] - owner["pos"]) if owner else 0
                new_start, new_end = start + delta, end + delta
            kept_markers.append((m, new_start, new_end))
        print(f"  Markers: {len(kept_markers)} kept, " + paint(f"{dropped} dropped", "dim"))

    if not args.save or args.dry_run:
        where = f"to {args.out.name}" if args.out else "the project"
        hint = "" if os.environ.get("RACE_FACADE") else paint(f"Add --save to write {where}.", "bold")
        print(paint("Preview only: nothing written. ", "info") + hint)
        return

    # ---- apply -------------------------------------------------------------------
    for e in layout:
        if e.get("removed"):
            v1.remove(e["item"])
            continue
        p = plans.get(id(e["item"]))
        if not p:
            continue
        chain = p["chain"]
        old_in, old_out = e["a"], e["b"]
        e["item"].set("in", to_tc(e["new_a"], fps))
        e["item"].set("out", to_tc(e["new_b"], fps))
        for f in chain.findall("filter"):
            if f.get("in") is None or f.get("out") is None:
                continue
            f_in, f_out = to_frames(f.get("in"), fps), to_frames(f.get("out"), fps)
            # A filter edge that sat on the clip's old edge follows the clip's new edge, so
            # whole-clip filters (and the split_timer panel) still cover the whole clip.
            n_in = e["new_a"] if f_in == old_in else f_in
            n_out = e["new_b"] if f_out == old_out else f_out
            if (n_in, n_out) == (f_in, f_out):
                continue
            f.set("in", to_tc(n_in, fps))
            f.set("out", to_tc(n_out, fps))
            if n_in != f_in and get_prop(f, "mlt_service") == "timer":
                # A timer's `start` counts from the filter's own start, so keep it on the same frame.
                for sp in f.findall("property"):
                    if sp.get("name") == "start":
                        sp.text = seconds_to_tc(tc_to_seconds(sp.text) + (f_in - n_in) / fps)
            if (f_in, f_out) == (old_in, old_out) and (get_prop(f, "shotcut:animIn")
                                                       or get_prop(f, "shotcut:animOut")):
                if not refit_fade(f, e["new_len"], fps):
                    print(paint(f"  Warning: could not re-fit filter {f.get('id')}; check its "
                                "animation in Shotcut", "warn"))

    if holder is not None:
        for m in list(holder):
            holder.remove(m)
        for i, (m, s, en) in enumerate(kept_markers):
            m.set("name", str(i))
            for prop_el in m.findall("property"):
                if prop_el.get("name") == "start":
                    prop_el.text = to_tc(s, fps)
                elif prop_el.get("name") == "end":
                    prop_el.text = to_tc(en, fps)
            holder.append(m)

    # Project length: tractor, background, and any other-track clips that now overrun.
    tractor.set("out", to_tc(new_total - 1, fps))
    for pl in tracks:
        if pl is v1 or pl.get("id") == "background":     # the backdrop is fitted separately below
            continue
        pos = 0
        for item in list(pl):
            if item.tag == "blank":
                pos += to_frames(item.get("length"), fps)
                continue
            if item.tag != "entry":
                continue
            a, b = to_frames(item.get("in"), fps), to_frames(item.get("out"), fps)
            if pos >= new_total:
                pl.remove(item)
                print(paint(f"  Removed a clip on {get_prop(pl, 'shotcut:name') or pl.get('id')} "
                            "that started after the new end", "warn"))
            elif pos + (b - a + 1) > new_total:
                item.set("out", to_tc(a + (new_total - pos) - 1, fps))
                print(paint(f"  Shortened a clip on {get_prop(pl, 'shotcut:name') or pl.get('id')} "
                            f"to fit ({(b - a + 1) / fps:.1f}s -> {(new_total - pos) / fps:.1f}s)",
                            "warn"))
                pos = new_total
            else:
                pos += b - a + 1
    bg = playlists.get("background")
    if bg is not None:                            # the black backdrop always spans the project
        for item in bg:
            if item.tag != "entry":
                continue
            item.set("out", to_tc(new_total - 1, fps))
            base = next((p for p in root.findall("producer") if p.get("id") == item.get("producer")), None)
            if base is not None and to_frames(base.get("out"), fps) + 1 < new_total:
                base.set("out", to_tc(new_total - 1, fps))
                for p in base.findall("property"):
                    if p.get("name") == "length":
                        p.text = to_tc(new_total, fps)

    target = (args.out or project).resolve()
    if target == project:
        backup_dir = project.parent / ".trim-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{project.stem}.{datetime.now():%Y%m%dT%H%M%S}.mlt"
        shutil.copy2(project, backup)
        print(paint(f"Backup: {backup.name}", "dim"))
    ET.indent(tree, space="  ")
    tree.write(target, encoding="utf-8", xml_declaration=True)
    print(paint(f"Wrote {target.name}", "bgreen"))


if __name__ == "__main__":
    main()
