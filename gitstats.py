#!/usr/bin/env python3
"""Git Stats - Interactive git repository statistics viewer.

Point this script at any git repository to visualize commit statistics
in a web browser. Data is cached in ~/.cache/gitstats/ for fast access.

Usage:
    python gitstats.py                  # Stats for repo in current directory
    python gitstats.py /path/to/repo    # Stats for another repo
    python gitstats.py -p 9000          # Use custom port
    python gitstats.py -b main          # Only commits reachable from main
    python gitstats.py --pathspec src/  # Only commits touching src/
    python gitstats.py --rescan         # Force full rescan
    python gitstats.py -n               # Don't auto-open browser
"""

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from urllib.parse import urlparse, parse_qs

# --- Constants ---

SCHEMA_VERSION = '2'
CHARTJS_URL = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.js'

COLORS = [
    '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
    '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac',
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]

# Filled in by main(): repo_root, db_path, branch, pathspec
CONFIG = {}

scan_status = {'scanning': False, 'total': 0, 'processed': 0, 'done': False}
scan_lock = threading.Lock()


def cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'gitstats')


# --- Database ---

def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row and row[0] != SCHEMA_VERSION:
        conn.execute('DROP TABLE IF EXISTS commits')
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute('''CREATE TABLE IF NOT EXISTS commits (
        hash TEXT PRIMARY KEY,
        author TEXT NOT NULL,
        email TEXT NOT NULL,
        date_utc TEXT NOT NULL,
        tz_offset_min INTEGER NOT NULL DEFAULT 0,
        additions INTEGER DEFAULT 0,
        deletions INTEGER DEFAULT 0
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON commits(date_utc)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_email ON commits(email)')
    conn.commit()
    conn.close()


# --- Git Parsing ---

def git_cmd(*args):
    return ['git', '-C', CONFIG['repo_root']] + list(args)


def parse_iso_date(s):
    """Parse ISO date with offset -> (utc naive 'YYYY-MM-DD HH:MM:SS', offset minutes)."""
    dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    offset = dt.utcoffset() or timedelta(0)
    utc = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return utc.strftime('%Y-%m-%d %H:%M:%S'), int(offset.total_seconds() // 60)


def rev_scope():
    """Rev-list scope: single ref or all refs."""
    return [CONFIG['branch']] if CONFIG.get('branch') else ['--all']


def pathspec_args():
    ps = CONFIG.get('pathspec')
    return ['--'] + ps if ps else []


def flush_block(lines, known, batch):
    """Parse one commit block (hash, name, email, date, numstat lines) into batch."""
    if len(lines) < 4:
        return
    commit_hash = lines[0].strip()
    if not commit_hash or commit_hash in known:
        return
    author = lines[1].strip()
    # Empty email would merge unrelated authors into one group
    email = lines[2].strip().lower() or author
    date_str = lines[3].strip()
    try:
        date_utc, tz_min = parse_iso_date(date_str)
    except ValueError:
        return

    additions = 0
    deletions = 0
    for line in lines[4:]:
        parts = line.split('\t')
        if len(parts) >= 2:
            try:
                additions += int(parts[0]) if parts[0] != '-' else 0
                deletions += int(parts[1]) if parts[1] != '-' else 0
            except ValueError:
                pass
    batch.append((commit_hash, author, email, date_utc, tz_min, additions, deletions))


def scan_repository(db_path, full=False):
    global scan_status
    with scan_lock:
        if scan_status['scanning']:
            return
        scan_status = {'scanning': True, 'total': 0, 'processed': 0, 'done': False}

    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')

    if full:
        conn.execute('DELETE FROM commits')
        conn.commit()

    known = set(r[0] for r in conn.execute('SELECT hash FROM commits'))
    if known:
        print(f"  Cache: {len(known)} commits, scanning for new...")

    # Fast pre-check: all reachable hashes, no diff stats
    try:
        rev_result = subprocess.run(
            git_cmd('rev-list', *rev_scope(), *pathspec_args()),
            capture_output=True, text=True, check=True, timeout=120
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Error running git rev-list: {e}", file=sys.stderr)
        scan_status = {'scanning': False, 'total': 0, 'processed': 0, 'done': True}
        conn.close()
        return

    all_hashes = set(rev_result.stdout.split())

    # Prune commits that disappeared (rebase, force-push, deleted branches)
    stale = known - all_hashes
    if stale:
        conn.executemany('DELETE FROM commits WHERE hash = ?', [(h,) for h in stale])
        conn.commit()
        known -= stale
        print(f"  Pruned {len(stale)} stale commits.")

    new_hashes = all_hashes - known
    if not new_hashes:
        scan_status = {'scanning': False, 'total': 0, 'processed': 0, 'done': True}
        print("  No new commits.")
        conn.close()
        return

    scan_status['total'] = len(new_hashes)
    print(f"  {len(new_hashes)} new commits to process...")

    # Fetch numstat only for the new commits, streamed to keep memory flat
    proc = subprocess.Popen(
        git_cmd('log', '--no-walk=unsorted', '--stdin',
                '--format=COMMIT_SEP%n%H%n%aN%n%aE%n%aI', '--numstat',
                *pathspec_args()),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )

    def feed():
        try:
            proc.stdin.write('\n'.join(new_hashes))
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()

    threading.Thread(target=feed, daemon=True).start()

    new_count = 0
    batch = []
    block = []

    def commit_batch():
        nonlocal batch
        conn.executemany('INSERT OR IGNORE INTO commits VALUES (?, ?, ?, ?, ?, ?, ?)', batch)
        conn.commit()
        batch = []

    for line in proc.stdout:
        line = line.rstrip('\n')
        if line == 'COMMIT_SEP':
            if block:
                before = len(batch)
                flush_block(block, known, batch)
                new_count += len(batch) - before
                scan_status['processed'] = new_count
                block = []
                if len(batch) >= 500:
                    commit_batch()
        else:
            # Keep empty lines: author email (%aE) can be empty and
            # positions in the block are fixed
            block.append(line)
    if block:
        before = len(batch)
        flush_block(block, known, batch)
        new_count += len(batch) - before

    proc.wait()
    commit_batch()
    if proc.returncode != 0:
        print(f"Error: git log exited with code {proc.returncode}", file=sys.stderr)

    conn.close()
    scan_status = {'scanning': False, 'total': new_count, 'processed': new_count, 'done': True}
    print(f"  Scan complete: {new_count} new commits.")


# --- Data Aggregation ---

def get_period_start(period):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    days = {'week': 7, 'month': 30, 'quarter': 90, 'year': 365}.get(period)
    return now - timedelta(days=days) if days else None


def bucket_format(period):
    if period in ('week', 'month', 'quarter'):
        return 'day'
    elif period == 'year':
        return 'week'
    return 'month'


BUCKET_SQL = {
    'day': "date(date_utc)",
    'week': "date(date_utc, '-6 days', 'weekday 1')",
    'month': "strftime('%Y-%m', date_utc)",
}


def generate_labels(start_date, end_date, fmt):
    labels = []
    s = start_date.date() if isinstance(start_date, datetime) else start_date
    e = end_date.date() if isinstance(end_date, datetime) else end_date

    if fmt == 'day':
        cur = s
        while cur <= e:
            labels.append(cur.isoformat())
            cur += timedelta(days=1)
    elif fmt == 'week':
        cur = s - timedelta(days=s.weekday())
        while cur <= e:
            labels.append(cur.isoformat())
            cur += timedelta(weeks=1)
    else:
        cur = s.replace(day=1)
        while cur <= e:
            labels.append(cur.strftime('%Y-%m'))
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)
    return labels


def display_names(conn, where, params):
    """Map email -> display name (most frequent author name for that email)."""
    rows = conn.execute(
        f'SELECT email, author, COUNT(*) FROM commits {where} GROUP BY email, author',
        params
    ).fetchall()
    best = {}
    for email, author, cnt in rows:
        if email not in best or cnt > best[email][1]:
            best[email] = (author, cnt)
    names = {}
    used = {}
    for email, (author, _) in sorted(best.items()):
        if author in used:
            names[email] = f'{author} ({email})'
            # retroactively disambiguate the first holder too
            first_email = used[author]
            names[first_email] = f'{author} ({first_email})'
        else:
            used[author] = email
            names[email] = author
    return names


def get_data(db_path, period='month'):
    conn = sqlite3.connect(db_path)
    start = get_period_start(period)
    fmt = bucket_format(period)
    bucket = BUCKET_SQL[fmt]

    where = ''
    params = ()
    if start:
        where = 'WHERE date_utc >= ?'
        params = (start.strftime('%Y-%m-%d %H:%M:%S'),)

    minmax = conn.execute(f'SELECT MIN(date_utc), MAX(date_utc) FROM commits {where}', params).fetchone()
    if not minmax or not minmax[0]:
        conn.close()
        return {'authors': [], 'timeseries': {'labels': [], 'data': {}},
                'totals': {}, 'heatmap': {}}

    min_date = datetime.strptime(minmax[0], '%Y-%m-%d %H:%M:%S')
    max_date = datetime.strptime(minmax[1], '%Y-%m-%d %H:%M:%S')
    if start and start > min_date:
        min_date = start
    labels = generate_labels(min_date, max_date, fmt)
    label_idx = {lbl: i for i, lbl in enumerate(labels)}

    names = display_names(conn, where, params)

    # Totals per author (grouped by email)
    totals = {}
    for email, c, a, d in conn.execute(
        f'SELECT email, COUNT(*), SUM(additions), SUM(deletions) FROM commits {where} GROUP BY email',
        params
    ):
        totals[names[email]] = {'commits': c, 'additions': a, 'deletions': d, 'changes': a + d}

    sorted_authors = sorted(totals.keys(), key=lambda n: totals[n]['commits'], reverse=True)

    # Timeseries: aggregated in SQL, gaps filled in Python
    ts_data = {
        n: {'commits': [0] * len(labels), 'additions': [0] * len(labels),
            'deletions': [0] * len(labels), 'changes': [0] * len(labels)}
        for n in sorted_authors
    }
    for email, b, c, a, d in conn.execute(
        f'SELECT email, {bucket} AS b, COUNT(*), SUM(additions), SUM(deletions) '
        f'FROM commits {where} GROUP BY email, b',
        params
    ):
        i = label_idx.get(b)
        if i is None:
            continue
        t = ts_data[names[email]]
        t['commits'][i] += c
        t['additions'][i] += a
        t['deletions'][i] += d
        t['changes'][i] += a + d

    # Heatmap (commits by author-local weekday x hour); %w: 0=Sun -> row 0=Mon
    heatmap = {n: [[0] * 24 for _ in range(7)] for n in sorted_authors}
    for email, dow, hour, c in conn.execute(
        f"SELECT email, CAST(strftime('%w', datetime(date_utc, tz_offset_min || ' minutes')) AS INTEGER), "
        f"CAST(strftime('%H', datetime(date_utc, tz_offset_min || ' minutes')) AS INTEGER), COUNT(*) "
        f'FROM commits {where} GROUP BY 1, 2, 3',
        params
    ):
        heatmap[names[email]][(dow + 6) % 7][hour] += c

    conn.close()

    authors = [{'name': n, 'color': COLORS[i % len(COLORS)]} for i, n in enumerate(sorted_authors)]
    return {
        'authors': authors,
        'timeseries': {'labels': labels, 'data': ts_data},
        'totals': totals,
        'heatmap': heatmap,
    }


def export_csv(db_path, period='month'):
    data = get_data(db_path, period)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['bucket', 'author', 'commits', 'additions', 'deletions', 'changes'])
    labels = data['timeseries']['labels']
    for author in data['authors']:
        series = data['timeseries']['data'][author['name']]
        for i, label in enumerate(labels):
            if series['commits'][i] or series['changes'][i]:
                w.writerow([label, author['name'], series['commits'][i],
                            series['additions'][i], series['deletions'][i], series['changes'][i]])
    return buf.getvalue().encode('utf-8')


# --- Chart.js (local vendored file / cached download / CDN fallback) ---

def get_chartjs():
    if 'chartjs' in CONFIG:
        return CONFIG['chartjs']
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, 'chart.umd.js'),
        os.path.join(cache_dir(), 'chart.umd.js'),
    ]
    for p in candidates:
        if os.path.exists(p):
            with open(p, 'rb') as f:
                CONFIG['chartjs'] = f.read()
                return CONFIG['chartjs']
    try:
        with urllib.request.urlopen(CHARTJS_URL, timeout=15) as resp:
            data = resp.read()
        os.makedirs(cache_dir(), exist_ok=True)
        with open(candidates[1], 'wb') as f:
            f.write(data)
        CONFIG['chartjs'] = data
        return data
    except OSError:
        CONFIG['chartjs'] = None
        return None


# --- HTTP Server ---

class StatsHandler(BaseHTTPRequestHandler):
    db_path = None
    repo_name = ''

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        period = params.get('period', ['month'])[0]
        if period not in ('week', 'month', 'quarter', 'year', 'all'):
            period = 'month'

        if path == '/':
            self._respond(200, 'text/html; charset=utf-8', HTML_PAGE.encode())
        elif path == '/chart.js':
            js = get_chartjs()
            if js:
                self._respond(200, 'application/javascript', js)
            else:
                self._respond(404, 'text/plain', b'Chart.js unavailable')
        elif path == '/api/status':
            self._json({**scan_status, 'repo': self.repo_name})
        elif path == '/api/data':
            self._json(get_data(self.db_path, period))
        elif path == '/api/export':
            body = export_csv(self.db_path, period)
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition',
                             f'attachment; filename="gitstats-{self.repo_name}-{period}.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._respond(404, 'text/plain', b'Not found')

    def do_POST(self):
        if urlparse(self.path).path == '/api/rescan':
            if scan_status['scanning']:
                self._json({'started': False, 'reason': 'already scanning'})
                return
            threading.Thread(
                target=scan_repository, args=(self.db_path,), kwargs={'full': True}, daemon=True
            ).start()
            self._json({'started': True})
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._respond(200, 'application/json', json.dumps(data).encode())


# --- HTML Page ---

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Git Stats</title>
<script src="/chart.js"></script>
<script>
if (typeof Chart === 'undefined') {
    document.write('<script src="https://cdn.jsdelivr.net/npm/chart.js@4"><\/script>');
}
</script>
<style>
:root {
    --bg: #f0f2f5;
    --text: #1a1a2e;
    --header-bg: #1a1a2e;
    --card-bg: white;
    --btn-bg: white;
    --btn-text: #555;
    --btn-hover: #f5f5f5;
    --btn-border: #eee;
    --grid-color: #f0f0f0;
    --shadow: rgba(0,0,0,0.1);
    --subtitle: #555;
    --total-color: #1a1a2e;
    --spinner-track: #eee;
    --pie-border: #fff;
}
[data-theme="dark"] {
    --bg: #121218;
    --text: #e0e0e8;
    --header-bg: #1a1a28;
    --card-bg: #1e1e2a;
    --btn-bg: #2a2a3a;
    --btn-text: #aab;
    --btn-hover: #33334a;
    --btn-border: #333348;
    --grid-color: #2a2a3a;
    --shadow: rgba(0,0,0,0.3);
    --subtitle: #99a;
    --total-color: #e0e0e8;
    --spinner-track: #333;
    --pie-border: #1e1e2a;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    transition: background 0.3s, color 0.3s;
}
header {
    background: var(--header-bg);
    color: white;
    padding: 1rem 2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
}
header h1 { font-size: 1.4rem; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 1rem; }
.repo-name { opacity: 0.7; font-size: 0.9rem; }
.header-btn {
    background: none;
    border: 1px solid rgba(255,255,255,0.25);
    color: white;
    cursor: pointer;
    font-size: 1.1rem;
    padding: 0.3rem 0.5rem;
    border-radius: 6px;
    transition: border-color 0.2s;
    line-height: 1;
}
.header-btn:hover { border-color: rgba(255,255,255,0.5); }
main { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }
.controls {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 1.5rem;
}
.btn-group {
    display: flex;
    background: var(--btn-bg);
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 3px var(--shadow);
}
.btn-group button, .btn-group a {
    padding: 0.5rem 1rem;
    border: none;
    background: var(--btn-bg);
    cursor: pointer;
    font-size: 0.85rem;
    color: var(--btn-text);
    transition: all 0.2s;
    border-right: 1px solid var(--btn-border);
    text-decoration: none;
}
.btn-group button:last-child, .btn-group a:last-child { border-right: none; }
.btn-group button:hover, .btn-group a:hover { background: var(--btn-hover); }
.btn-group button.active { background: #4e79a7; color: white; }
.card {
    background: var(--card-bg);
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px var(--shadow);
    margin-bottom: 1.5rem;
}
.card h3 { font-size: 0.95rem; color: var(--subtitle); margin-bottom: 1rem; }
.chart-container { position: relative; height: 400px; }
.legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 1rem;
}
.legend-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.7rem;
    border-radius: 20px;
    cursor: pointer;
    font-size: 0.85rem;
    border: 2px solid transparent;
    transition: all 0.2s;
    user-select: none;
}
.legend-item.active { border-color: currentColor; }
.legend-item.inactive { opacity: 0.3; }
.legend-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
}
.pie-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1.5rem;
    margin-bottom: 1.5rem;
}
.pie-card {
    background: var(--card-bg);
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px var(--shadow);
    text-align: center;
}
.pie-card h3 { font-size: 0.95rem; color: var(--subtitle); margin-bottom: 0.5rem; }
.pie-card .total { font-size: 1.4rem; font-weight: 700; color: var(--total-color); margin-bottom: 1rem; }
.pie-container { position: relative; max-width: 220px; margin: 0 auto; }
.heatmap {
    display: grid;
    grid-template-columns: 2.2rem repeat(24, 1fr);
    gap: 2px;
    font-size: 0.7rem;
    color: var(--subtitle);
}
.heatmap .hm-cell {
    aspect-ratio: 1;
    border-radius: 3px;
    background: var(--grid-color);
    min-width: 8px;
}
.heatmap .hm-label { display: flex; align-items: center; }
.heatmap .hm-hour { text-align: center; }
.loading {
    text-align: center;
    padding: 4rem;
    color: var(--subtitle);
}
.spinner {
    display: inline-block;
    width: 40px;
    height: 40px;
    border: 3px solid var(--spinner-track);
    border-top-color: #4e79a7;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.scan-info { margin-top: 1rem; font-size: 0.9rem; }
.empty-msg { text-align: center; padding: 3rem; color: var(--subtitle); }
</style>
</head>
<body>
<header>
    <h1>Git Stats</h1>
    <div class="header-right">
        <span class="repo-name" id="repoName"></span>
        <button class="header-btn" id="rescanBtn">&#8635;</button>
        <button class="header-btn" id="themeToggle">&#9790;</button>
    </div>
</header>
<main>
    <div id="loadingView" class="loading">
        <div class="spinner"></div>
        <div class="scan-info" id="scanInfo"></div>
    </div>
    <div id="mainView" style="display:none">
        <div class="controls">
            <div class="btn-group" id="periodBtns">
                <button data-val="week"></button>
                <button data-val="month" class="active"></button>
                <button data-val="quarter"></button>
                <button data-val="year"></button>
                <button data-val="all"></button>
            </div>
            <div class="btn-group" id="metricBtns">
                <button data-val="commits" class="active"></button>
                <button data-val="additions"></button>
                <button data-val="deletions"></button>
                <button data-val="changes"></button>
            </div>
            <div class="btn-group">
                <button id="trendBtn" class="active">Trend</button>
            </div>
            <div class="btn-group">
                <a id="exportLink" href="/api/export?period=month" download>Export CSV</a>
            </div>
        </div>
        <div id="emptyMsg" class="card empty-msg" style="display:none"></div>
        <div id="dataView">
            <div class="card">
                <div class="legend" id="legend"></div>
                <div class="chart-container">
                    <canvas id="timeChart"></canvas>
                </div>
            </div>
            <div class="pie-grid" id="pieGrid">
                <div class="pie-card">
                    <h3 data-metric="commits"></h3>
                    <div class="total" id="totalCommits">0</div>
                    <div class="pie-container"><canvas id="pieCommits"></canvas></div>
                </div>
                <div class="pie-card">
                    <h3 data-metric="additions"></h3>
                    <div class="total" id="totalAdditions">0</div>
                    <div class="pie-container"><canvas id="pieAdditions"></canvas></div>
                </div>
                <div class="pie-card">
                    <h3 data-metric="deletions"></h3>
                    <div class="total" id="totalDeletions">0</div>
                    <div class="pie-container"><canvas id="pieDeletions"></canvas></div>
                </div>
                <div class="pie-card">
                    <h3 data-metric="changes"></h3>
                    <div class="total" id="totalChanges">0</div>
                    <div class="pie-container"><canvas id="pieChanges"></canvas></div>
                </div>
            </div>
            <div class="card">
                <h3 id="heatmapTitle"></h3>
                <div class="heatmap" id="heatmap"></div>
            </div>
        </div>
    </div>
</main>
<script>
// --- i18n: pick language from browser locale, fall back to English ---
const LANGS = {
    en: {
        week: 'Week', month: 'Month', quarter: 'Quarter', year: 'Year', all: 'All',
        commits: 'Commits', additions: 'Added lines', deletions: 'Deleted lines',
        changes: 'Total changes',
        scanning: 'Scanning repository...', commitsWord: 'commits',
        empty: 'No commits in the selected period.',
        heatmapTitle: 'Activity by day and hour (commits, author local time)',
        trendTotal: 'Trend (total)',
        rescanTitle: 'Rescan repository', themeTitle: 'Toggle dark/light mode',
        days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        locale: 'en-US',
    },
    cs: {
        week: 'Týden', month: 'Měsíc', quarter: 'Kvartál', year: 'Rok', all: 'Vše',
        commits: 'Commity', additions: 'Přidané řádky', deletions: 'Smazané řádky',
        changes: 'Změny celkem',
        scanning: 'Skenování repozitáře...', commitsWord: 'commitů',
        empty: 'Žádné commity ve zvoleném období.',
        heatmapTitle: 'Aktivita podle dne a hodiny (commity, lokální čas autora)',
        trendTotal: 'Trend (celkem)',
        rescanTitle: 'Znovu naskenovat repozitář', themeTitle: 'Přepnout tmavý/světlý režim',
        days: ['Po', 'Út', 'St', 'Čt', 'Pá', 'So', 'Ne'],
        locale: 'cs-CZ',
    },
};
const LANG_CODE = (navigator.language || 'en').slice(0, 2).toLowerCase();
const L = LANGS[LANG_CODE] || LANGS.en;

function applyStrings() {
    document.documentElement.lang = LANGS[LANG_CODE] ? LANG_CODE : 'en';
    document.getElementById('scanInfo').textContent = L.scanning;
    document.getElementById('emptyMsg').textContent = L.empty;
    document.getElementById('heatmapTitle').textContent = L.heatmapTitle;
    document.getElementById('rescanBtn').title = L.rescanTitle;
    document.getElementById('themeToggle').title = L.themeTitle;
    document.querySelectorAll('#periodBtns button, #metricBtns button').forEach(btn => {
        btn.textContent = L[btn.dataset.val];
    });
    document.querySelectorAll('[data-metric]').forEach(el => {
        el.textContent = L[el.dataset.metric];
    });
}
applyStrings();

// Dark mode
(function() {
    const saved = localStorage.getItem('gitstats-theme');
    if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
        document.documentElement.setAttribute('data-theme', 'dark');
    }
})();

let currentPeriod = 'month';
let currentMetric = 'commits';
let showTrend = true;
let activeAuthors = new Set();
let chartData = null;
let timeChart = null;
let pieCharts = {};

async function init() {
    await waitForScan();
    document.getElementById('loadingView').style.display = 'none';
    document.getElementById('mainView').style.display = 'block';
    setupControls();
    await loadData();
}

async function waitForScan() {
    while (true) {
        const resp = await fetch('/api/status');
        const s = await resp.json();
        document.getElementById('repoName').textContent = s.repo;
        if (s.done) break;
        let msg = L.scanning;
        if (s.total > 0) {
            const pct = Math.round(100 * s.processed / s.total);
            msg += ' ' + s.processed + ' / ' + s.total + ' ' + L.commitsWord + ' (' + pct + ' %)';
        }
        document.getElementById('scanInfo').textContent = msg;
        await new Promise(r => setTimeout(r, 500));
    }
}

function isDark() {
    return document.documentElement.getAttribute('data-theme') === 'dark';
}

function getChartColors() {
    return {
        gridColor: isDark() ? '#2a2a3a' : '#f0f0f0',
        tickColor: isDark() ? '#99a' : '#666',
        pieBorder: isDark() ? '#1e1e2a' : '#fff',
    };
}

function setupControls() {
    document.getElementById('themeToggle').addEventListener('click', () => {
        const dark = !isDark();
        document.documentElement.setAttribute('data-theme', dark ? 'dark' : '');
        localStorage.setItem('gitstats-theme', dark ? 'dark' : 'light');
        document.getElementById('themeToggle').innerHTML = dark ? '&#9788;' : '&#9790;';
        renderAll();
    });
    document.getElementById('themeToggle').innerHTML = isDark() ? '&#9788;' : '&#9790;';
    document.getElementById('rescanBtn').addEventListener('click', async () => {
        await fetch('/api/rescan', {method: 'POST'});
        document.getElementById('mainView').style.display = 'none';
        document.getElementById('loadingView').style.display = 'block';
        await waitForScan();
        document.getElementById('loadingView').style.display = 'none';
        document.getElementById('mainView').style.display = 'block';
        await loadData();
    });
    document.querySelectorAll('#periodBtns button').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelector('#periodBtns .active').classList.remove('active');
            btn.classList.add('active');
            currentPeriod = btn.dataset.val;
            document.getElementById('exportLink').href = '/api/export?period=' + currentPeriod;
            loadData();
        });
    });
    document.querySelectorAll('#metricBtns button').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelector('#metricBtns .active').classList.remove('active');
            btn.classList.add('active');
            currentMetric = btn.dataset.val;
            renderAll();
        });
    });
    document.getElementById('trendBtn').addEventListener('click', () => {
        showTrend = !showTrend;
        document.getElementById('trendBtn').classList.toggle('active', showTrend);
        renderAll();
    });
}

function movingAverage(data, window) {
    const result = [];
    for (let i = 0; i < data.length; i++) {
        const start = Math.max(0, i - Math.floor(window / 2));
        const end = Math.min(data.length, i + Math.ceil(window / 2));
        let sum = 0;
        for (let j = start; j < end; j++) sum += data[j];
        result.push(sum / (end - start));
    }
    return result;
}

function trendWindowSize(points) {
    if (points <= 14) return 3;
    if (points <= 40) return 5;
    if (points <= 100) return 7;
    return Math.min(21, Math.round(points / 10));
}

async function loadData() {
    const resp = await fetch('/api/data?period=' + currentPeriod);
    chartData = await resp.json();
    activeAuthors = new Set(chartData.authors.map(a => a.name));
    const empty = !chartData.authors.length;
    document.getElementById('emptyMsg').style.display = empty ? 'block' : 'none';
    document.getElementById('dataView').style.display = empty ? 'none' : 'block';
    buildLegend();
    renderAll();
}

function buildLegend() {
    const el = document.getElementById('legend');
    el.innerHTML = '';
    chartData.authors.forEach(author => {
        const item = document.createElement('div');
        item.className = 'legend-item active';
        item.style.color = author.color;
        item.dataset.name = author.name;
        const dot = document.createElement('span');
        dot.className = 'legend-dot';
        dot.style.background = author.color;
        item.appendChild(dot);
        item.appendChild(document.createTextNode(author.name));
        item.addEventListener('click', (e) => {
            if (e.ctrlKey || e.metaKey) {
                // Solo mode: select only this author (or restore all if already solo)
                const soloActive = activeAuthors.size === 1 && activeAuthors.has(author.name);
                if (soloActive) {
                    activeAuthors = new Set(chartData.authors.map(a => a.name));
                } else {
                    activeAuthors = new Set([author.name]);
                }
                updateLegendStyles();
            } else {
                if (activeAuthors.has(author.name)) {
                    activeAuthors.delete(author.name);
                } else {
                    activeAuthors.add(author.name);
                }
                item.classList.toggle('active', activeAuthors.has(author.name));
                item.classList.toggle('inactive', !activeAuthors.has(author.name));
            }
            renderAll();
        });
        el.appendChild(item);
    });
}

function updateLegendStyles() {
    document.querySelectorAll('.legend-item').forEach(item => {
        const name = item.dataset.name;
        item.classList.toggle('active', activeAuthors.has(name));
        item.classList.toggle('inactive', !activeAuthors.has(name));
    });
}

function renderAll() {
    if (!chartData || !chartData.authors.length) return;
    renderTimeChart();
    renderPieCharts();
    renderHeatmap();
}

function renderTimeChart() {
    const ctx = document.getElementById('timeChart');
    if (timeChart) timeChart.destroy();

    const labels = chartData.timeseries.labels;
    const many = labels.length > 60;
    const datasets = chartData.authors
        .filter(a => activeAuthors.has(a.name))
        .map(a => ({
            label: a.name,
            data: chartData.timeseries.data[a.name][currentMetric],
            borderColor: a.color,
            backgroundColor: a.color + '18',
            borderWidth: 2,
            pointRadius: many ? 0 : 3,
            pointHoverRadius: 5,
            tension: 0.3,
            fill: false,
        }));

    if (showTrend && datasets.length > 0) {
        // Sum across active authors
        const totals = new Array(labels.length).fill(0);
        datasets.forEach(ds => ds.data.forEach((v, i) => totals[i] += v));
        const win = trendWindowSize(labels.length);
        const trendData = movingAverage(totals, win);
        const trendColor = isDark() ? 'rgba(255,255,255,0.5)' : 'rgba(0,0,0,0.4)';
        datasets.push({
            label: L.trendTotal,
            data: trendData,
            borderColor: trendColor,
            backgroundColor: 'transparent',
            borderWidth: 3,
            borderDash: [8, 4],
            pointRadius: 0,
            pointHoverRadius: 0,
            tension: 0.4,
            fill: false,
        });
    }

    const cc = getChartColors();
    timeChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        label: item => item.dataset.label + ': ' +
                            item.parsed.y.toLocaleString(L.locale)
                    }
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 30, color: cc.tickColor }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: cc.gridColor },
                    ticks: {
                        color: cc.tickColor,
                        callback: v => v.toLocaleString(L.locale)
                    }
                }
            }
        }
    });
}

function renderPieCharts() {
    const metrics = ['commits', 'additions', 'deletions', 'changes'];
    const ids = ['pieCommits', 'pieAdditions', 'pieDeletions', 'pieChanges'];
    const totalIds = ['totalCommits', 'totalAdditions', 'totalDeletions', 'totalChanges'];

    metrics.forEach((metric, i) => {
        if (pieCharts[metric]) pieCharts[metric].destroy();

        const active = chartData.authors.filter(a => activeAuthors.has(a.name));
        const labels = active.map(a => a.name);
        const colors = active.map(a => a.color);
        const values = active.map(a => {
            const t = chartData.totals[a.name];
            return t ? t[metric] : 0;
        });
        const total = values.reduce((s, v) => s + v, 0);
        document.getElementById(totalIds[i]).textContent = total.toLocaleString(L.locale);

        pieCharts[metric] = new Chart(document.getElementById(ids[i]), {
            type: 'doughnut',
            data: {
                labels,
                datasets: [{
                    data: values,
                    backgroundColor: colors,
                    borderWidth: 2,
                    borderColor: getChartColors().pieBorder
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => {
                                const t = ctx.dataset.data.reduce((s, v) => s + v, 0);
                                const pct = t ? ((ctx.parsed / t) * 100).toFixed(1) : 0;
                                return ctx.label + ': ' +
                                    ctx.parsed.toLocaleString(L.locale) + ' (' + pct + '%)';
                            }
                        }
                    }
                }
            }
        });
    });
}

function renderHeatmap() {
    const el = document.getElementById('heatmap');
    el.innerHTML = '';
    const days = L.days;

    // Sum heatmaps of active authors
    const grid = Array.from({length: 7}, () => new Array(24).fill(0));
    chartData.authors.filter(a => activeAuthors.has(a.name)).forEach(a => {
        const hm = chartData.heatmap[a.name];
        if (!hm) return;
        for (let d = 0; d < 7; d++)
            for (let h = 0; h < 24; h++)
                grid[d][h] += hm[d][h];
    });
    const max = Math.max(1, ...grid.flat());

    // Header row: hour labels every 3 hours
    el.appendChild(document.createElement('div'));
    for (let h = 0; h < 24; h++) {
        const c = document.createElement('div');
        c.className = 'hm-hour';
        c.textContent = (h % 3 === 0) ? h : '';
        el.appendChild(c);
    }
    for (let d = 0; d < 7; d++) {
        const lbl = document.createElement('div');
        lbl.className = 'hm-label';
        lbl.textContent = days[d];
        el.appendChild(lbl);
        for (let h = 0; h < 24; h++) {
            const cell = document.createElement('div');
            cell.className = 'hm-cell';
            const v = grid[d][h];
            if (v > 0) {
                const alpha = 0.15 + 0.85 * (v / max);
                cell.style.background = 'rgba(78, 121, 167, ' + alpha.toFixed(2) + ')';
            }
            cell.title = days[d] + ' ' + h + ':00 — ' + v.toLocaleString(L.locale) + ' ' + L.commitsWord;
            el.appendChild(cell);
        }
    }
}

init();
</script>
</body>
</html>
"""


# --- Main ---

def resolve_repo_root(path):
    try:
        result = subprocess.run(
            ['git', '-C', path, 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except FileNotFoundError:
        print("Error: git not found in PATH.", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError:
        print(f"Error: '{path}' is not a git repository.", file=sys.stderr)
        sys.exit(1)


def db_path_for(repo_root, branch, pathspec):
    key = '\0'.join([repo_root, branch or '--all'] + (pathspec or []))
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    name = os.path.basename(repo_root) or 'repo'
    return os.path.join(cache_dir(), f'{name}-{digest}.db')


def create_server(port):
    """Bind to requested port, or next free one within +49."""
    ThreadingTCPServer.allow_reuse_address = True
    for candidate in range(port, port + 50):
        try:
            return ThreadingTCPServer(('127.0.0.1', candidate), StatsHandler), candidate
        except OSError:
            continue
    print(f"Error: no free port in range {port}-{port + 49}.", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Git Stats - Repository statistics viewer')
    parser.add_argument('path', nargs='?', default='.',
                        help='Path to git repository (default: current directory)')
    parser.add_argument('-p', '--port', type=int, default=8787, help='Port (default: 8787)')
    parser.add_argument('-n', '--no-browser', action='store_true', help="Don't open browser")
    parser.add_argument('-b', '--branch', default=None,
                        help='Scan only commits reachable from this ref (default: all refs)')
    parser.add_argument('--pathspec', nargs='+', default=None, metavar='PATH',
                        help='Limit stats to commits touching these paths (git pathspec)')
    parser.add_argument('--rescan', action='store_true', help='Force full rescan (clear cache)')
    args = parser.parse_args()

    repo_root = resolve_repo_root(args.path)
    repo_name = os.path.basename(repo_root)
    db_path = db_path_for(repo_root, args.branch, args.pathspec)

    CONFIG['repo_root'] = repo_root
    CONFIG['branch'] = args.branch
    CONFIG['pathspec'] = args.pathspec

    if args.rescan:
        for suffix in ('', '-wal', '-shm'):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)
        print("Cache cleared.")

    print(f"Git Stats - {repo_name}")
    print(f"  Repo:  {repo_root}")
    if args.branch:
        print(f"  Ref:   {args.branch}")
    if args.pathspec:
        print(f"  Paths: {' '.join(args.pathspec)}")
    print(f"  Cache: {db_path}")
    init_db(db_path)

    StatsHandler.db_path = db_path
    StatsHandler.repo_name = repo_name

    # Start scan in background
    threading.Thread(target=scan_repository, args=(db_path,), daemon=True).start()

    server, port = create_server(args.port)
    if port != args.port:
        print(f"  Port {args.port} busy, using {port}.")
    url = f'http://127.0.0.1:{port}'
    print(f"Server: {url}")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == '__main__':
    main()
