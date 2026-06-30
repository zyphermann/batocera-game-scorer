#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared SQLite helpers for the Perfect Arcade Games pipeline.

This module is intentionally dependency-free so it can run on Batocera.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = SCRIPT_DIR / "perfect_arcade_games.sqlite"
SCHEMA_VERSION = 2


def now_ts() -> int:
    return int(time.time())


def connect_db(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games (
          game_key TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          norm_title TEXT NOT NULL,
          system TEXT NOT NULL,
          batocera_id TEXT,
          md5 TEXT,
          path TEXT,
          favorite INTEGER NOT NULL DEFAULT 1,
          hidden INTEGER NOT NULL DEFAULT 0,
          file_exists INTEGER NOT NULL DEFAULT 0,
          first_seen_at INTEGER NOT NULL,
          last_seen_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_games_system_title
          ON games(system, norm_title);

        CREATE INDEX IF NOT EXISTS idx_games_md5
          ON games(md5);

        CREATE TABLE IF NOT EXISTS playtime (
          game_key TEXT PRIMARY KEY,
          playcount INTEGER NOT NULL DEFAULT 0,
          gametime INTEGER NOT NULL DEFAULT 0,
          gametime_hours REAL NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY (game_key) REFERENCES games(game_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS playtime_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          game_key TEXT NOT NULL,
          playcount INTEGER NOT NULL,
          gametime INTEGER NOT NULL,
          gametime_hours REAL NOT NULL,
          scanned_at INTEGER NOT NULL,
          FOREIGN KEY (game_key) REFERENCES games(game_key) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_playtime_history_game_time
          ON playtime_history(game_key, scanned_at);

        CREATE TABLE IF NOT EXISTS scores (
          game_key TEXT PRIMARY KEY,
          usage_score REAL,
          moby_score_100 REAL,
          catchup_score REAL,
          manual_score REAL,
          overall_score REAL,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY (game_key) REFERENCES games(game_key) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_scores_overall
          ON scores(overall_score DESC);

        CREATE INDEX IF NOT EXISTS idx_scores_usage
          ON scores(usage_score DESC);

        CREATE TABLE IF NOT EXISTS mobygames_matches (
          game_key TEXT PRIMARY KEY,
          moby_game_id INTEGER,
          moby_title TEXT,
          moby_platforms TEXT,
          moby_url TEXT,
          moby_score REAL,
          moby_score_100 REAL,
          moby_num_votes INTEGER,
          match_status TEXT NOT NULL,
          match_confidence REAL,
          query_titles TEXT,
          platform_query TEXT,
          updated_at INTEGER NOT NULL,
          FOREIGN KEY (game_key) REFERENCES games(game_key) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_mobygames_status
          ON mobygames_matches(match_status, match_confidence DESC);

        CREATE TABLE IF NOT EXISTS api_cache (
          cache_key TEXT PRIMARY KEY,
          url TEXT NOT NULL,
          status INTEGER NOT NULL,
          response TEXT NOT NULL,
          fetched_at INTEGER NOT NULL
        );
        """
    )
    conn.executescript(
        """
        DROP VIEW IF EXISTS v_mobygames_review;
        DROP VIEW IF EXISTS v_mobygames_pending;
        DROP VIEW IF EXISTS v_top_by_catchup;
        DROP VIEW IF EXISTS v_top_by_playtime;
        DROP VIEW IF EXISTS v_top_by_combined_60_40;
        DROP VIEW IF EXISTS v_top_by_moby_score;
        DROP VIEW IF EXISTS v_top_by_usage;
        DROP VIEW IF EXISTS v_all_games;
        DROP VIEW IF EXISTS v_active_games;

        CREATE VIEW IF NOT EXISTS v_active_games AS
        SELECT
          g.game_key,
          g.title,
          g.system,
          g.batocera_id,
          g.md5,
          g.path,
          p.playcount,
          p.gametime,
          p.gametime_hours,
          s.usage_score,
          s.moby_score_100,
          s.catchup_score,
          s.manual_score,
          s.overall_score,
          m.match_status AS moby_match_status,
          m.match_confidence AS moby_match_confidence,
          m.moby_title,
          m.moby_url
        FROM games g
        LEFT JOIN playtime p ON p.game_key = g.game_key
        LEFT JOIN scores s ON s.game_key = g.game_key
        LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
        WHERE g.favorite = 1
          AND g.hidden = 0
          AND g.file_exists = 1;

        CREATE VIEW IF NOT EXISTS v_all_games AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY title COLLATE NOCASE ASC, system COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games;

        CREATE VIEW IF NOT EXISTS v_top_by_usage AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY usage_score DESC, title COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games
        WHERE usage_score IS NOT NULL;

        CREATE VIEW IF NOT EXISTS v_top_by_moby_score AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY moby_score_100 DESC, title COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games
        WHERE moby_score_100 IS NOT NULL;

        CREATE VIEW IF NOT EXISTS v_top_by_playtime AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY gametime DESC, playcount DESC, title COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games
        WHERE gametime IS NOT NULL;

        CREATE VIEW IF NOT EXISTS v_top_by_combined_60_40 AS
        SELECT
          ROW_NUMBER() OVER (
            ORDER BY ROUND((usage_score * 0.6) + (moby_score_100 * 0.4), 2) DESC, title COLLATE NOCASE ASC
          ) AS rank,
          *,
          ROUND((usage_score * 0.6) + (moby_score_100 * 0.4), 2) AS combined_score
        FROM v_active_games
        WHERE usage_score IS NOT NULL
          AND moby_score_100 IS NOT NULL;

        CREATE VIEW IF NOT EXISTS v_top_by_catchup AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY catchup_score DESC, moby_score_100 DESC, title COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games
        WHERE catchup_score IS NOT NULL;

        CREATE VIEW IF NOT EXISTS v_mobygames_pending AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY usage_score DESC, gametime DESC, title COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games
        WHERE moby_match_status IS NULL;

        CREATE VIEW IF NOT EXISTS v_mobygames_review AS
        SELECT
          ROW_NUMBER() OVER (ORDER BY moby_match_confidence ASC, title COLLATE NOCASE ASC) AS rank,
          *
        FROM v_active_games
        WHERE moby_match_status = 'review';
        """
    )
    ensure_column(conn, "scores", "catchup_score", "REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scores_catchup ON scores(catchup_score DESC)")
    conn.execute(
        """
        INSERT INTO schema_meta(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})")
    }
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def build_game_key(system: str, md5: str = "", batocera_id: str = "", path: str = "", norm_title: str = "") -> str:
    system = (system or "-").strip().casefold()
    md5 = (md5 or "").strip().casefold()
    batocera_id = (batocera_id or "").strip()
    path = (path or "").strip()
    norm_title = (norm_title or "").strip().casefold()

    if md5:
        return f"{system}:md5:{md5}"
    if batocera_id and batocera_id != "-":
        return f"{system}:id:{batocera_id}"
    if path:
        return f"{system}:path:{path}"
    return f"{system}:title:{norm_title}"


def upsert_game(
    conn: sqlite3.Connection,
    *,
    game_key: str,
    title: str,
    norm_title: str,
    system: str,
    batocera_id: str | None = None,
    md5: str | None = None,
    path: str | None = None,
    favorite: bool = True,
    hidden: bool = False,
    file_exists: bool = True,
    seen_at: int | None = None,
) -> None:
    ts = seen_at or now_ts()
    conn.execute(
        """
        INSERT INTO games (
          game_key, title, norm_title, system, batocera_id, md5, path,
          favorite, hidden, file_exists, first_seen_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_key) DO UPDATE SET
          title = excluded.title,
          norm_title = excluded.norm_title,
          system = excluded.system,
          batocera_id = excluded.batocera_id,
          md5 = excluded.md5,
          path = excluded.path,
          favorite = excluded.favorite,
          hidden = excluded.hidden,
          file_exists = excluded.file_exists,
          last_seen_at = excluded.last_seen_at
        """,
        (
            game_key,
            title,
            norm_title,
            system,
            batocera_id,
            md5,
            path,
            int(favorite),
            int(hidden),
            int(file_exists),
            ts,
            ts,
        ),
    )


def upsert_playtime(
    conn: sqlite3.Connection,
    *,
    game_key: str,
    playcount: int,
    gametime: int,
    updated_at: int | None = None,
    write_history: bool = True,
) -> None:
    ts = updated_at or now_ts()
    gametime_hours = round((gametime or 0) / 3600, 2)
    conn.execute(
        """
        INSERT INTO playtime(game_key, playcount, gametime, gametime_hours, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(game_key) DO UPDATE SET
          playcount = excluded.playcount,
          gametime = excluded.gametime,
          gametime_hours = excluded.gametime_hours,
          updated_at = excluded.updated_at
        """,
        (game_key, int(playcount or 0), int(gametime or 0), gametime_hours, ts),
    )
    if write_history:
        conn.execute(
            """
            INSERT INTO playtime_history(game_key, playcount, gametime, gametime_hours, scanned_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (game_key, int(playcount or 0), int(gametime or 0), gametime_hours, ts),
        )


def log_normalized(value: float | int | None, max_value: float | int | None) -> float:
    if value is None or max_value is None or value <= 0 or max_value <= 0:
        return 0.0
    return math.log1p(float(value)) / math.log1p(float(max_value))


def update_usage_scores(
    conn: sqlite3.Connection,
    *,
    playcount_weight: float = 0.65,
    gametime_weight: float = 0.35,
) -> None:
    row = conn.execute(
        """
        SELECT
          MAX(playcount) AS max_playcount,
          MAX(gametime_hours) AS max_gametime_hours
        FROM playtime
        """
    ).fetchone()
    max_playcount = row["max_playcount"] if row else 0
    max_gametime_hours = row["max_gametime_hours"] if row else 0
    ts = now_ts()

    for item in conn.execute("SELECT game_key, playcount, gametime_hours FROM playtime"):
        usage_score = 100 * (
            playcount_weight * log_normalized(item["playcount"], max_playcount)
            + gametime_weight * log_normalized(item["gametime_hours"], max_gametime_hours)
        )
        conn.execute(
            """
            INSERT INTO scores(game_key, usage_score, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(game_key) DO UPDATE SET
              usage_score = excluded.usage_score,
              updated_at = excluded.updated_at
            """,
            (item["game_key"], round(usage_score, 2), ts),
        )


def upsert_mobygames_match(
    conn: sqlite3.Connection,
    *,
    game_key: str,
    match_status: str,
    moby_game_id: int | None = None,
    moby_title: str | None = None,
    moby_platforms: str | Iterable[str] | None = None,
    moby_url: str | None = None,
    moby_score: float | None = None,
    moby_num_votes: int | None = None,
    match_confidence: float | None = None,
    query_titles: str | Iterable[str] | None = None,
    platform_query: str | None = None,
    updated_at: int | None = None,
) -> None:
    ts = updated_at or now_ts()
    platforms_text = _join_text(moby_platforms)
    query_titles_text = _join_text(query_titles)
    moby_score_100 = None if moby_score is None else round(float(moby_score) * 20, 2)

    conn.execute(
        """
        INSERT INTO mobygames_matches (
          game_key, moby_game_id, moby_title, moby_platforms, moby_url,
          moby_score, moby_score_100, moby_num_votes, match_status,
          match_confidence, query_titles, platform_query, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_key) DO UPDATE SET
          moby_game_id = excluded.moby_game_id,
          moby_title = excluded.moby_title,
          moby_platforms = excluded.moby_platforms,
          moby_url = excluded.moby_url,
          moby_score = excluded.moby_score,
          moby_score_100 = excluded.moby_score_100,
          moby_num_votes = excluded.moby_num_votes,
          match_status = excluded.match_status,
          match_confidence = excluded.match_confidence,
          query_titles = excluded.query_titles,
          platform_query = excluded.platform_query,
          updated_at = excluded.updated_at
        """,
        (
            game_key,
            moby_game_id,
            moby_title,
            platforms_text,
            moby_url,
            moby_score,
            moby_score_100,
            moby_num_votes,
            match_status,
            match_confidence,
            query_titles_text,
            platform_query,
            ts,
        ),
    )
    conn.execute(
        """
        INSERT INTO scores(game_key, moby_score_100, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(game_key) DO UPDATE SET
          moby_score_100 = excluded.moby_score_100,
          updated_at = excluded.updated_at
        """,
        (game_key, moby_score_100, ts),
    )


def update_catchup_scores(conn: sqlite3.Connection) -> None:
    ts = now_ts()
    for row in conn.execute("SELECT game_key, usage_score, moby_score_100 FROM scores"):
        usage_score = row["usage_score"]
        moby_score_100 = row["moby_score_100"]
        if usage_score is None or moby_score_100 is None:
            catchup_score = None
        else:
            usage_factor = max(0.0, min(1.0, float(usage_score) / 100.0))
            catchup_score = round(float(moby_score_100) * (1.0 - usage_factor), 2)

        conn.execute(
            """
            UPDATE scores
            SET catchup_score = ?, updated_at = ?
            WHERE game_key = ?
            """,
            (catchup_score, ts, row["game_key"]),
        )


def update_overall_scores(
    conn: sqlite3.Connection,
    *,
    usage_weight: float = 0.45,
    moby_weight: float = 0.35,
    manual_weight: float = 0.20,
) -> None:
    ts = now_ts()
    update_catchup_scores(conn)
    for row in conn.execute("SELECT game_key, usage_score, moby_score_100, manual_score FROM scores"):
        weighted_sum = 0.0
        used_weight = 0.0
        for score, weight in (
            (row["usage_score"], usage_weight),
            (row["moby_score_100"], moby_weight),
            (row["manual_score"], manual_weight),
        ):
            if score is not None:
                weighted_sum += float(score) * weight
                used_weight += weight
        overall = None if used_weight == 0 else round(weighted_sum / used_weight, 2)
        conn.execute(
            """
            UPDATE scores
            SET overall_score = ?, updated_at = ?
            WHERE game_key = ?
            """,
            (overall, ts, row["game_key"]),
        )


def cache_get(conn: sqlite3.Connection, cache_key: str) -> tuple[int, Any] | None:
    row = conn.execute("SELECT status, response FROM api_cache WHERE cache_key = ?", (cache_key,)).fetchone()
    if not row:
        return None
    return int(row["status"]), json.loads(row["response"])


def cache_put(conn: sqlite3.Connection, cache_key: str, url: str, status: int, response: Any, fetched_at: int | None = None) -> None:
    conn.execute(
        """
        INSERT INTO api_cache(cache_key, url, status, response, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
          url = excluded.url,
          status = excluded.status,
          response = excluded.response,
          fetched_at = excluded.fetched_at
        """,
        (cache_key, url, int(status), json.dumps(response, ensure_ascii=False), fetched_at or now_ts()),
    )


def cache_delete(conn: sqlite3.Connection, cache_key: str) -> None:
    conn.execute("DELETE FROM api_cache WHERE cache_key = ?", (cache_key,))


def top_games(conn: sqlite3.Connection, limit: int = 250, order_by: str = "overall_score") -> list[sqlite3.Row]:
    allowed = {
        "overall_score": "s.overall_score",
        "usage_score": "s.usage_score",
        "moby_score_100": "s.moby_score_100",
        "catchup_score": "s.catchup_score",
        "gametime": "p.gametime",
        "playcount": "p.playcount",
    }
    order_expr = allowed.get(order_by)
    if order_expr is None:
        raise ValueError(f"Unsupported order_by: {order_by}")

    return list(
        conn.execute(
            f"""
            SELECT
              g.game_key,
              g.title,
              g.system,
              g.batocera_id,
              g.md5,
              g.path,
              p.playcount,
              p.gametime,
              p.gametime_hours,
              s.usage_score,
              s.moby_score_100,
              s.catchup_score,
              s.manual_score,
              s.overall_score,
              m.match_status AS moby_match_status,
              m.match_confidence AS moby_match_confidence,
              m.moby_title,
              m.moby_url
            FROM games g
            LEFT JOIN playtime p ON p.game_key = g.game_key
            LEFT JOIN scores s ON s.game_key = g.game_key
            LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
            WHERE g.favorite = 1
              AND g.hidden = 0
              AND g.file_exists = 1
            ORDER BY
              {order_expr} IS NULL,
              {order_expr} DESC,
              g.title COLLATE NOCASE ASC
            LIMIT ?
            """,
            (int(limit),),
        )
    )


def _join_text(value: str | Iterable[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return "; ".join(str(item) for item in value)
