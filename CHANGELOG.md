# Changelog

## 2026-07-14

### Added
- Repository path as CLI argument (`gitstats.py /path/to/repo`), works from any directory.
- `-b/--branch REF` — limit stats to commits reachable from a single ref (default: all refs).
- `--pathspec PATH...` — limit stats to commits touching given paths.
- Activity heatmap (weekday × hour, author local time).
- CSV export (`/api/export?period=...`, Export CSV button).
- Rescan button in UI + `POST /api/rescan` endpoint (full rescan without restart).
- Scan progress with total and percentage.
- UI localization by browser locale (Czech, fallback English).
- Automatic port fallback: if the port is busy, the next free one (up to +49) is used.

### Changed
- Cache moved from `.gitstats/` inside the repo to `~/.cache/gitstats/` (XDG);
  one DB per repo × branch × pathspec combination. Repos stay untouched.
- Authors are grouped by e-mail (case-insensitive) instead of by name;
  display name is the most frequent one, `.mailmap` is respected as before.
- Dates are stored normalized to UTC (consistent bucketing across timezones);
  the commit timezone offset is kept for the heatmap.
- Chart.js is served locally (vendored `chart.umd.js`, or downloaded once and
  cached in `~/.cache/gitstats/`); CDN is only a fallback. Works offline.
- Legend labels rendered via DOM API instead of innerHTML (author names are no
  longer interpreted as HTML).

### Performance
- Incremental scan reads diff stats only for new commits
  (`git log --no-walk --stdin`) instead of re-reading the whole history.
- `git log` output is streamed and inserted in batches — flat memory usage
  even on huge repositories.
- Aggregation moved from Python to SQL (`GROUP BY` in SQLite).

### Fixed
- Commits removed by rebase/force-push are pruned from the cache instead of
  inflating stats forever.
- Commits with an empty author e-mail were dropped during parsing (63 of
  154 606 on FFmpeg); empty e-mail no longer breaks field alignment and such
  authors are grouped by name.
- Busy port no longer crashes with a traceback.
