#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local web dashboard for the Batocera game scoring database.

No external dependencies; uses Python's standard library.
"""

from __future__ import annotations

import argparse
import html
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import arcade_db


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_VIEW = "v_top_by_playtime"
LAUNCH_ENV = {"DISPLAY": ":0"}
EMULATOR_LAUNCH_COMMAND = ["/usr/bin/python", "/usr/bin/emulatorlauncher"]
STOP_PROCESS_PATTERNS = [
    "retroarch",
    "duckstation",
    "pcsx2",
    "ppsspp",
    "dolphin-emu",
    "mupen64plus",
    "ryujinx",
    "Ryujinx",
    "yuzu",
    "suyu",
    "eden",
    "citron",
]
DISPLAY_RESET_COMMANDS = [
    ["batocera-resolution", "setMode", "max-1920x1080"],
    ["batocera-resolution", "setMode", "max"],
]

VIEW_CONFIG = {
    "v_all_games": {
        "label": "Alle Spiele",
        "columns": [
            "rank",
            "title",
            "system",
            "playcount",
            "gametime_hours",
            "usage_score",
            "moby_score_100",
            "catchup_score",
            "overall_score",
            "moby_match_status",
            "moby_match_confidence",
            "moby_title",
            "moby_url",
            "path",
            "game_key",
        ],
    },
    "v_top_by_catchup": {
        "label": "Nachholbedarf",
        "columns": [
            "rank",
            "title",
            "system",
            "playcount",
            "gametime_hours",
            "usage_score",
            "moby_score_100",
            "catchup_score",
            "moby_title",
            "moby_url",
            "path",
            "game_key",
        ],
    },
    "v_top_by_combined_60_40": {
        "label": "Kombi 60/40",
        "columns": ["rank", "title", "system", "usage_score", "moby_score_100", "combined_score", "moby_title", "path", "game_key"],
    },
    "v_top_by_moby_score": {
        "label": "MobyGames",
        "columns": ["rank", "title", "system", "moby_score_100", "moby_match_confidence", "moby_title", "moby_url", "path", "game_key"],
    },
    "v_top_by_usage": {
        "label": "Nutzung",
        "columns": ["rank", "title", "system", "playcount", "gametime_hours", "usage_score", "path", "game_key"],
    },
    "v_top_by_playtime": {
        "label": "Spielzeit",
        "columns": ["rank", "title", "system", "gametime_hours", "playcount", "usage_score", "path", "game_key"],
    },
    "v_mobygames_pending": {
        "label": "Moby offen",
        "columns": ["rank", "title", "system", "usage_score", "playcount", "gametime_hours", "path", "game_key"],
    },
    "v_mobygames_review": {
        "label": "Moby Review",
        "columns": ["rank", "title", "system", "moby_match_status", "moby_match_confidence", "moby_title", "moby_url", "path", "game_key"],
    },
}


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self.logs: list[str] = []
        self.process: subprocess.Popen[str] | None = None

    def is_running(self) -> bool:
        with self.lock:
            return bool(self.current and self.current.get("running"))

    def start(self, name: str, command: list[str]) -> tuple[bool, str]:
        with self.lock:
            if self.current and self.current.get("running"):
                return False, f"Job already running: {self.current['name']}"
            self.current = {
                "name": name,
                "command": command,
                "started_at": time.time(),
                "finished_at": None,
                "returncode": None,
                "running": True,
            }
            self.logs = [f"$ {' '.join(command)}"]

        thread = threading.Thread(target=self._run, args=(name, command), daemon=True)
        thread.start()
        return True, f"Started {name}"

    def _run(self, name: str, command: list[str]) -> None:
        try:
            process = subprocess.Popen(
                command,
                cwd=SCRIPT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                self.add_log(line.rstrip())
            returncode = process.wait()
        except Exception as error:
            self.add_log(f"[ERROR] {type(error).__name__}: {error}")
            returncode = 1

        with self.lock:
            if self.current:
                self.current["running"] = False
                self.current["finished_at"] = time.time()
                self.current["returncode"] = returncode
        self.add_log(f"[DONE] {name} exited with code {returncode}")

    def add_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-500:]

    def snapshot(self) -> tuple[dict[str, Any] | None, list[str]]:
        with self.lock:
            return (dict(self.current) if self.current else None, list(self.logs))


JOB_MANAGER = JobManager()


class GameLauncher:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current: dict[str, Any] | None = None
        self.logs: list[str] = []

    def start(self, db_path: Path, game_key: str) -> tuple[bool, str]:
        game = load_game_for_launch(db_path, game_key)
        if not game:
            return False, "Game not found"
        if not game["path"] or not os.path.exists(game["path"]):
            return False, f"ROM path missing: {game['path']}"

        command = [
            *EMULATOR_LAUNCH_COMMAND,
            "-system",
            game["system"],
            "-rom",
            game["path"],
        ]
        env = os.environ.copy()
        env.update(LAUNCH_ENV)

        with self.lock:
            if self.current and self.current.get("running"):
                return False, f"Game already running: {self.current['title']}"
            self.logs = [f"$ DISPLAY=:0 {shlex.join(command)}"]
            self.current = {
                "game_key": game_key,
                "title": game["title"],
                "system": game["system"],
                "path": game["path"],
                "started_at": time.time(),
                "returncode": None,
                "running": True,
            }

        try:
            process = subprocess.Popen(
                command,
                cwd="/",
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except Exception as error:
            with self.lock:
                if self.current:
                    self.current["running"] = False
                    self.current["returncode"] = 1
            self.add_log(f"[ERROR] {type(error).__name__}: {error}")
            return False, str(error)

        with self.lock:
            self.process = process
            if self.current:
                self.current["pid"] = process.pid

        thread = threading.Thread(target=self._watch, args=(process,), daemon=True)
        thread.start()
        return True, f"Launching {game['title']}"

    def _watch(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            self.add_log(line)
            if "no generator found for emulator" in line:
                self.add_log("[HINT] Batocera config points to an emulator generator that is not installed. Check /userdata/system/batocera.conf for this system.")
        returncode = process.wait()
        with self.lock:
            if self.current:
                self.current["running"] = False
                self.current["returncode"] = returncode
            self.process = None
        self.add_log(f"[DONE] game exited with code {returncode}")

    def stop(self) -> tuple[bool, str]:
        stopped = []
        self.add_log("Stopping game...")
        with self.lock:
            pid = self.current.get("pid") if self.current else None
            process = self.process

        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGINT)
                stopped.append(f"launcher process group SIGINT {process.pid}")
                self.add_log(f"Sent SIGINT to launcher process group {process.pid}")
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.add_log("[WARN] launcher did not exit after SIGINT; sending SIGTERM")
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    stopped.append(f"launcher process group SIGTERM {process.pid}")
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.add_log("[WARN] launcher did not exit after SIGTERM; sending SIGKILL")
                    os.killpg(process.pid, signal.SIGKILL)
                    stopped.append(f"launcher process group SIGKILL {process.pid}")
            except ProcessLookupError:
                pass
            except Exception as error:
                self.add_log(f"[WARN] failed to stop launcher process group: {type(error).__name__}: {error}")

        if pid and not stopped:
            try:
                subprocess.run(["kill", "-INT", str(pid)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                stopped.append(f"pid {pid} SIGINT")
            except Exception as error:
                self.add_log(f"[WARN] failed to kill launcher pid {pid}: {error}")

        time.sleep(3)
        if process and process.poll() is None:
            for pattern in STOP_PROCESS_PATTERNS:
                result = subprocess.run(["pkill", "-TERM", "-f", pattern], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode == 0:
                    stopped.append(f"{pattern} SIGTERM")

        self.reset_display()

        with self.lock:
            if self.current:
                self.current["running"] = False
                self.current["returncode"] = process.returncode if process else -15
        message = "Stopped " + ", ".join(stopped) if stopped else "No emulator process matched"
        self.add_log(message)
        return bool(stopped), message

    def _run_command(self, command: list[str], label: str) -> bool:
        env = os.environ.copy()
        env.update(LAUNCH_ENV)
        try:
            result = subprocess.run(
                command,
                cwd="/",
                env=env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
        except Exception as error:
            self.add_log(f"[WARN] {label} failed: {type(error).__name__}: {error}")
            return False
        if result.stdout.strip():
            self.add_log(f"{label}: {result.stdout.strip()}")
        return result.returncode == 0

    def reset_display(self) -> None:
        for command in DISPLAY_RESET_COMMANDS:
            if self._run_command(command, "display reset"):
                self.add_log(f"display reset: {' '.join(command)}")
                return
        self.add_log("[WARN] display reset command failed")

    def add_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line)
            self.logs = self.logs[-300:]

    def snapshot(self) -> tuple[dict[str, Any] | None, list[str]]:
        with self.lock:
            return (dict(self.current) if self.current else None, list(self.logs))


GAME_LAUNCHER = GameLauncher()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local web dashboard for perfect_arcade_games.sqlite.")
    parser.add_argument("--db", default=str(arcade_db.DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    return parser.parse_args()


def table_exists(conn: sqlite3.Connection, name: str, kind: str = "table") -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        (kind, name),
    ).fetchone()
    return row is not None


def load_game_for_launch(db_path: Path, game_key: str) -> dict[str, str] | None:
    conn = arcade_db.connect_db(db_path)
    try:
        row = conn.execute(
            """
            SELECT game_key, title, system, path
            FROM v_active_games
            WHERE game_key = ?
            """,
            (game_key,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "game_key": row["game_key"],
        "title": row["title"],
        "system": row["system"],
        "path": row["path"],
    }


def db_stats(db_path: Path) -> dict[str, Any]:
    conn = arcade_db.connect_db(db_path)
    try:
        def count(sql: str) -> int:
            try:
                return int(conn.execute(sql).fetchone()[0])
            except sqlite3.OperationalError:
                return 0

        stats = {
            "games": count("SELECT COUNT(*) FROM games WHERE favorite = 1 AND hidden = 0 AND file_exists = 1"),
            "moby_matches": count("SELECT COUNT(*) FROM mobygames_matches"),
            "pending": count("SELECT COUNT(*) FROM v_mobygames_pending"),
            "review": count("SELECT COUNT(*) FROM v_mobygames_review"),
            "catchup_ready": count("SELECT COUNT(*) FROM v_top_by_catchup"),
        }
    finally:
        conn.close()
    return stats


def query_view(
    db_path: Path,
    view_name: str,
    limit: int,
    page: int,
    sort: str,
    direction: str,
    search: str,
) -> tuple[list[str], list[sqlite3.Row], int]:
    if view_name not in VIEW_CONFIG:
        view_name = DEFAULT_VIEW
    limit = max(1, min(1000, int(limit)))
    page = max(1, int(page))
    columns = VIEW_CONFIG[view_name]["columns"]
    sort_column = sort if sort in columns else "rank"
    sort_direction = "DESC" if direction.lower() == "desc" else "ASC"
    if sort_column == "rank" and not sort:
        sort_direction = "ASC"
    col_sql = ", ".join(columns)
    offset = (page - 1) * limit
    where_sql = ""
    query_params: list[Any] = []
    search = search.strip()
    if search:
        where_sql = "WHERE title LIKE ? OR system LIKE ?"
        pattern = f"%{search}%"
        query_params.extend([pattern, pattern])

    conn = arcade_db.connect_db(db_path)
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM {view_name} {where_sql}", query_params).fetchone()[0]
        rows = list(
            conn.execute(
                f"""
                SELECT {col_sql}
                FROM {view_name}
                {where_sql}
                ORDER BY {sort_column} IS NULL, {sort_column} {sort_direction}, title COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
                """,
                (*query_params, limit, offset),
            )
        )
    finally:
        conn.close()
    return columns, rows, total


def refresh_scores(db_path: Path) -> None:
    conn = arcade_db.connect_db(db_path)
    try:
        arcade_db.update_usage_scores(conn)
        arcade_db.update_overall_scores(conn)
        conn.commit()
    finally:
        conn.close()


def esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def render_page(db_path: Path, params: dict[str, list[str]]) -> bytes:
    selected_view = params.get("view", [DEFAULT_VIEW])[0]
    if selected_view not in VIEW_CONFIG:
        selected_view = DEFAULT_VIEW
    limit_text = params.get("limit", ["100"])[0]
    page_text = params.get("page", ["1"])[0]
    sort = params.get("sort", ["rank"])[0]
    direction = params.get("dir", ["asc"])[0].lower()
    search = params.get("q", [""])[0].strip()
    try:
        limit = int(limit_text)
    except ValueError:
        limit = 100
    try:
        page = int(page_text)
    except ValueError:
        page = 1

    stats = db_stats(db_path)
    columns, rows, total_rows = query_view(db_path, selected_view, limit, page, sort, direction, search)
    job, logs = JOB_MANAGER.snapshot()
    game, game_logs = GAME_LAUNCHER.snapshot()

    view_options = "\n".join(
        f'<option value="{esc(name)}" {"selected" if name == selected_view else ""}>{esc(config["label"])}</option>'
        for name, config in VIEW_CONFIG.items()
    )
    cards = "\n".join(
        f'<div class="stat"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'
        for label, value in [
            ("Games", stats["games"]),
            ("Moby matches", stats["moby_matches"]),
            ("Pending", stats["pending"]),
            ("Review", stats["review"]),
            ("Catch-up ready", stats["catchup_ready"]),
        ]
    )
    total_pages = max(1, (total_rows + max(1, limit) - 1) // max(1, limit))
    page = min(max(1, page), total_pages)
    opposite_dir = "desc" if direction != "desc" else "asc"
    display_columns = [col for col in columns if col not in ("path", "game_key")]
    header_cells = ['<th>action</th>']
    for col in display_columns:
        next_dir = opposite_dir if col == sort else "asc"
        marker = " ▲" if col == sort and direction != "desc" else " ▼" if col == sort else ""
        href = (
            f"/?view={urllib.parse.quote(selected_view)}&limit={limit}&page=1"
            f"&sort={urllib.parse.quote(col)}&dir={next_dir}&q={urllib.parse.quote(search)}"
        )
        header_cells.append(f'<th><a href="{href}">{esc(col)}{marker}</a></th>')
    header = "".join(header_cells)
    body_rows = []
    for row in rows:
        game_key = row["game_key"] if "game_key" in row.keys() else ""
        rom_path = row["path"] if "path" in row.keys() else ""
        cells = [
            (
                '<td><form method="post" action="/action">'
                '<input type="hidden" name="action" value="launch">'
                f'<input type="hidden" name="game_key" value="{esc(game_key)}">'
                f'<button type="submit" {"disabled" if not rom_path else ""}>Launch</button>'
                '</form></td>'
            )
        ]
        for col in display_columns:
            value = row[col]
            if col.endswith("_url") and value:
                cells.append(f'<td><a href="{esc(value)}" target="_blank" rel="noreferrer">link</a></td>')
            else:
                cells.append(f"<td>{esc(value)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    table_body = "\n".join(body_rows)

    if job:
        status = "running" if job.get("running") else f"done ({job.get('returncode')})"
        job_text = f"{job['name']} - {status}"
        job_key = f"{job.get('name')}:{job.get('started_at')}:{job.get('running')}:{job.get('returncode')}"
    else:
        job_text = ""
        job_key = "logs"
    log_text = "\n".join(esc(line) for line in logs[-120:])
    if game:
        game_status = "running" if game.get("running") else f"done ({game.get('returncode')})"
        game_text = f"{game.get('title')} [{game.get('system')}] - {game_status}"
    else:
        game_text = "No game running"
    game_log_text = "\n".join(esc(line) for line in game_logs[-80:])
    stop_disabled = "" if game and game.get("running") else "disabled"
    game_log_block = f"<pre>{game_log_text}</pre>" if game_logs else ""
    game_section = f"""
      <section class="job status-panel game-panel">
        <div class="panel-title"><strong>{esc(game_text)}</strong></div>
        <form method="post" action="/action" class="panel-action">
          <input type="hidden" name="action" value="stop_game">
          <button type="submit" {stop_disabled}>Stop Game</button>
        </form>
        {game_log_block}
      </section>
    """
    job_section = ""
    if job or logs:
        job_section = f"""
      <section class="job status-panel dismissible" id="job-section" data-dismiss-key="{esc(job_key)}">
        <div class="panel-title"><strong>{esc(job_text)}</strong></div>
        <button type="button" class="panel-close" title="Close" aria-label="Close" onclick="dismissPanel('job-section')">X</button>
        <pre>{log_text}</pre>
      </section>
    """
    prev_page = max(1, page - 1)
    next_page = min(total_pages, page + 1)
    base_query = (
        f"view={urllib.parse.quote(selected_view)}&limit={limit}"
        f"&sort={urllib.parse.quote(sort)}&dir={direction}&q={urllib.parse.quote(search)}"
    )
    pagination = f"""
      <div class="pagination">
        <a class="button" href="/?{base_query}&page=1">First</a>
        <a class="button" href="/?{base_query}&page={prev_page}">Prev</a>
        <span>Page {page} / {total_pages} ({total_rows} rows)</span>
        <a class="button" href="/?{base_query}&page={next_page}">Next</a>
        <a class="button" href="/?{base_query}&page={total_pages}">Last</a>
      </div>
    """

    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Batocera Game Scorer - {esc(stats["games"])} Games</title>
  <script>
    function dismissPanel(id) {{
      const panel = document.getElementById(id);
      if (!panel) return;
      localStorage.setItem("dismissed:" + id, panel.dataset.dismissKey || "");
      panel.hidden = true;
    }}
    document.addEventListener("DOMContentLoaded", function () {{
      document.querySelectorAll(".dismissible").forEach(function (panel) {{
        const dismissedKey = localStorage.getItem("dismissed:" + panel.id);
        if (dismissedKey && dismissedKey === panel.dataset.dismissKey) {{
          panel.hidden = true;
        }}
      }});
    }});
  </script>
  <style>
    :root {{
      --bg: #f6f7f8;
      --panel: #ffffff;
      --ink: #172026;
      --muted: #607080;
      --line: #d9e0e6;
      --accent: #0f766e;
      --accent-dark: #0b5f59;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }}
    header {{
      padding: 18px 24px;
      background: #122026;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}
    h1 {{ margin: 0; font-size: 22px; font-weight: 650; }}
    main {{ padding: 18px 24px 32px; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .stat span {{ display: block; color: var(--muted); font-size: 12px; }}
    .stat strong {{ display: block; margin-top: 4px; font-size: 22px; }}
    .toolbar, .actions, .job {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 14px;
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .status-panel {{ position: relative; }}
    .panel-title {{ padding-right: 34px; }}
    .panel-action {{ margin-left: auto; }}
    label {{ color: var(--muted); font-size: 13px; }}
    select, input {{
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: #fff;
      color: var(--ink);
    }}
    button, .button {{
      height: 36px;
      border: 0;
      border-radius: 6px;
      padding: 0 12px;
      background: var(--accent);
      color: white;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }}
    button:hover, .button:hover {{ background: var(--accent-dark); }}
    button.panel-close {{
      position: absolute;
      top: 8px;
      right: 8px;
      width: 28px;
      height: 28px;
      padding: 0;
      justify-content: center;
      background: #e2e8ee;
      color: var(--ink);
      font-weight: 800;
    }}
    button.panel-close:hover {{ background: #cbd5de; }}
    button:disabled, button:disabled:hover {{
      background: #b8c4cc;
      cursor: not-allowed;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
      white-space: nowrap;
    }}
    th {{ background: #edf2f5; position: sticky; top: 0; z-index: 1; }}
    th a {{ color: var(--ink); text-decoration: none; display: block; }}
    .tablewrap {{ overflow: auto; max-height: 68vh; border-radius: 8px; }}
    .pagination {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin: 10px 0 14px;
    }}
    pre {{
      width: 100%;
      margin: 8px 0 0;
      padding: 10px;
      background: #101820;
      color: #d6e4ef;
      border-radius: 6px;
      overflow: auto;
      max-height: 220px;
      font-size: 12px;
    }}
    .muted {{ color: var(--muted); }}
  </style>
</head>
<body>
  <header>
    <h1>Batocera Game Scorer - {esc(stats["games"])} Games</h1>
    <div class="muted">{esc(db_path)}</div>
  </header>
  <main>
    {job_section}
    <form class="toolbar" method="get" action="/">
      <label>View <select name="view">{view_options}</select></label>
      <label>Search <input name="q" value="{esc(search)}" size="18" placeholder="title or system"></label>
      <label>Limit <input name="limit" value="{esc(limit)}" inputmode="numeric" size="5"></label>
      <input type="hidden" name="page" value="1">
      <input type="hidden" name="sort" value="{esc(sort)}">
      <input type="hidden" name="dir" value="{esc(direction)}">
      <button type="submit">Show</button>
    </form>
    <div class="tablewrap"><table><thead><tr>{header}</tr></thead><tbody>{table_body}</tbody></table></div>
    {pagination}
    {game_section}
    <section class="actions">
      <form method="post" action="/action"><input type="hidden" name="action" value="scan"><button type="submit">Update Playtime</button></form>
      <form method="post" action="/action"><input type="hidden" name="action" value="refresh"><button type="submit">Refresh Scores</button></form>
      <form method="post" action="/action">
        <input type="hidden" name="action" value="enrich">
        <label>Moby limit <input name="limit" value="250" inputmode="numeric" size="5"></label>
        <button type="submit">Continue MobyGames</button>
      </form>
    </section>
    <section class="stats">{cards}</section>
  </main>
</body>
</html>"""
    return html_text.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    db_path: Path

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        body = render_page(self.db_path, params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/action":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        form = urllib.parse.parse_qs(data)
        action = form.get("action", [""])[0]

        if action == "scan":
            JOB_MANAGER.start("scan playtime", [sys.executable, str(SCRIPT_DIR / "scan_batocera_playtime.py")])
        elif action == "refresh":
            refresh_scores(self.db_path)
            JOB_MANAGER.add_log("Scores refreshed")
        elif action == "enrich":
            limit = form.get("limit", ["250"])[0]
            command = [sys.executable, str(SCRIPT_DIR / "enrich_mobygames.py")]
            if limit:
                command += ["--limit", limit]
            JOB_MANAGER.start("enrich mobygames", command)
        elif action == "launch":
            game_key = form.get("game_key", [""])[0]
            ok, message = GAME_LAUNCHER.start(self.db_path, game_key)
            GAME_LAUNCHER.add_log(message if ok else f"[ERROR] {message}")
        elif action == "stop_game":
            GAME_LAUNCHER.stop()

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        print("Run scan_batocera_playtime.py first.", file=sys.stderr)
        return 1

    arcade_db.connect_db(db_path).close()
    DashboardHandler.db_path = db_path
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Dashboard running on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
