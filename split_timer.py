#!/usr/bin/env python3
"""Build a LiveSplit-style lap split panel in a Shotcut project from timeline markers.

Just drop plain markers on the timeline (names don't matter). On each clip, the first marker
is the START of lap 1, every marker after it is the END of a lap, and the last marker is the
finish: a clip with 5 markers is a 4-lap run. Each clip is one run, so one .mlt can cover
several .ts files (put each in its own clip). Your PB is the fastest N consecutive laps
(N = --laps, default 3): the panel is drawn over the best N-lap stretch of each run, so mark
every lap and the script finds it.

Each panel has a header, one row per lap and a big total timer:
  * before a lap starts its row shows that lap of your PB run as a dim target;
  * while a lap runs its row is highlighted and shows a live timer;
  * when it ends the time freezes and a delta vs your PB run's same lap appears
    (green = faster, red = slower); a best-lap-ever turns the time and delta gold;
  * the total timer and its delta turn gold on a new PB run, red when slower.
Bests (best lap ever, PB total, and the PB run's laps) live in a JSON file shared by all
projects and are updated automatically when beaten.

    python split_timer.py <project.mlt> [--laps N] [--track NAME] [--title TEXT]
                          [--bests FILE] [--out FILE] [--save]

By default it only PREVIEWS: nothing is written. Add --save to write the project (after a
backup) and update the bests file. With --out FILE --save it writes to FILE instead and leaves
your project and bests alone.

Close the project in Shotcut first; a later save from Shotcut overwrites this.
Re-running is safe: filters made by this script are tagged and rebuilt each time.

Performance note: any overlay makes Shotcut convert each frame to RGBA, which costs about
the same however many overlay filters there are. It only affects live preview speed, not
the exported file.
"""

import argparse
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

TAG = "shotcut:splittimer"
DEFAULT_LAPS = 3

# ---- Look & layout (project pixels, 1280x720) -------------------------------
PANEL_X, PANEL_Y, PANEL_W = 56, 130, 220
HEAD_H, ROW_H, FOOT_H = 26, 30, 50
PAD = 10                      # inner left/right padding
TIME_W = 78                   # width of the time column
NAME_W = 56                   # width of the lap-name column
BIG_W = 130                   # width of the big total timer
SIZE_TITLE, SIZE_BEST = 15, 11
SIZE_ROW, SIZE_DELTA, SIZE_PB = 16, 14, 18
FONT = "Segoe UI"

C_HEAD_BG = "#e00b0b0b"
C_ROW_BG = ("#d9181818", "#d9222222")
C_ROW_HI = "#e6294a7a"        # current lap (LiveSplit blue)
C_FOOT_BG = "#e6090909"
C_WHITE, C_DIM, C_TITLE = "#ffffffff", "#ff8a8a8a", "#ffd0d0d0"
C_GOLD, C_RED, C_GREEN = "#ffffc93c", "#ffff5a4d", "#ff4ade80"
C_CLEAR = "#00000000"


class MarkerError(Exception):
    """Something is wrong with a project's markers or clips (message is user-facing)."""


# ---- terminal colour (shared by check_pb.py and trim_best.py) -----------------------
def _color_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _enable_windows_ansi():
    """Turn on ANSI escape handling in the Windows console (a no-op elsewhere)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


USE_COLOR = _color_enabled()
if USE_COLOR:
    _enable_windows_ansi()

# Colour roles, chosen so no two meanings share a colour:
#   gold = records/PB (bold yellow)   green/red = faster/slower   bred = errors
#   warn = warnings (bright magenta)  info = hints and "preview only" (bright blue)
_ANSI = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
         "cyan": "36", "gold": "1;33", "bred": "1;31", "bgreen": "1;32", "bcyan": "1;36",
         "warn": "95", "info": "94"}


def paint(text, *styles):
    """Wrap `text` in ANSI styles, e.g. paint('NEW PB', 'gold'); plain text when colour is off."""
    codes = ";".join(_ANSI[s] for s in styles if s)
    return f"\033[{codes}m{text}\033[0m" if USE_COLOR and codes else str(text)


def style_for(colour):
    """The terminal style matching one of the panel's colours."""
    return {"#ffffc93c": "gold", "#ff4ade80": "green", "#ffff5a4d": "red", "#ff8a8a8a": "dim"}.get(colour, "")


