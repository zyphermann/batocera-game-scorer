#!/usr/bin/env python3
"""
Enrich the central SQLite database with MobyGames API matches and scores.

Usage:
  python3 enrich_mobygames.py --limit 250
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import arcade_db


API_BASE_URL = "https://api.mobygames.com/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_TOKEN_PATH = SCRIPT_DIR / "mobygames_api_key.txt"

SYSTEM_TO_PLATFORM = {
    "3do": "3DO",
    "3ds": "Nintendo 3DS",
    "amiga": "Amiga",
    "amiga1200": "Amiga",
    "arcade": "Arcade",
    "atari2600": "Atari 2600",
    "atari5200": "Atari 5200",
    "atari7800": "Atari 7800",
    "atarilynx": "Lynx",
    "c64": "Commodore 64",
    "dreamcast": "Dreamcast",
    "dos": "DOS",
    "ds": "Nintendo DS",
    "fbneo": "Arcade",
    "fds": "Famicom Disk System",
    "gb": "Game Boy",
    "gba": "Game Boy Advance",
    "gbc": "Game Boy Color",
    "gamecube": "GameCube",
    "gamegear": "Game Gear",
    "genesis": "Genesis",
    "jaguar": "Jaguar",
    "mame": "Arcade",
    "mastersystem": "SEGA Master System",
    "megadrive": "Genesis",
    "msx": "MSX",
    "n64": "Nintendo 64",
    "nds": "Nintendo DS",
    "neogeo": "Neo Geo",
    "nes": "NES",
    "ngpc": "Neo Geo Pocket Color",
    "pcengine": "TurboGrafx-16",
    "ps2": "PlayStation 2",
    "ps3": "PlayStation 3",
    "ps4": "PlayStation 4",
    "psp": "PSP",
    "psvita": "PS Vita",
    "psx": "PlayStation",
    "saturn": "SEGA Saturn",
    "sega32x": "SEGA 32X",
    "segacd": "SEGA CD",
    "snes": "SNES",
    "switch": "Nintendo Switch",
    "wii": "Wii",
    "wiiu": "Wii U",
    "windows": "Windows",
    "xbox": "Xbox",
    "xbox360": "Xbox 360",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enrich SQLite games with MobyGames scores.")
    parser.add_argument("--db", default=str(arcade_db.DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("--api-key", default=None, help="MobyGames API key. Prefer token file or MOBYGAMES_API_KEY.")
    parser.add_argument("--token-file", default=str(DEFAULT_TOKEN_PATH), help="File containing the MobyGames API key.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum not-yet-enriched games to process.")
    parser.add_argument("--max-requests", type=int, default=None, help="Stop after this many uncached API requests.")
    parser.add_argument("--sleep", type=float, default=1.05, help="Seconds between uncached API requests.")
    parser.add_argument("--min-usage-score", type=float, default=None, help="Only enrich rows with usage_score >= this value.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned queries without calling the API.")
    parser.add_argument("--title-variants", type=int, default=3, help="Maximum title variants to try per game.")
    parser.add_argument("--redo", action="store_true", help="Reprocess games even if they already have a MobyGames status.")
    return parser.parse_args()


def load_api_key(cli_api_key: str | None, token_file: Path) -> str:
    if cli_api_key:
        return cli_api_key.strip()

    env_key = os.environ.get("MOBYGAMES_API_KEY", "").strip()
    if env_key:
        return env_key

    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()

    return ""


class MobyClient:
    def __init__(self, api_key: str, conn: sqlite3.Connection, sleep_seconds: float, max_requests: int | None) -> None:
        self.api_key = api_key
        self.conn = conn
        self.sleep_seconds = sleep_seconds
        self.max_requests = max_requests
        self.uncached_requests = 0
        self.last_request_at = 0.0

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.max_requests is not None and self.uncached_requests >= self.max_requests:
            raise RuntimeError(f"Reached --max-requests={self.max_requests}")

        full_params = {**params, "api_key": self.api_key}
        query = urllib.parse.urlencode(full_params, doseq=True)
        url = f"{API_BASE_URL}{path}?{query}"
        cache_key = f"{path}?{urllib.parse.urlencode(params, doseq=True)}"

        cached = arcade_db.cache_get(self.conn, cache_key)
        if cached:
            status, response = cached
            if status >= 400:
                raise RuntimeError(f"Cached API error {status}: {response}")
            return response

        elapsed = time.time() - self.last_request_at
        if self.last_request_at and elapsed < self.sleep_seconds:
            time.sleep(self.sleep_seconds - elapsed)

        req = urllib.request.Request(url, headers={"User-Agent": "perfect-arcade-games-mobygames-enricher/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                status = response.status
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            status = error.code
            body = error.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"error": body}
            arcade_db.cache_put(self.conn, cache_key, url, status, payload)
            self.conn.commit()
            raise RuntimeError(f"MobyGames API error {status}: {payload}") from error

        self.last_request_at = time.time()
        self.uncached_requests += 1
        arcade_db.cache_put(self.conn, cache_key, url, status, payload)
        self.conn.commit()
        return payload


def normalize_title(title: str) -> str:
    text = title.casefold()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\[[^]]*\]", " ", text)
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def title_variants(title: str, max_variants: int) -> list[str]:
    cleaned = re.sub(r"\s+\((us|usa|eu|europe|jp|japan|world|rev[^)]*)\)\s*$", "", title, flags=re.IGNORECASE)
    variants = [cleaned.strip()]

    for part in re.split(r"\s+/\s+", cleaned):
        part = part.strip()
        # Slash-separated library labels often start with a broad franchise prefix
        # such as "Mario / Super Mario RPG"; short fragments are poor API queries.
        if part and part not in variants and (len(part) >= 6 or " " in part):
            variants.append(part)

    without_subtitle = re.split(r"\s+[-:]\s+", cleaned, maxsplit=1)[0].strip()
    if without_subtitle and without_subtitle not in variants:
        variants.append(without_subtitle)

    return variants[:max_variants]


def platform_lookup(platforms_payload: dict[str, Any]) -> dict[str, int]:
    return {
        platform["platform_name"].casefold(): int(platform["platform_id"])
        for platform in platforms_payload.get("platforms", [])
        if platform.get("platform_name") and platform.get("platform_id") is not None
    }


def platform_id_for_system(system: str, lookup: dict[str, int]) -> tuple[int | None, str | None]:
    platform_name = SYSTEM_TO_PLATFORM.get(system.casefold())
    if not platform_name:
        return None, None

    direct = lookup.get(platform_name.casefold())
    if direct is not None:
        return direct, platform_name

    match = difflib.get_close_matches(platform_name.casefold(), lookup.keys(), n=1, cutoff=0.88)
    if not match:
        return None, platform_name
    return lookup[match[0]], platform_name


def game_platform_names(game: dict[str, Any]) -> list[str]:
    return [
        platform.get("platform_name", "")
        for platform in game.get("platforms", [])
        if platform.get("platform_name")
    ]


def score_match(source_title: str, source_system: str, candidate: dict[str, Any], wanted_platform_name: str | None) -> float:
    source_norm = normalize_title(source_title)
    candidate_norm = normalize_title(candidate.get("title", ""))
    title_ratio = difflib.SequenceMatcher(None, source_norm, candidate_norm).ratio()

    alternate_ratios = [
        difflib.SequenceMatcher(None, source_norm, normalize_title(item.get("title", ""))).ratio()
        for item in candidate.get("alternate_titles", [])
        if item.get("title")
    ]
    title_score = max([title_ratio, *alternate_ratios] or [0])

    platform_score = 0.0
    if wanted_platform_name:
        platform_names = [name.casefold() for name in game_platform_names(candidate)]
        platform_score = 1.0 if wanted_platform_name.casefold() in platform_names else 0.0
    elif source_system:
        platform_score = 0.25

    return round((0.82 * title_score + 0.18 * platform_score) * 100, 2)


def best_match(source_title: str, source_system: str, games: list[dict[str, Any]], wanted_platform_name: str | None) -> dict[str, Any] | None:
    if not games:
        return None
    scored = [
        (score_match(source_title, source_system, game, wanted_platform_name), game)
        for game in games
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    confidence, game = scored[0]
    return {"confidence": confidence, "game": game}


def enrich_game(row: sqlite3.Row, client: MobyClient, platform_lookup_by_name: dict[str, int], max_title_variants: int) -> dict[str, Any]:
    platform_id, wanted_platform_name = platform_id_for_system(row["system"] or "", platform_lookup_by_name)
    params_base: dict[str, Any] = {"format": "normal", "limit": 10}
    if platform_id is not None:
        params_base["platform"] = platform_id

    searched_titles = []
    best: dict[str, Any] | None = None
    for candidate_title in title_variants(row["title"], max_title_variants):
        searched_titles.append(candidate_title)
        payload = client.get("/games", {**params_base, "title": candidate_title})
        current = best_match(candidate_title, row["system"] or "", payload.get("games", []), wanted_platform_name)
        if current and (best is None or current["confidence"] > best["confidence"]):
            best = current
        if best and best["confidence"] >= 92:
            break

    output: dict[str, Any] = {
        "game_key": row["game_key"],
        "query_titles": searched_titles,
        "platform_query": wanted_platform_name or "",
        "match_status": "no_match",
        "match_confidence": None,
        "moby_game_id": None,
        "moby_title": None,
        "moby_platforms": None,
        "moby_score": None,
        "moby_num_votes": None,
        "moby_url": None,
    }

    if not best:
        return output

    game = best["game"]
    confidence = best["confidence"]
    score = game.get("moby_score")
    output["match_confidence"] = confidence
    output["match_status"] = "matched" if confidence >= 86 else "review"
    output["moby_game_id"] = game.get("game_id")
    output["moby_title"] = game.get("title")
    output["moby_platforms"] = game_platform_names(game)
    output["moby_score"] = None if score is None else float(score)
    output["moby_num_votes"] = game.get("num_votes")
    output["moby_url"] = game.get("moby_url")
    return output


def select_games_for_enrichment(
    conn: sqlite3.Connection,
    *,
    limit: int | None,
    min_usage_score: float | None,
    redo: bool,
) -> list[sqlite3.Row]:
    where = [
        "g.favorite = 1",
        "g.hidden = 0",
        "g.file_exists = 1",
    ]
    params: list[Any] = []

    if not redo:
        where.append("m.game_key IS NULL")
    if min_usage_score is not None:
        where.append("COALESCE(s.usage_score, 0) >= ?")
        params.append(min_usage_score)

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT ?"
        params.append(limit)

    return list(
        conn.execute(
            f"""
            SELECT
              g.game_key,
              g.title,
              g.system,
              s.usage_score,
              p.playcount,
              p.gametime_hours,
              m.match_status
            FROM games g
            LEFT JOIN playtime p ON p.game_key = g.game_key
            LEFT JOIN scores s ON s.game_key = g.game_key
            LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
            WHERE {" AND ".join(where)}
            ORDER BY
              s.usage_score IS NULL,
              s.usage_score DESC,
              p.gametime IS NULL,
              p.gametime DESC,
              g.title COLLATE NOCASE ASC
            {limit_sql}
            """,
            params,
        )
    )


def main() -> int:
    args = parse_args()
    db_path = Path(args.db).expanduser().resolve()
    token_file = Path(args.token_file).expanduser().resolve()

    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        print("Run scan_batocera_playtime.py first.", file=sys.stderr)
        return 1

    conn = arcade_db.connect_db(db_path)
    process_rows = select_games_for_enrichment(
        conn,
        limit=args.limit,
        min_usage_score=args.min_usage_score,
        redo=args.redo,
    )

    if args.dry_run:
        total_games = conn.execute(
            "SELECT COUNT(*) FROM games WHERE favorite = 1 AND hidden = 0 AND file_exists = 1"
        ).fetchone()[0]
        enriched_games = conn.execute("SELECT COUNT(*) FROM mobygames_matches").fetchone()[0]
        print(f"Database: {db_path}")
        print(f"Favorite visible games: {total_games}")
        print(f"Existing MobyGames rows: {enriched_games}")
        print(f"Rows selected for enrichment: {len(process_rows)}")
        for row in process_rows[:20]:
            print(
                f"{row['title']} [{row['system']}] "
                f"usage={row['usage_score']} -> "
                f"{title_variants(row['title'], args.title_variants)}"
            )
        conn.close()
        return 0

    api_key = load_api_key(args.api_key, token_file)
    if not api_key:
        print("Missing API key.", file=sys.stderr)
        print(f"Create {token_file} with the key, set MOBYGAMES_API_KEY, or pass --api-key.", file=sys.stderr)
        conn.close()
        return 1

    client = MobyClient(api_key, conn, args.sleep, args.max_requests)
    processed = 0
    try:
        platforms_payload = client.get("/platforms", {})
        platforms = platform_lookup(platforms_payload)

        for position, row in enumerate(process_rows, start=1):
            try:
                enriched = enrich_game(row, client, platforms, args.title_variants)
            except RuntimeError as error:
                print(f"Stopped at {row['title']} [{row['system']}]: {error}", file=sys.stderr)
                break

            arcade_db.upsert_mobygames_match(
                conn,
                game_key=enriched["game_key"],
                match_status=enriched["match_status"],
                moby_game_id=enriched["moby_game_id"],
                moby_title=enriched["moby_title"],
                moby_platforms=enriched["moby_platforms"],
                moby_url=enriched["moby_url"],
                moby_score=enriched["moby_score"],
                moby_num_votes=enriched["moby_num_votes"],
                match_confidence=enriched["match_confidence"],
                query_titles=enriched["query_titles"],
                platform_query=enriched["platform_query"],
            )
            arcade_db.update_overall_scores(conn)
            conn.commit()
            processed += 1
            confidence_text = "" if enriched["match_confidence"] is None else f"{enriched['match_confidence']:.2f}"

            print(
                f"{position}/{len(process_rows)} "
                f"{row['title']} [{row['system']}] -> "
                f"{enriched['match_status']} "
                f"{enriched['moby_title'] or ''} "
                f"({confidence_text})"
            )
    finally:
        conn.close()

    print(f"Database updated: {db_path}")
    print(f"Newly enriched games this run: {processed}")
    print(f"Uncached API requests this run: {client.uncached_requests}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
