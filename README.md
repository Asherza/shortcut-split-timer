# race.py

Turns markers on a Shotcut (`.mlt`) project into a LiveSplit-style lap timer, checks if you set
a new PB, and trims the timeline down to your best laps — one guided command.

## Requirements

- **[Shotcut](https://shotcut.org/)** — a free, open-source video editor. These scripts read
  and write its `.mlt` project files directly; Shotcut itself doesn't need to be running.
- **[Python](https://www.python.org/downloads/) 3.9 or later** — standard library only, no
  packages to install.

## Setup

1. Place plain markers on your clip in Shotcut: the first marker is the start of lap 1, and
   every marker after it ends a lap. Marker names don't matter.
2. Save the project and close it in Shotcut.

## Run it

```bash
python race.py your_project.mlt
```

Leave the project out and it lists your most recent ones to pick from:

```bash
python race.py
```

It walks through three steps, showing a preview and asking before it writes anything:

1. **Check** — tells you if your run is a new PB (read-only)
2. **Panel** — adds the timer overlay and records any new bests
3. **Trim** — cuts the timeline down to your best laps plus a short buffer

Answer `y` to a prompt to apply that step, or just press Enter to use the default shown.
Every write makes a backup first, so you can always undo a step by copying that file back.

## Useful options

| Option | What it does |
|---|---|
| `--preview` | Show all three previews and stop — nothing is written, nothing is asked |
| `--yes` | Do every step without asking |
| `--skip-panel` | Don't add the timer panel |
| `--skip-trim` | Don't trim the timeline |
| `--laps N` | Laps that make up a PB (default 3) |

Run `python race.py --help` for the full list.