# ---- small helpers ------------------------------------------------------------
def tc_to_seconds(tc):
    h, m, s = tc.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def tc_to_frames(value, fps):
    """A clip/blank time as frames. MLT files hold either a timecode or a plain frame count."""
    value = str(value).strip()
    return round(tc_to_seconds(value) * fps) if ":" in value else int(float(value))


def seconds_to_tc(sec):
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def prop(parent, name, value):
    el = ET.SubElement(parent, "property", name=name)
    el.text = str(value)
    return el


def get_prop(el, name):
    for p in el.findall("property"):
        if p.get("name") == name:
            return p.text
    return None


# ---- reading a project ------------------------------------------------------------
def project_fps(root):
    prof = root.find("profile")
    return int(prof.get("frame_rate_num")) / int(prof.get("frame_rate_den"))


def read_markers(root, fps):
    tractor = next((t for t in root.iter("tractor") if get_prop(t, "shotcut") == "1"), None)
    if tractor is None:
        raise MarkerError("not a Shotcut project (no main tractor)")
    holder = next((p for p in tractor.findall("properties")
                   if p.get("name") == "shotcut:markers"), None)
    markers = []
    for m in (holder if holder is not None else []):
        text = (get_prop(m, "text") or "").strip()
        markers.append((text, tc_to_frames(get_prop(m, "start"), fps)))
    return sorted(markers, key=lambda tf: tf[1])


def runs_from_markers(root, markers, fps):
    """Group markers by the clip they sit on. Each clip with 2+ markers is one run:
    first marker = start, every later marker ends a lap, the last one is the finish.
    Marker names are ignored. Returns (runs, warnings); a run is
    {'bounds': [start, lap1_end, ..., end], 'clip': (chain, offset, clip_in, clip_out)}."""
    groups, order, warnings = {}, [], []
    for text, frame in markers:
        clip = clip_at(root, frame, fps)
        if clip is None:
            warnings.append(f"marker at frame {frame} is not on a video clip and was ignored")
            continue
        key = (id(clip[0]), clip[1])
        if key not in groups:
            groups[key] = {"clip": clip, "bounds": []}
            order.append(key)
        groups[key]["bounds"].append(frame)
    runs = []
    for key in order:
        g = groups[key]
        name = get_prop(g["clip"][0], "resource") or g["clip"][0].get("id")
        start_s = (g["clip"][2] - g["clip"][1]) / fps       # where the clip begins on the timeline
        where = f"{int(start_s // 60)}:{start_s % 60:04.1f}"
        if len(g["bounds"]) < 2:
            warnings.append(f"{name} (the clip at {where} on the timeline) has only one marker; "
                            "a run needs at least a start and an end")
        elif len(set(g["bounds"])) != len(g["bounds"]):
            raise MarkerError(f"{name} has two markers on the same frame")
        else:
            runs.append(g)
    return runs, warnings


def clip_at(root, frame, fps):
    """Return (chain, timeline->source offset, clip source in, clip source out) or None."""
    tractor = next(t for t in root.iter("tractor") if get_prop(t, "shotcut") == "1")
    for track in tractor.findall("track"):
        pl = next((p for p in root.findall("playlist") if p.get("id") == track.get("producer")), None)
        if pl is None or get_prop(pl, "shotcut:video") != "1":
            continue
        pos = 0
        for item in pl:
            if item.tag == "blank":
                pos += tc_to_frames(item.get("length"), fps)
            elif item.tag == "entry":
                a = tc_to_frames(item.get("in"), fps)
                b = tc_to_frames(item.get("out"), fps)
                length = b - a + 1
                if pos <= frame < pos + length:
                    chain = next(c for c in list(root.iter("chain")) + list(root.iter("producer"))
                                 if c.get("id") == item.get("producer"))
                    return chain, a - pos, a, b
                pos += length
    return None


def lap_times(bounds, fps):
    return [(bounds[i + 1] - bounds[i]) / fps for i in range(len(bounds) - 1)]


def best_window(laps, size):
    """Index of the first lap of the fastest `size` consecutive laps (earliest wins ties).
    Returns None when the run is shorter than `size`."""
    if len(laps) < size:
        return None
    sums = [sum(laps[j:j + size]) for j in range(len(laps) - size + 1)]
    return sums.index(min(sums))


# ---- bests and verdicts ---------------------------------------------------------------
def lap_delta(seconds, reference):
    """(text, colour) for a lap against the same lap of your PB run (no gold here)."""
    if reference is None:
        return "", C_DIM
    d = seconds - reference
    if abs(d) < 0.005:
        return "0.00", C_DIM
    return f"{d:+.2f}", (C_GREEN if d < 0 else C_RED)


