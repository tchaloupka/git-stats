# Git Stats

Interactive web-based git repository statistics viewer. Single Python script with no external dependencies (stdlib only; Chart.js is bundled).

## Usage

```bash
python3 gitstats.py [path] [options]
```

```bash
python3 gitstats.py                    # repo in current directory
python3 gitstats.py ~/src/myrepo       # repo given by path
python3 gitstats.py -b main            # only commits reachable from main
python3 gitstats.py --pathspec src/    # only commits touching src/
```

A browser opens automatically at `http://127.0.0.1:8787` (or the next free port).

## Options

| Flag | Description |
|------|-------------|
| `path` | Path to a git repository (or any directory inside one). Default: current directory |
| `-p PORT` | Preferred port (default: 8787). If busy, the next free port up to +49 is used |
| `-n` | Don't auto-open browser |
| `-b REF`, `--branch REF` | Count only commits reachable from `REF` (default: all refs, `--all`) |
| `--pathspec PATH [PATH...]` | Count only commits touching the given paths (git pathspec) |
| `--rescan` | Clear cache and rescan everything |

## Features

- **Time series chart** (line chart) — per-author activity over time with automatic bucketing:
  - week/month/quarter — daily buckets
  - year — weekly buckets
  - all — monthly buckets
- **Trend line** — moving average of the sum of active authors (dashed line), adaptive window size, toggleable
- **4 pie charts** — totals for commits, added/deleted/changed lines for the selected period
- **Activity heatmap** — commits by weekday × hour of day, in the author's local time
- **Period filter**: week, month, quarter, year, all
- **Metric switching**: commits, added lines, deleted lines, total changes
- **Clickable author legend** — click to toggle visibility, Ctrl+click to solo an author (Ctrl+click again to restore all)
- **CSV export** — per-bucket, per-author rows for the selected period (Export CSV button or `GET /api/export?period=...`)
- **Rescan button** (↻ in header) — full rescan without restarting the server
- **Author grouping** — authors are merged by e-mail (case-insensitive), `.mailmap` is respected; display name is the most frequent one, name collisions are disambiguated with the e-mail
- **Dark mode** — toggle in header, respects system preference, persists choice
- **Localization** — UI language follows the browser locale (Czech available, anything else falls back to English)
- **Incremental scanning** — fast pre-check via `git rev-list`; diff stats are read only for new commits, streamed with flat memory usage. Commits that disappear (rebase, force-push) are pruned from the cache automatically
- **Background scan** — the server is available immediately, progress (count + %) is shown while scanning

## Working files

Everything lives outside the repository being analyzed:

- **Cache**: `~/.cache/gitstats/<repo>-<hash>.db` (respects `$XDG_CACHE_HOME`).
  One SQLite database per repo × branch × pathspec combination. `--rescan`
  deletes it; deleting the directory is always safe.
- **Chart.js**: served from `chart.umd.js` next to the script if present
  (bundled in this repo). Otherwise it is downloaded once from the CDN and
  cached in `~/.cache/gitstats/chart.umd.js` — after that the tool works fully
  offline. The CDN is used directly only as a last resort.

## Notes

- Merge commits are counted with 0 added/deleted lines (`git log --numstat`
  does not produce diff stats for merges); they do count as commits.
- Time series buckets use UTC; the heatmap uses each author's local time.
- The server binds to `127.0.0.1` only.

## Requirements

- Python 3.7+
- Git
