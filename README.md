# Git Stats

Interactive web-based git repository statistics viewer. Single Python script with no external dependencies (stdlib only + Chart.js from CDN).

## Usage

```bash
cd /path/to/git/repo
python3 /path/to/gitstats.py
```

A browser will automatically open at `http://127.0.0.1:8787`.

## Features

- **Time series chart** (line chart) - per-author activity over time with automatic bucketing:
  - week/month - daily buckets
  - quarter/year - weekly buckets
  - all - monthly buckets
- **Trend line** - moving average of the sum of active authors (dashed line), adaptive window size, toggleable
- **4 pie charts** below the main chart - totals for commits, added/deleted/changed lines for the selected period
- **Period filter**: week, month, quarter, year, all
- **Metric switching**: commits, added lines, deleted lines, total changes
- **Clickable author legend** - click to toggle visibility, Ctrl+click to solo an author (Ctrl+click again to restore all)
- **Dark mode** - toggle in header, respects system preference, persists choice
- **SQLite cache** in `.gitstats/cache.db` - fast pre-check via `git rev-list`, near-instant startup when no new commits
- **Background scan** - server is available immediately, data loads in the background

## Options

| Flag | Description |
|------|-------------|
| `-p PORT` | Custom port (default: 8787) |
| `-n` | Don't auto-open browser |
| `--rescan` | Clear cache and rescan everything |

## Requirements

- Python 3.7+
- Git
- Browser with internet access (Chart.js is loaded from CDN)