def total_verdict(total, best_total, batch_total=None):
    """(text, colour) for the whole run against your PB total. Gold means it is the new PB;
    a run that ties your PB (within 5 ms) is the PB itself, so it reads plain 'PB'.
    `batch_total` is the best total among all the runs being processed together: a run that
    beats your stored PB but not that one is green, since it won't be the PB."""
    is_best = batch_total is None or total <= batch_total + 0.005
    if best_total is None:
        return ("PB", C_GOLD) if is_best else ("", C_DIM)
    if abs(total - best_total) < 0.005:
        return "PB", C_GOLD
    d = total - best_total
    if d < 0:
        return f"{d:+.2f}", (C_GOLD if is_best else C_GREEN)
    return f"{d:+.2f}", C_RED


def analyze(laps, total, tb, here, best_lap_ever=None, batch_total=None):
    """Judge one stretch of laps against the stored bests `tb` (used by both scripts).

    best_lap_ever: the fastest lap of all, counting your stored bests AND every run being
        processed together. Only a lap matching it is marked as the best lap ever.
    batch_total: the best PB-sized total among the runs processed together (see total_verdict).
    """
    n = len(laps)
    best_lap, best_total = tb.get("best_lap"), tb.get("best_total")
    pb_laps = tb.get("pb_laps")                  # the laps of your personal-best run
    if pb_laps is not None and len(pb_laps) != n:
        pb_laps = None                           # different lap count: nothing to compare to

    # Best lap ever: a lap that matches the fastest of everything (within 5 ms), so a lap that
    # only beats your stored best but is slower than another lap in the batch is NOT marked.
    if best_lap_ever is not None:
        records = [d <= best_lap_ever + 0.005 for d in laps]
    else:                                        # standalone use: judge against the stored best
        records, running = [], best_lap
        for d in laps:
            rec = running is None or d <= running + 0.005
            records.append(rec)
            if rec:
                running = d if running is None else min(running, d)

    deltas = []
    for i, d in enumerate(laps):
        text, colour = lap_delta(d, pb_laps[i] if pb_laps else None)
        deltas.append((text, C_GOLD if records[i] else colour))

    # A total only means something against a PB made of the same number of laps: a 1-lap run
    # is not "28 s faster" than a 3-lap PB. When the counts differ there is no verdict at all.
    mismatch = tb.get("pb_laps") is not None and pb_laps is None
    comparable = not mismatch
    return {
        "pb_laps": pb_laps, "records": records, "deltas": deltas,
        "total": total_verdict(total, best_total, batch_total) if comparable else ("", C_DIM),
        "new_pb": comparable and (best_total is None or total < best_total - 0.005),
        "matches_pb": comparable and best_total is not None and abs(total - best_total) < 0.005,
        "off_pb": None if (best_total is None or not comparable) else total - best_total,
        "lap_count_mismatch": mismatch,
    }


def apply_run_to_bests(tb, all_laps, window, here):
    """Update `tb` in place. Best lap uses every lap of the run; the PB uses `window`,
    a list of the counted laps (or None when the run has too few laps to count)."""
    updates = []
    fastest = min(all_laps)
    if tb.get("best_lap") is None or fastest < tb["best_lap"] - 0.005:
        tb.update(best_lap=round(fastest, 3), best_lap_source=here)
        updates.append(f"new best lap {fastest:.3f}s")
    if window is not None:
        total = sum(window)
        if tb.get("best_total") is None or total < tb["best_total"] - 0.005:
            tb.update(best_total=round(total, 3), best_total_source=here,
                      pb_laps=[round(d, 3) for d in window])
            updates.append(f"new PB {total:.3f}s")
    return updates


def load_bests(path):
    # utf-8-sig also accepts a file saved with a byte-order mark (Notepad and PowerShell add one)
    return json.loads(path.read_text("utf-8-sig")) if path.exists() else {}


# ---- filter builders ----------------------------------------------------------
STYLE = {"family": FONT, "style": "normal", "olcolour": C_CLEAR, "outline": "0",
         "pad": "0", "opacity": "1"}


