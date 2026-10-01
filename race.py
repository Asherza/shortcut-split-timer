#!/usr/bin/env python3
"""race.py - one guided command for a race project.

Walks a Shotcut project through the whole process, showing a preview at each step and
asking before anything is written:

  1. Check   - reads your markers and tells you if it's a new PB (check_pb.py, read-only)
  2. Panel   - adds the LiveSplit-style timer panel and records new bests (split_timer.py)
  3. Trim    - cuts the timeline to just the best laps plus a buffer (trim_best.py)

    python race.py [project.mlt] [options]

With no project it lists your most recent ones to pick from. Before you run it: place plain
markers in Shotcut (on each clip the first marker is the start of lap 1, every later one ends
a lap), save, and close the project in Shotcut.

Options
  --preview           run every step's preview and stop; writes nothing, asks nothing
  --yes               do every step without asking (only skipping steps you turn off)
  --skip-panel        don't offer the timer panel        --skip-trim   don't offer the trim
  --laps N            laps that make up a PB (default 3)
  --track NAME        bests key (default: the project file name without .mlt)
  --title TEXT        panel title
  --bests FILE        bests file (default: bests.json next to these scripts)
  --pre SEC --post SEC   buffer kept around the best laps when trimming (default 5 and 5)
  --keep best|all     trim: keep only the single best run (default) or every run
  --extend            trim: let the buffer borrow footage past the clip's current edges

Every real write makes a backup first (.splittimer-backups / .trim-backups next to the
project), so any step can be undone by copying that file back.
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from split_timer import DEFAULT_LAPS, USE_COLOR, paint

HERE = Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def run(script, *args):
    """Run a sibling script with our terminal attached (so its colours show)."""
    env = dict(os.environ, RACE_FACADE="1")
    sys.stdout.flush()                           # keep our text in order with the child's output
    return subprocess.run([sys.executable, str(HERE / script), *map(str, args)], env=env).returncode


def apply(script, *args):
    """Run a script's --save step after its preview was shown: print only the Backup/Wrote
    lines instead of repeating the whole report."""
    env = dict(os.environ, RACE_FACADE="1")
    if USE_COLOR:
        env["FORCE_COLOR"] = "1"
    sys.stdout.flush()
    p = subprocess.run([sys.executable, str(HERE / script), *map(str, args), "--save"], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (p.stdout or "").splitlines():
        if ANSI.sub("", line).startswith(("Backup:", "Wrote", "Saved")):
            print(line)
    if p.returncode:
        print(p.stderr or p.stdout)
    return p.returncode


def step(n, total, title):
    print("\n" + paint(f"Step {n}/{total}  {title}", "bcyan"))
    print(paint("-" * 60, "dim"), flush=True)


def shotcut_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq shotcut.exe", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return "shotcut.exe" in out.lower()


def pick_project():
    found = sorted((p for p in HERE.rglob("*.mlt")
                    if not any(part.startswith(".") for part in p.relative_to(HERE).parts)),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    if not found:
        sys.exit(paint(f"No .mlt projects found under {HERE}. Pass a project path.", "bred"))
    print(paint("Recent projects:", "bold"))
    for i, p in enumerate(found, 1):
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%b %d %H:%M")
        print(f"  {paint(str(i), 'bcyan')}. {p.relative_to(HERE)}  {paint(when, 'dim')}")
    try:
        choice = input(paint("Which one? [1] ", "bold")).replace("﻿", "").strip() or "1"
        return found[int(choice) - 1]
    except (ValueError, IndexError, EOFError):
        sys.exit(paint("No project chosen.", "warn"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", type=Path)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--skip-panel", action="store_true")
    ap.add_argument("--skip-trim", action="store_true")
    ap.add_argument("--laps", type=int, default=DEFAULT_LAPS)
    ap.add_argument("--track")
    ap.add_argument("--title")
    ap.add_argument("--bests", type=Path)
    ap.add_argument("--pre", type=float, default=5.0)
    ap.add_argument("--post", type=float, default=5.0)
    ap.add_argument("--keep", choices=("best", "all"), default="best")
    ap.add_argument("--extend", action="store_true")
    args = ap.parse_args()

    def ask(question, default=False):
        if args.yes:
            return True
        try:
            answer = input(paint(question + (" [Y/n] " if default else " [y/N] "), "bold"))
            answer = answer.replace("﻿", "").strip().lower()   # PowerShell pipes add a BOM
        except EOFError:
            return False
        return default if not answer else answer in ("y", "yes")

    project = (args.project or pick_project()).resolve()
    if not project.is_file():
        sys.exit(paint(f"{project} not found.", "bred"))

    shared = ["--laps", args.laps]
    if args.track:
        shared += ["--track", args.track]
    if args.bests:
        shared += ["--bests", args.bests]
    panel_args = shared + (["--title", args.title] if args.title else [])
    trim_args = ["--laps", args.laps, "--pre", args.pre, "--post", args.post, "--keep", args.keep]
    if args.extend:
        trim_args.append("--extend")

    total = 1 + (0 if args.skip_panel else 1) + (0 if args.skip_trim else 1)
    print(paint(f"\nProject: {project}", "bold"))
    if not args.preview and shotcut_running():
        print(paint("Shotcut is running. If this project is open in it, close it first: saving "
                    "from Shotcut later would undo these changes.", "warn"))
        if not ask("Continue anyway?", default=True):
            sys.exit("Stopped. Nothing was changed.")

    done = []

    # ---- 1. check ---------------------------------------------------------------
    step(1, total, "Check for a new PB (read-only)")
    if run("check_pb.py", project, *shared):
        sys.exit(paint("The check failed; nothing was changed.", "bred"))

    n = 1
    # ---- 2. panel ---------------------------------------------------------------
    if not args.skip_panel:
        n += 1
        if args.yes and not args.preview:        # not asking, so no separate preview: show it once
            step(n, total, "Timer panel")
            if run("split_timer.py", project, *panel_args, "--save"):
                sys.exit(paint("Couldn't add the panel.", "bred"))
            done.append("Added the timer panel and updated your bests")
        else:
            step(n, total, "Timer panel (preview)")
            if run("split_timer.py", project, *panel_args):
                sys.exit(paint("Couldn't build the panel; nothing was changed.", "bred"))
            if not args.preview:
                print()
                if ask("Add this panel to the project and record any new bests?", default=True):
                    if apply("split_timer.py", project, *panel_args):
                        sys.exit(paint("Saving the panel failed.", "bred"))
                    done.append("Added the timer panel and updated your bests")
                elif ask("Record new bests without adding the panel?", default=False):
                    if apply("check_pb.py", project, *shared):
                        sys.exit(paint("Saving the bests failed.", "bred"))
                    done.append("Recorded new bests (no panel)")

    # ---- 3. trim ----------------------------------------------------------------
    if not args.skip_trim:
        n += 1
        if args.yes and not args.preview:
            step(n, total, "Trim the timeline to the best laps")
            if run("trim_best.py", project, *trim_args, "--save"):
                print(paint("Nothing to trim.", "info"))
            else:
                done.append("Trimmed the timeline to the best laps")
        else:
            step(n, total, "Trim the timeline to the best laps (preview)")
            if run("trim_best.py", project, *trim_args):
                print(paint("Nothing to trim, or the preview failed.", "info"))
            elif not args.preview:
                print()
                if ask("Trim the timeline to this? Everything else on it is removed.", default=False):
                    if apply("trim_best.py", project, *trim_args):
                        sys.exit(paint("Trimming failed.", "bred"))
                    done.append("Trimmed the timeline to the best laps")

    # ---- summary ----------------------------------------------------------------
    print("\n" + paint("Summary", "bcyan"))
    print(paint("-" * 60, "dim"))
    if args.preview:
        print(paint("Preview only: nothing was written. Run again without --preview to apply.", "info"))
    elif done:
        for line in done:
            print(paint("  + " + line, "bgreen"))
        print(paint("Reopen the project in Shotcut to see the changes, then export.", "bold"))
        print(paint(f"Backups of the previous versions are in {project.parent}\\.splittimer-backups "
                    "and .trim-backups", "dim"))
    else:
        print(paint("No changes were made.", "dim"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(paint("\nStopped. Anything already written keeps its backup.", "info"))
