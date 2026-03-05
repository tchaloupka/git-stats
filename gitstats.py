#!/usr/bin/env python3
"""Git Stats - Interactive git repository statistics viewer.

Run this script from any git repository to visualize commit statistics
in a web browser. Data is cached in .gitstats/cache.db for fast access.

Usage:
    python gitstats.py              # Start server and open browser
    python gitstats.py -p 9000      # Use custom port
    python gitstats.py --rescan     # Force full rescan
    python gitstats.py -n           # Don't auto-open browser
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
from urllib.parse import urlparse, parse_qs

# --- Constants ---

DB_DIR = '.gitstats'
DB_FILE = os.path.join(DB_DIR, 'cache.db')

COLORS = [
    '#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
    '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac',
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf',
]

scan_status = {'scanning': False, 'total': 0, 'done': False}


# --- Database ---

def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS commits (
        hash TEXT PRIMARY KEY,
        author TEXT NOT NULL,
        date TEXT NOT NULL,
        additions INTEGER DEFAULT 0,
        deletions INTEGER DEFAULT 0
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_date ON commits(date)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_author ON commits(author)')
    conn.commit()
    conn.close()


# --- Git Parsing ---

def get_repo_name():
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True, text=True, check=True
        )
        return os.path.basename(result.stdout.strip())
    except subprocess.CalledProcessError:
        return 'Unknown'


def scan_repository(db_path):
    global scan_status
    scan_status = {'scanning': True, 'total': 0, 'done': False}

    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')

    # Get known hashes
    known = set(r[0] for r in conn.execute('SELECT hash FROM commits'))
    if known:
        print(f"  Cache: {len(known)} commits, scanning for new...")

    # Quick check: get all commit hashes (fast, no diff stats)
    try:
        rev_result = subprocess.run(
            ['git', 'rev-list', '--all'],
            capture_output=True, text=True, check=True, timeout=60
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Error running git rev-list: {e}", file=sys.stderr)
        scan_status = {'scanning': False, 'total': 0, 'done': True}
        conn.close()
        return

    all_hashes = set(rev_result.stdout.strip().split('\n')) if rev_result.stdout.strip() else set()
    new_hashes = all_hashes - known

    if not new_hashes:
        scan_status = {'scanning': False, 'total': 0, 'done': True}
        print(f"  No new commits.")
        conn.close()
        return

    print(f"  {len(new_hashes)} new commits to process...")

    # Full log with numstat only when there are new commits
    try:
        result = subprocess.run(
            ['git', 'log', '--all', '--format=COMMIT_SEP%n%H%n%aN%n%aI', '--numstat'],
            capture_output=True, text=True, check=True, timeout=300
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Error running git log: {e}", file=sys.stderr)
        scan_status = {'scanning': False, 'total': 0, 'done': True}
        conn.close()
        return

    blocks = result.stdout.split('COMMIT_SEP\n')
    new_count = 0
    batch = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split('\n')
        if len(lines) < 3:
            continue

        commit_hash = lines[0].strip()
        if commit_hash in known:
            continue

        author = lines[1].strip()
        date_str = lines[2].strip()

        additions = 0
        deletions = 0
        for line in lines[3:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split('\t')
            if len(parts) >= 2:
                try:
                    additions += int(parts[0]) if parts[0] != '-' else 0
                    deletions += int(parts[1]) if parts[1] != '-' else 0
                except ValueError:
                    pass

        batch.append((commit_hash, author, date_str, additions, deletions))
        new_count += 1
        scan_status['total'] = new_count

        if len(batch) >= 500:
            conn.executemany(
                'INSERT OR IGNORE INTO commits VALUES (?, ?, ?, ?, ?)', batch
            )
            conn.commit()
            batch = []

    if batch:
        conn.executemany(
            'INSERT OR IGNORE INTO commits VALUES (?, ?, ?, ?, ?)', batch
        )
        conn.commit()

    conn.close()
    scan_status = {'scanning': False, 'total': new_count, 'done': True}
    print(f"  Scan complete: {new_count} new commits.")


# --- Data Aggregation ---

def parse_date(s):
    """Parse ISO date string, return naive (local) datetime."""
    s = s.replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.fromisoformat(s[:19])
    # Strip timezone to keep everything as naive datetimes
    return dt.replace(tzinfo=None)


def get_period_start(period):
    now = datetime.now()
    if period == 'week':
        return now - timedelta(days=7)
    elif period == 'month':
        return now - timedelta(days=30)
    elif period == 'quarter':
        return now - timedelta(days=90)
    elif period == 'year':
        return now - timedelta(days=365)
    return None


def bucket_format(period):
    if period in ('week', 'month'):
        return 'day'
    elif period == 'quarter':
        return 'day'
    elif period == 'year':
        return 'week'
    return 'month'


def date_to_bucket(dt, fmt):
    if fmt == 'day':
        return dt.strftime('%Y-%m-%d')
    elif fmt == 'week':
        d = dt.date() if isinstance(dt, datetime) else dt
        d -= timedelta(days=d.weekday())
        return d.isoformat()
    else:
        return dt.strftime('%Y-%m')


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


def get_data(db_path, period='month'):
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    start = get_period_start(period)
    fmt = bucket_format(period)

    if start:
        rows = conn.execute(
            'SELECT author, date, additions, deletions FROM commits WHERE date >= ? ORDER BY date',
            (start.isoformat(),)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT author, date, additions, deletions FROM commits ORDER BY date'
        ).fetchall()
    conn.close()

    if not rows:
        return {'authors': [], 'timeseries': {'labels': [], 'data': {}}, 'totals': {}}

    # Parse dates and find range
    parsed = []
    dates = []
    for author, date_str, adds, dels in rows:
        dt = parse_date(date_str)
        parsed.append((author, dt, adds, dels))
        dates.append(dt)

    min_date = min(dates)
    max_date = max(dates)
    if start and start > min_date:
        min_date = start

    labels = generate_labels(min_date, max_date, fmt)
    label_set = set(labels)

    # Aggregate
    agg = {}
    totals = {}

    for author, dt, adds, dels in parsed:
        bucket = date_to_bucket(dt, fmt)
        if bucket not in label_set:
            continue

        if author not in agg:
            agg[author] = {}
        b = agg[author].setdefault(bucket, {'c': 0, 'a': 0, 'd': 0})
        b['c'] += 1
        b['a'] += adds
        b['d'] += dels

        t = totals.setdefault(author, {'commits': 0, 'additions': 0, 'deletions': 0, 'changes': 0})
        t['commits'] += 1
        t['additions'] += adds
        t['deletions'] += dels
        t['changes'] += adds + dels

    # Sort authors by total commits
    sorted_authors = sorted(totals.keys(), key=lambda a: totals[a]['commits'], reverse=True)

    # Build timeseries
    ts_data = {}
    for author in sorted_authors:
        commits, additions, deletions, changes = [], [], [], []
        for label in labels:
            d = agg.get(author, {}).get(label, {'c': 0, 'a': 0, 'd': 0})
            commits.append(d['c'])
            additions.append(d['a'])
            deletions.append(d['d'])
            changes.append(d['a'] + d['d'])
        ts_data[author] = {
            'commits': commits, 'additions': additions,
            'deletions': deletions, 'changes': changes
        }

    authors = [{'name': n, 'color': COLORS[i % len(COLORS)]} for i, n in enumerate(sorted_authors)]

    return {
        'authors': authors,
        'timeseries': {'labels': labels, 'data': ts_data},
        'totals': totals
    }


# --- HTTP Server ---

class StatsHandler(BaseHTTPRequestHandler):
    db_path = DB_FILE
    repo_name = ''

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == '/':
            self._respond(200, 'text/html; charset=utf-8', HTML_PAGE.encode())
        elif path == '/api/status':
            data = {**scan_status, 'repo': self.repo_name}
            self._json(data)
        elif path == '/api/data':
            period = params.get('period', ['month'])[0]
            if period not in ('week', 'month', 'quarter', 'year', 'all'):
                period = 'month'
            self._json(get_data(self.db_path, period))
        else:
            self._respond(404, 'text/plain', b'Not found')

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        body = json.dumps(data).encode()
        self._respond(200, 'application/json', body)


# --- HTML Page ---

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Git Stats</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
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
.theme-toggle {
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
.theme-toggle:hover { border-color: rgba(255,255,255,0.5); }
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
.btn-group button {
    padding: 0.5rem 1rem;
    border: none;
    background: var(--btn-bg);
    cursor: pointer;
    font-size: 0.85rem;
    color: var(--btn-text);
    transition: all 0.2s;
    border-right: 1px solid var(--btn-border);
}
.btn-group button:last-child { border-right: none; }
.btn-group button:hover { background: var(--btn-hover); }
.btn-group button.active { background: #4e79a7; color: white; }
.card {
    background: var(--card-bg);
    border-radius: 12px;
    padding: 1.5rem;
    box-shadow: 0 1px 3px var(--shadow);
    margin-bottom: 1.5rem;
}
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
        <button class="theme-toggle" id="themeToggle" title="Přepnout tmavý/světlý režim">&#9790;</button>
    </div>
</header>
<main>
    <div id="loadingView" class="loading">
        <div class="spinner"></div>
        <div class="scan-info" id="scanInfo">Skenování repozitáře...</div>
    </div>
    <div id="mainView" style="display:none">
        <div class="controls">
            <div class="btn-group" id="periodBtns">
                <button data-val="week">Týden</button>
                <button data-val="month" class="active">Měsíc</button>
                <button data-val="quarter">Kvartál</button>
                <button data-val="year">Rok</button>
                <button data-val="all">Vše</button>
            </div>
            <div class="btn-group" id="metricBtns">
                <button data-val="commits" class="active">Commity</button>
                <button data-val="additions">Přidané řádky</button>
                <button data-val="deletions">Smazané řádky</button>
                <button data-val="changes">Změny celkem</button>
            </div>
            <div class="btn-group">
                <button id="trendBtn" class="active">Trend</button>
            </div>
        </div>
        <div class="card">
            <div class="legend" id="legend"></div>
            <div class="chart-container">
                <canvas id="timeChart"></canvas>
            </div>
        </div>
        <div class="pie-grid" id="pieGrid">
            <div class="pie-card">
                <h3>Commity</h3>
                <div class="total" id="totalCommits">0</div>
                <div class="pie-container"><canvas id="pieCommits"></canvas></div>
            </div>
            <div class="pie-card">
                <h3>Přidané řádky</h3>
                <div class="total" id="totalAdditions">0</div>
                <div class="pie-container"><canvas id="pieAdditions"></canvas></div>
            </div>
            <div class="pie-card">
                <h3>Smazané řádky</h3>
                <div class="total" id="totalDeletions">0</div>
                <div class="pie-container"><canvas id="pieDeletions"></canvas></div>
            </div>
            <div class="pie-card">
                <h3>Změny celkem</h3>
                <div class="total" id="totalChanges">0</div>
                <div class="pie-container"><canvas id="pieChanges"></canvas></div>
            </div>
        </div>
    </div>
</main>
<script>
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
        document.getElementById('scanInfo').textContent =
            'Skenování repozitáře... ' + s.total + ' commitů';
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
    // Set initial icon
    document.getElementById('themeToggle').innerHTML = isDark() ? '&#9788;' : '&#9790;';
    document.querySelectorAll('#periodBtns button').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelector('#periodBtns .active').classList.remove('active');
            btn.classList.add('active');
            currentPeriod = btn.dataset.val;
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
        item.innerHTML = '<span class="legend-dot" style="background:' +
            author.color + '"></span>' + author.name;
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
        const name = item.textContent;
        item.classList.toggle('active', activeAuthors.has(name));
        item.classList.toggle('inactive', !activeAuthors.has(name));
    });
}

function renderAll() {
    renderTimeChart();
    renderPieCharts();
}

function renderTimeChart() {
    const ctx = document.getElementById('timeChart');
    if (timeChart) timeChart.destroy();

    if (!chartData || !chartData.authors.length) {
        timeChart = null;
        return;
    }

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
            label: 'Trend (celkem)',
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
                            item.parsed.y.toLocaleString('cs-CZ')
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
                        callback: v => v.toLocaleString('cs-CZ')
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
        document.getElementById(totalIds[i]).textContent = total.toLocaleString('cs-CZ');

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
                                    ctx.parsed.toLocaleString('cs-CZ') + ' (' + pct + '%)';
                            }
                        }
                    }
                }
            }
        });
    });
}

init();
</script>
</body>
</html>
"""


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description='Git Stats - Repository statistics viewer')
    parser.add_argument('-p', '--port', type=int, default=8787, help='Port (default: 8787)')
    parser.add_argument('-n', '--no-browser', action='store_true', help="Don't open browser")
    parser.add_argument('--rescan', action='store_true', help='Force full rescan (clear cache)')
    args = parser.parse_args()

    # Check git repo
    try:
        subprocess.run(['git', 'rev-parse', '--git-dir'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Not a git repository. Run this from inside a git repo.", file=sys.stderr)
        sys.exit(1)

    repo_name = get_repo_name()
    db_path = os.path.join(DB_DIR, 'cache.db')

    if args.rescan and os.path.exists(db_path):
        os.remove(db_path)
        print("Cache cleared.")

    print(f"Git Stats - {repo_name}")
    print("Initializing...")
    init_db(db_path)

    # Configure handler
    StatsHandler.db_path = db_path
    StatsHandler.repo_name = repo_name

    # Start scan in background
    scan_thread = threading.Thread(target=scan_repository, args=(db_path,), daemon=True)
    scan_thread.start()

    # Start server
    ThreadingTCPServer.allow_reuse_address = True
    server = ThreadingTCPServer(('127.0.0.1', args.port), StatsHandler)
    url = f'http://127.0.0.1:{args.port}'
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