class Builder:
    def __init__(self, root, fps):
        self.fps = fps
        self.used = {f.get("id") for f in root.iter("filter")}
        self.n = 0

    def _new(self, in_f, out_f, props):
        while True:
            self.n += 1
            fid = f"filter_split{self.n}"
            if fid not in self.used:
                self.used.add(fid)
                break
        f = ET.Element("filter", id=fid, **{"in": seconds_to_tc(in_f / self.fps),
                                            "out": seconds_to_tc(out_f / self.fps)})
        for k, v in {**props, TAG: "1"}.items():
            prop(f, k, v)
        return f

    def rect(self, in_f, out_f, x, y, w, h, colour):
        """A filled rectangle (rich text with blank html, which fills its whole geometry)."""
        return self._new(in_f, out_f, {
            "argument": "plain text", "html": "<p>&nbsp;</p>",
            "geometry": f"{x} {y} {w} {h} 1", "bgcolour": colour,
            "mlt_service": "qtext", "shotcut:filter": "richText"})

    def text(self, in_f, out_f, x, y, w, h, text, *, fg, size, weight=400,
             halign="left", bg=C_CLEAR):
        return self._new(in_f, out_f, {
            "argument": text, "geometry": f"{x} {y} {w} {h} 1", "size": str(size),
            "weight": str(weight), "fgcolour": fg, "bgcolour": bg, "halign": halign,
            "valign": "middle", **STYLE,
            "mlt_service": "qtext", "shotcut:filter": "richText"})

    def timer(self, in_f, out_f, x, y, w, h, *, start_s, duration_s, offset_s=0.0,
              fg, weight=700):
        return self._new(in_f, out_f, {
            "format": "SS.SS", "start": seconds_to_tc(start_s),
            "duration": seconds_to_tc(duration_s), "offset": seconds_to_tc(offset_s),
            "speed": "1", "direction": "up", "geometry": f"{x} {y} {w} {h} 1",
            "size": "720", "weight": str(weight), "fgcolour": fg, "bgcolour": C_CLEAR,
            "halign": "right", "valign": "middle", **STYLE,
            "shotcut:usePointSize": "0", "shotcut:pointSize": "540",
            "mlt_service": "timer", "shotcut:filter": "timer"})


