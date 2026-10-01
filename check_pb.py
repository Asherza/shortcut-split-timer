#!/usr/bin/env python3
"""Check whether the runs marked in your Shotcut projects are new personal bests.

Reads the plain markers on each clip (first = start, then one per lap end; see split_timer.py)
from one or more .mlt projects and, for every clip, finds the fastest N consecutive laps
(N = --laps, default 3) and tells you whether that beats your stored PB, and whether any
single lap beats your best lap. Nothing is changed unless you pass --save.

    python check_pb.py <project.mlt | folder> [more ...] [--laps N] [--track NAME]
                       [--bests FILE] [--save]

A folder is searched recursively for .mlt files (hidden and backup folders are skipped).
--track applies one bests key to every project; by default each project uses its file name
without .mlt (track_1.mlt -> "track_1"), the same as split_timer.py.
--save writes any new bests to the bests file, so a later split_timer.py run shows them.
"""

import argparse
import copy
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from split_timer import (DEFAULT_LAPS, MarkerError, analyze, apply_run_to_bests, best_window,
                         get_prop, lap_times, load_bests, paint, project_fps, read_markers,
                         runs_from_markers)


def find_projects(paths):
    found = []
    for p in paths:
        if p.is_dir():
            found += sorted(f for f in p.rglob("*.mlt")
                            if not any(part.startswith(".") for part in f.relative_to(p).parts))
        elif p.is_file():
            found.append(p)
        else:
            print(paint(f"Skipping {p}: not found", "warn"), file=sys.stderr)
    return found


def fmt(seconds):
    return f"{seconds:.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path, help=".mlt project files or folders to scan")
    ap.add_argument("--laps", type=int, default=DEFAULT_LAPS, help=f"laps in a PB (default {DEFAULT_LAPS})")
    ap.add_argument("--track", help="bests key for every project (default: each project's file name)")
    ap.add_argument("--bests", type=Path, help="bests JSON (default: bests.json next to this script)")
    ap.add_argument("--save", action="store_true", help="write new bests to the bests file")
    args = ap.parse_args()

    bests_path = (args.bests or Path(__file__).with_name("bests.json")).resolve()
    bests = load_bests(bests_path)
    work = copy.deepcopy(bests)                  # scratch copy the scan updates as it goes
    projects = find_projects([p.resolve() for p in args.paths])
    if not projects:
        sys.exit(paint("No .mlt projects found.", "bred"))

    pbs = []                                     # (track, file, run number, total, saved?) new PBs
    lap_records = []                             # (track, file, run number, lap seconds)
    scanned = 0
    for project in projects:
        track = args.track or project.stem
        try:
            root = ET.parse(project).getroot()
            fps = project_fps(root)
            runs, warnings = runs_from_markers(root, read_markers(root, fps), fps)
        except (MarkerError, ET.ParseError) as e:
            print(paint(f"{project}", "bcyan") + "\n" + paint(f"  skipped: {e}\n", "red"))
            continue
        if not runs:
            print(paint(f"{project}", "bcyan") + "\n" + paint("  no runs (put at least two markers "
                                                             "on a clip)\n", "dim"))
            for w in warnings:
                print(paint(f"  warning: {w}", "warn"))
            continue
        scanned += 1
        print(paint(f"{project}", "bcyan") + paint(f"   [track: {track}]", "dim"))
        # Runs are judged in order against the bests as they'd stand after the earlier runs, so
        # a later run must beat an earlier new record to count as one too. `work` is a scratch
        # copy: it is written to the bests file only with --save.
        ref = work.setdefault(track, {})
        for w in warnings:
            print(paint(f"  warning: {w}", "warn"))
        for k, run in enumerate(runs, 1):
            source = get_prop(run["clip"][0], "resource") or run["clip"][0].get("id")
            laps = lap_times(run["bounds"], fps)
            j = best_window(laps, args.laps)
            lap_list = "  ".join(fmt(d) for d in laps)
            if j is None:                        # too short to count toward a PB: keep it quiet
                print(paint(f"  Run {k}: {source}, {len(laps)} lap(s): {lap_list}", "dim")
                      + paint(f"   (too few laps for a {args.laps}-lap PB)", "dim"))
            else:
                print(paint(f"  Run {k}: {source}, {len(laps)} laps:", "bold") + f" {lap_list}")
                window = laps[j:j + args.laps]
                total = sum(window)
                a = analyze(window, total, ref, project.name)
                where = f"laps {j + 1}-{j + args.laps}"
                if a["matches_pb"]:
                    verdict = paint("matches your saved PB", "cyan")
                elif a["new_pb"]:
                    gap = ("" if a["off_pb"] is None
                           else f"  ({a['off_pb']:+.2f} vs PB {fmt(ref['best_total'])})")
                    verdict = paint(f"NEW PB!{gap}", "gold")
                    pbs.append((track, project.name, k, total))
                else:
                    verdict = paint(f"{a['off_pb']:+.2f} off your PB of {fmt(ref['best_total'])}", "red")
                print(f"    best {args.laps} in a row: {where} = " + paint(fmt(total), "bold")
                      + f"   {verdict}")
            fastest = min(laps)
            best_lap = ref.get("best_lap")
            lap_no = laps.index(fastest) + 1
            if best_lap is None or fastest < best_lap - 0.005:
                lap_records.append((track, project.name, k, fastest))
                was = "" if best_lap is None else f"  (was {fmt(best_lap)})"
                print(f"    fastest lap {fmt(fastest)} (lap {lap_no})   " + paint(f"NEW BEST LAP!{was}", "gold"))
            elif j is not None:
                print(paint(f"    fastest lap {fmt(fastest)} (lap {lap_no})   "
                            f"best lap on record is {fmt(best_lap)}", "dim"))
            apply_run_to_bests(ref, laps, laps[j:j + args.laps] if j is not None else None,
                               project.name)
        print()

    print(paint(f"Scanned {scanned} project(s).", "dim"))
    if pbs:
        best_by_track = {}
        for track, name, k, total in pbs:
            if track not in best_by_track or total < best_by_track[track][2]:
                best_by_track[track] = (name, k, total)
        for track, (name, k, total) in best_by_track.items():
            print(paint(f"Best new PB for {track}: {fmt(total)} in {name} (run {k})", "gold"))
    else:
        print("No new PBs.")
    if lap_records:                              # several runs may set records; show only the best
        best_lap_by_track = {}
        for track, name, k, secs in lap_records:
            if track not in best_lap_by_track or secs < best_lap_by_track[track][2]:
                best_lap_by_track[track] = (name, k, secs)
        for track, (name, k, secs) in best_lap_by_track.items():
            print(paint(f"Best new lap for {track}: {fmt(secs)} in {name} (run {k})", "gold"))
    if args.save:
        bests_path.write_text(json.dumps(work, indent=2) + "\n", "utf-8")
        print(paint(f"Saved bests to {bests_path.name}.", "bgreen"))
    elif (pbs or lap_records) and not os.environ.get("RACE_FACADE"):
        print(paint("Run again with --save to record these in the bests file.", "info"))


if __name__ == "__main__":
    main()
