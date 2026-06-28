#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Export ranked games from the central SQLite database to CSV.
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from pathlib import Path

import arcade_db


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "top_games.csv"


SCHEMAS = {
    "local_usage": {
        "label": "Local Usage",
        "description": "Mix aus Starts und Spielzeit.",
        "weights": {"playcount_norm": 0.60, "gametime_norm": 0.40},
    },
    "quality_mix": {
        "label": "Quality Mix",
        "description": "Mix aus Starts, Spielzeit und MobyGames-Score.",
        "weights": {"playcount_norm": 0.30, "gametime_norm": 0.25, "moby_score_100": 0.45},
    },
    "playtime_focus": {
        "label": "Playtime Focus",
        "description": "Sortiert primaer nach Spielzeit.",
        "weights": {"gametime_norm": 1.00},
    },
    "replay_focus": {
        "label": "Replay Focus",
        "description": "Sortiert primaer nach Anzahl der Starts.",
        "weights": {"playcount_norm": 1.00},
    },
    "critic_favorites": {
        "label": "Critic Favorites",
        "description": "MobyGames-Score dominiert, lokale Nutzung bricht Gleichstaende.",
        "weights": {"moby_score_100": 0.75, "playcount_norm": 0.15, "gametime_norm": 0.10},
    },
    "balanced": {
        "label": "Balanced",
        "description": "Ausgewogener Gesamtscore aus Nutzung und MobyGames.",
        "weights": {"usage_score": 0.50, "moby_score_100": 0.50},
    },
}

DEFAULT_SCHEMA = "quality_mix"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export ranked games from SQLite to CSV.")
    parser.add_argument("--db", default=str(arcade_db.DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output CSV path.")
    parser.add_argument("--top", type=int, default=250, help="Number of games to export.")
    parser.add_argument("--schema", choices=SCHEMAS.keys(), default=DEFAULT_SCHEMA, help="Ranking schema.")
    parser.add_argument("--list-schemas", action="store_true", help="List available ranking schemas and exit.")
    parser.add_argument("--include-review", action="store_true", help="Include MobyGames matches marked as review.")
    parser.add_argument("--matched-only", action="store_true", help="Only export rows with match_status = matched.")
    return parser.parse_args()


def log_normalized(value: float | int | None, max_value: float | int | None) -> float | None:
    if value is None:
        return None
    if max_value is None or max_value <= 0:
        return 0.0
    if value <= 0:
        return 0.0
    return 100 * (math.log1p(float(value)) / math.log1p(float(max_value)))


def get_export_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
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
            """
        )
    )


def score_row(row: sqlite3.Row, schema_name: str, max_playcount: int, max_gametime: int) -> float | None:
    weights = SCHEMAS[schema_name]["weights"]
    score_values = {
        "playcount_norm": log_normalized(row["playcount"], max_playcount),
        "gametime_norm": log_normalized(row["gametime"], max_gametime),
        "usage_score": row["usage_score"],
        "moby_score_100": row["moby_score_100"],
        "manual_score": row["manual_score"],
        "overall_score": row["overall_score"],
    }

    weighted_sum = 0.0
    used_weight = 0.0
    for field, weight in weights.items():
        value = score_values[field]
        if value is None:
            continue
        weighted_sum += float(value) * weight
        used_weight += weight

    if used_weight == 0:
        return None
    return round(weighted_sum / used_weight, 2)


def list_schemas() -> None:
    for name, config in SCHEMAS.items():
        weights = ", ".join(f"{field}={weight:g}" for field, weight in config["weights"].items())
        print(f"{name}: {config['label']} - {config['description']} ({weights})")


def export_rows(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = arcade_db.connect_db(db_path)
    rows = get_export_candidates(conn)
    conn.close()

    max_playcount = max((row["playcount"] or 0 for row in rows), default=0)
    max_gametime = max((row["gametime"] or 0 for row in rows), default=0)
    scored_rows = []
    for row in rows:
        status = row["moby_match_status"]
        if args.matched_only and status != "matched":
            continue
        if not args.include_review and status == "review":
            continue

        export_score = score_row(row, args.schema, max_playcount, max_gametime)
        if export_score is None:
            continue
        scored_rows.append((export_score, row))

    scored_rows.sort(
        key=lambda item: (
            item[0],
            item[1]["usage_score"] if item[1]["usage_score"] is not None else -1,
            item[1]["gametime"] if item[1]["gametime"] is not None else -1,
            item[1]["title"].casefold(),
        ),
        reverse=True,
    )
    scored_rows = scored_rows[: args.top]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "rank",
        "export_schema",
        "export_score",
        "title",
        "system",
        "playcount",
        "gametime",
        "gametime_hours",
        "usage_score",
        "moby_score_100",
        "manual_score",
        "overall_score",
        "moby_match_status",
        "moby_match_confidence",
        "moby_title",
        "moby_url",
        "batocera_id",
        "md5",
        "path",
        "game_key",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for rank, (export_score, row) in enumerate(scored_rows, start=1):
            writer.writerow(
                {
                    "rank": rank,
                    "export_schema": args.schema,
                    "export_score": export_score,
                    "title": row["title"],
                    "system": row["system"],
                    "playcount": row["playcount"],
                    "gametime": row["gametime"],
                    "gametime_hours": row["gametime_hours"],
                    "usage_score": row["usage_score"],
                    "moby_score_100": row["moby_score_100"],
                    "manual_score": row["manual_score"],
                    "overall_score": row["overall_score"],
                    "moby_match_status": row["moby_match_status"],
                    "moby_match_confidence": row["moby_match_confidence"],
                    "moby_title": row["moby_title"],
                    "moby_url": row["moby_url"],
                    "batocera_id": row["batocera_id"],
                    "md5": row["md5"],
                    "path": row["path"],
                    "game_key": row["game_key"],
                }
            )

    print(f"Schema: {args.schema} ({SCHEMAS[args.schema]['label']})")
    print(f"Wrote {len(scored_rows)} rows to {output_path}")
    return len(scored_rows)


def main() -> int:
    args = parse_args()
    if args.list_schemas:
        list_schemas()
        return 0
    export_rows(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