def run_filters(B, fps, bounds, offset, clip_in, clip_out, title, tb, here,
                best_lap_ever=None, batch_total=None):
    """Overlay filters for one stretch of laps (`bounds` = its start, lap ends, end),
    in draw order. Returns (filters, report lines, laps, total)."""
    laps = lap_times(bounds, fps)
    total = (bounds[-1] - bounds[0]) / fps
    n = len(laps)
    a = analyze(laps, total, tb, here, best_lap_ever, batch_total)
    src = [b + offset for b in bounds]           # marker frames in the clip's source frames

    x0, w = PANEL_X, PANEL_W
    row0 = PANEL_Y + HEAD_H
    foot_y = row0 + ROW_H * n
    tcol_x = x0 + w - PAD - TIME_W
    dcol_x = x0 + PAD + NAME_W
    dcol_w = tcol_x - dcol_x - 4

    bgs, highlights, texts, report = [], [], [], []

    # Header
    bgs.append(B.rect(clip_in, clip_out, x0, PANEL_Y, w, HEAD_H, C_HEAD_BG))
    texts.append(B.text(clip_in, clip_out, x0 + PAD, PANEL_Y, 120, HEAD_H, title, fg=C_TITLE,
                        size=SIZE_TITLE, weight=600))
    if tb.get("best_total") is not None:
        texts.append(B.text(clip_in, clip_out, x0 + w - PAD - 90, PANEL_Y, 90, HEAD_H,
                            f"PB {tb['best_total']:.2f}", fg=C_DIM, size=SIZE_BEST, weight=600,
                            halign="right"))

    # Lap rows
    for i, dur in enumerate(laps):
        y = row0 + ROW_H * i
        s, e = src[i], src[i + 1]
        bgs.append(B.rect(clip_in, clip_out, x0, y, w, ROW_H, C_ROW_BG[i % 2]))
        highlights.append(B.rect(s, e - 1 if i < n - 1 else e, x0, y, w, ROW_H, C_ROW_HI))
        texts.append(B.text(clip_in, clip_out, x0 + PAD, y, NAME_W, ROW_H, f"Lap {i + 1}", fg=C_WHITE,
                            size=SIZE_ROW))
        ref = a["pb_laps"][i] if a["pb_laps"] else None
        target = f"{ref:.2f}" if ref is not None else "-"       # dim comparison time before the lap
        texts.append(B.text(clip_in, s - 1, tcol_x, y, TIME_W, ROW_H, target, fg=C_DIM, size=SIZE_ROW,
                            weight=600, halign="right"))
        if a["records"][i]:                                     # best lap ever: gold time
            texts.append(B.timer(s, e - 1, tcol_x, y + 5, TIME_W, ROW_H - 10,
                                 start_s=0, duration_s=dur, fg=C_WHITE))
            texts.append(B.timer(e, clip_out, tcol_x, y + 5, TIME_W, ROW_H - 10,
                                 start_s=0, duration_s=0.001, offset_s=dur, fg=C_GOLD))
        else:
            texts.append(B.timer(s, clip_out, tcol_x, y + 5, TIME_W, ROW_H - 10,
                                 start_s=0, duration_s=dur, fg=C_WHITE))
        text, colour = a["deltas"][i]
        if text:
            texts.append(B.text(e, clip_out, dcol_x, y, dcol_w, ROW_H, text, fg=colour, size=SIZE_DELTA,
                                weight=600, halign="right"))
        report.append(f"      Lap {i + 1}: {dur:7.3f}s  " + paint(f"{text or '-':>6}", style_for(colour))
                      + (paint("  BEST LAP EVER", "gold") if a["records"][i] else ""))

    # Footer: big total timer, coloured by result once the stretch ends
    bgs.append(B.rect(clip_in, clip_out, x0, foot_y, w, FOOT_H, C_FOOT_BG))
    t_text, t_colour = a["total"]
    if not t_text:                                # nothing to compare to: plain white total
        t_colour = C_WHITE
    big_x = x0 + w - PAD - BIG_W
    texts.append(B.timer(clip_in, src[-1] - 1, big_x, foot_y + 7, BIG_W, FOOT_H - 14,
                         start_s=(src[0] - clip_in) / fps, duration_s=total, fg=C_WHITE))
    texts.append(B.timer(src[-1], clip_out, big_x, foot_y + 7, BIG_W, FOOT_H - 14,
                         start_s=0, duration_s=0.001, offset_s=total, fg=t_colour))
    if t_text:
        texts.append(B.text(src[-1], clip_out, x0 + PAD, foot_y, 60, FOOT_H, t_text, fg=t_colour,
                            size=SIZE_PB, weight=700))
    report.append(f"      Total : {total:7.3f}s  " + paint(t_text or "-", style_for(t_colour), "bold"))
    if a["lap_count_mismatch"]:
        report.append(paint("      (the stored PB has a different lap count, so it isn't compared)", "dim"))
    return bgs + highlights + texts, report, laps, total


# ---- main -----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path)
    ap.add_argument("--laps", type=int, default=DEFAULT_LAPS,
                    help=f"laps that make up a PB (default {DEFAULT_LAPS})")
    ap.add_argument("--track", help="key in the bests file (default: project file name without .mlt)")
    ap.add_argument("--title", help="panel title (default: the track key, upper-cased)")
    ap.add_argument("--bests", type=Path, help="bests JSON (default: bests.json next to this script)")
    ap.add_argument("--save", action="store_true",
                    help="write the changes (default is a preview that writes nothing)")
    ap.add_argument("--out", type=Path,
                    help="with --save, write to this file instead of editing the project "
                         "(the bests file is left alone)")
    ap.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)   # old spelling: now the default
    args = ap.parse_args()

    project = args.project.resolve()
    track = args.track or project.stem
    default_title = args.title or track.replace("_", " ").upper()
    bests_path = (args.bests or Path(__file__).with_name("bests.json")).resolve()

    try:
        tree = ET.parse(project)
        root = tree.getroot()
        fps = project_fps(root)
        runs, warnings = runs_from_markers(root, read_markers(root, fps), fps)
        if not runs:
            raise MarkerError("no runs found; put at least two markers (start and end) on a clip")
        placed = [(run, run["clip"]) for run in runs]
    except MarkerError as e:
        sys.exit(paint(f"{project.name}: {e}", "bred"))
    if not os.environ.get("RACE_FACADE"):        # race.py already showed these in its check step
        for w in warnings:
            print(paint(f"  Warning: {w}", "warn"))

    bests = load_bests(bests_path)
    tb = bests.get(track, {})
    stored = dict(tb)                            # every run is judged against the bests as stored
    here = project.name

    for chain in {id(c): c for _, (c, *_r) in placed}.values():
        for f in [f for f in chain.findall("filter") if get_prop(f, TAG) == "1"]:
            chain.remove(f)
    legacy = sorted({f.get("id") for _, (c, *_r) in placed for f in c.findall("filter")
                     if get_prop(f, "mlt_service") == "timer"})

    B = Builder(root, fps)
    print(paint(f"{project.name}", "bcyan") + paint(f"  track={track}  fps={fps:g}  runs={len(runs)}  "
                                                     f"PB = best {args.laps} laps", "dim"))
    print(paint(f"  Bests before: lap={stored.get('best_lap')}  total={stored.get('best_total')}  "
                f"pb_laps={stored.get('pb_laps')}", "dim"))
    updates = []
    # Records are judged across everything in this project, not run by run, so only the truly
    # fastest lap is marked "best lap ever" and only the fastest PB-sized stretch is the new PB.
    run_laps = [lap_times(r["bounds"], fps) for r, _ in placed]
    fastest_lap = min(min(ls) for ls in run_laps)
    best_lap_ever = (fastest_lap if stored.get("best_lap") is None
                     else min(fastest_lap, stored["best_lap"]))
    window_totals = []
    for ls in run_laps:
        jj = best_window(ls, args.laps)
        if jj is not None:
            window_totals.append(sum(ls[jj:jj + args.laps]))
    batch_total = min(window_totals) if window_totals else None
    for k, (run, (chain, off, clip_in, clip_out)) in enumerate(placed, 1):
        all_laps = lap_times(run["bounds"], fps)
        j = best_window(all_laps, args.laps)
        # A run shorter than a PB can't set a PB; it still gets a panel over all its laps.
        lo, hi = (j, j + args.laps) if j is not None else (0, len(all_laps))
        bounds = run["bounds"][lo:hi + 1]
        filters, report, laps, total = run_filters(B, fps, bounds, off, clip_in, clip_out,
                                                   default_title, stored, here,
                                                   best_lap_ever, batch_total)
        anchor = next((f for f in chain.findall("filter")
                       if (get_prop(f, "shotcut:filter") or "").startswith("fade")), None)
        idx = list(chain).index(anchor) if anchor is not None else len(list(chain))
        for i, f in enumerate(filters):
            chain.insert(idx + i, f)
        resource = get_prop(chain, "resource") or chain.get("id")
        note = (f"laps {lo + 1}-{hi} of {len(all_laps)}" if j is not None
                else f"only {len(all_laps)} lap(s), too few for a {args.laps}-lap PB")
        print(paint(f"  Run {k}: {resource}", "bold") + paint(f"  ({note})", "dim"))
        print("\n".join(report))
        apply_run_to_bests(tb, all_laps, laps if j is not None else None, here)
    bests[track] = tb
    # Report the net result, not every record along the way (a later run may beat an earlier one).
    updates = []
    if tb.get("best_lap") != stored.get("best_lap"):
        updates.append(f"new best lap {tb['best_lap']:.3f}s")
    if tb.get("best_total") != stored.get("best_total"):
        updates.append(f"new PB {tb['best_total']:.3f}s")
    print("  Bests update: " + (paint("; ".join(updates), "gold") if updates else paint("none", "dim")))
    if legacy:
        print(paint(f"  Note: other timer filter(s) on the clip ({', '.join(legacy)}) are not managed by "
                    "this script; remove them if they overlap the panel.", "warn"))

    if not args.save or args.dry_run:
        where = f"to {args.out.name}" if args.out else "the project and update the bests file"
        hint = "" if os.environ.get("RACE_FACADE") else paint(f"Add --save to write {where}.", "bold")
        print(paint("Preview only: nothing written. ", "info") + hint)
        return

    target = (args.out or project).resolve()
    if target == project:
        backup_dir = project.parent / ".splittimer-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{project.stem}.{datetime.now():%Y%m%dT%H%M%S}.mlt"
        shutil.copy2(project, backup)
        print(paint(f"Backup: {backup.name}", "dim"))
    ET.indent(tree, space="  ")
    tree.write(target, encoding="utf-8", xml_declaration=True)
    if target == project:
        bests_path.write_text(json.dumps(bests, indent=2) + "\n", "utf-8")
        print(paint(f"Wrote {project.name} and {bests_path.name}", "bgreen"))
    else:
        print(paint(f"Wrote {target} (bests file left untouched)", "bgreen"))


if __name__ == "__main__":
    main()
