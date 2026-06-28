#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scan Batocera gamelist.xml files and update the central SQLite database.

Default Batocera layout:
  script/db: /userdata/scoring
  scan root: /userdata/roms
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import shutil
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from xml.etree.ElementTree import iterparse

import arcade_db


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROMS_ROOT = Path("/userdata/roms")
DUMMY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
ONLY_DIGITS = re.compile(r"^\d+$")
NOISE_RX = re.compile(
    r"[\[(][^)\]]*[\])]|\bdisc\s*\d+\b|\brev\s*[a-z0-9]+\b|\b(ntsc|pal)\b",
    re.IGNORECASE,
)
PS3_ROOT_RX = re.compile(r"^(?P<root>.+?\.ps3)(?:[\\/].*)?$", re.IGNORECASE)
BAR_WIDTH_MIN = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan Batocera playtime into SQLite.")
    parser.add_argument("--root", default=str(DEFAULT_ROMS_ROOT), help="Batocera ROM root (default: /userdata/roms).")
    parser.add_argument("--db", default=str(arcade_db.DEFAULT_DB_PATH), help="SQLite DB path.")
    parser.add_argument("-md5", "--md5", action="store_true", help="Compute missing MD5 hashes for real files.")
    parser.add_argument("--all-visible", action="store_true", help="Import all non-hidden games, not only favorites.")
    parser.add_argument("--no-history", action="store_true", help="Do not append playtime_history rows.")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not mark existing DB games inactive before this scan. Useful for partial test scans.",
    )
    return parser.parse_args()


console_width = shutil.get_terminal_size(fallback=(100, 20)).columns


def format_bar(pct: float, width: int) -> str:
    width = max(width, BAR_WIDTH_MIN)
    fill = int(max(0.0, min(1.0, pct)) * width)
    return "[" + "#" * fill + "-" * (width - fill) + "]"


def print_line(msg: str) -> None:
    if len(msg) > console_width - 1:
        msg = msg[: console_width - 2] + "..."
    sys.stdout.write("\r" + msg)
    sys.stdout.flush()


def println(msg: str = "") -> None:
    sys.stdout.write("\n" + msg + "\n")
    sys.stdout.flush()


def is_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def safe_text(elem: ET.Element, tag: str, default: str = "") -> str:
    node = elem.find(tag)
    return (node.text if node is not None and node.text is not None else default).strip()


def safe_int(elem: ET.Element, tag: str, default: int = 0) -> int:
    try:
        return int(safe_text(elem, tag, str(default)) or default)
    except ValueError:
        return default


def find_system_name_from_path(file_path: str) -> str:
    return os.path.basename(os.path.dirname(file_path)) or "-"


def resolve_game_path(game_path: str, xml_dir: str) -> str:
    if not game_path:
        return ""
    if not os.path.isabs(game_path):
        game_path = os.path.join(xml_dir, game_path)
    path = os.path.realpath(os.path.abspath(os.path.normpath(os.path.expanduser(game_path))))
    return os.path.normcase(path)


def is_ps3_squashfs(path: str) -> bool:
    return path.lower().endswith(".ps3.squashfs")


def extract_ps3_root(path: str) -> str | None:
    match = PS3_ROOT_RX.match(path)
    return match.group("root") if match else None


def strip_invisibles(value: str) -> str:
    return "".join(ch for ch in value if unicodedata.category(ch) not in ("Cf", "Cc"))


def normalize_title_strict(title: str) -> str:
    text = unicodedata.normalize("NFKC", title)
    text = strip_invisibles(text)
    text = text.replace("™", "").replace("®", "")
    text = text.lower().strip()
    text = NOISE_RX.sub("", text)
    text = re.sub(r"[-_/]+", " ", text)
    return " ".join(text.split())


def clean_output_title(title: str) -> str:
    return strip_invisibles(title).strip()


def path_exists(path: str) -> bool:
    try:
        return bool(path) and os.path.exists(path)
    except Exception:
        return False


def has_real_id(row: dict[str, object]) -> bool:
    md5 = str(row.get("md5") or "")
    if md5:
        return md5.lower() != DUMMY_MD5
    game_id = str(row.get("id") or "")
    return bool(game_id and game_id != "-")


def better_row(existing: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    existing_has_id = has_real_id(existing)
    candidate_has_id = has_real_id(candidate)
    if existing_has_id != candidate_has_id:
        return existing if existing_has_id else candidate

    existing_exists = path_exists(str(existing.get("path") or ""))
    candidate_exists = path_exists(str(candidate.get("path") or ""))
    if existing_exists != candidate_exists:
        return existing if existing_exists else candidate

    if int(candidate.get("gametime") or 0) > int(existing.get("gametime") or 0):
        return candidate
    if int(candidate.get("playcount") or 0) > int(existing.get("playcount") or 0):
        return candidate
    return existing


def file_md5(path: str, chunk_size: int = 4 * 1024 * 1024) -> str:
    try:
        if not os.path.isfile(path):
            return ""
        digest = hashlib.md5()
        with open(path, "rb") as file:
            for chunk in iter(lambda: file.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return ""


def find_gamelists(root_path: Path) -> list[str]:
    seen_dirs: set[str] = set()
    seen_gamelist_real: set[str] = set()
    gamelists: list[str] = []

    for root, dirnames, filenames in os.walk(root_path, followlinks=True):
        for directory in list(dirnames):
            real = os.path.realpath(os.path.join(root, directory))
            if real in seen_dirs:
                dirnames.remove(directory)
            else:
                seen_dirs.add(real)

        for filename in fnmatch.filter(filenames, "gamelist.xml"):
            path = os.path.join(root, filename)
            real_path = os.path.realpath(path)
            if real_path in seen_gamelist_real:
                continue
            seen_gamelist_real.add(real_path)
            gamelists.append(path)

    return gamelists


def count_candidates(xml_path: str, all_visible: bool) -> int:
    count = 0
    try:
        for _, elem in iterparse(xml_path, events=("end",)):
            if elem.tag != "game":
                continue
            if is_true(safe_text(elem, "hidden", "")):
                elem.clear()
                continue
            if not all_visible and not is_true(safe_text(elem, "favorite", "")):
                elem.clear()
                continue
            count += 1
            elem.clear()
    except ET.ParseError:
        return 0
    return count


def progress_line(processed_in_file: int, candidates: int, kept: int, duplicates: int, md5_count: int) -> str:
    pct_file = (processed_in_file / candidates) if candidates else 0.0
    bar = format_bar(pct_file, 30)
    return f"   {bar} {processed_in_file}/{candidates} | kept:{kept} dup:{duplicates} md5+:{md5_count}"


def scan_entries(root_path: Path, *, all_visible: bool, md5_enabled: bool) -> tuple[dict[tuple[object, ...], dict[str, object]], dict[str, object]]:
    println("Searching gamelist.xml files...")
    gamelists = find_gamelists(root_path)
    println(f"Found {len(gamelists)} unique gamelist.xml files")

    entries_by_key: dict[tuple[object, ...], dict[str, object]] = {}
    duplicate_count = 0
    key_strategy_counts = {"MD5": 0, "ID": 0, "PATH": 0, "TITLE": 0}
    stats = {
        "hidden": 0,
        "not_favorite": 0,
        "missing_file": 0,
        "kept": 0,
        "md5_computed": 0,
        "md5_compute_failed": 0,
        "duplicates": 0,
        "gamelists": len(gamelists),
    }

    for file_index, file_path in enumerate(gamelists, start=1):
        system_name = find_system_name_from_path(file_path)
        println(f"[{file_index}/{len(gamelists)}] {file_path} (system: {system_name})")
        start = time.time()
        candidates = count_candidates(file_path, all_visible)
        println(f"   candidates: {candidates}")
        processed_in_file = 0
        kept_in_file = 0

        try:
            for _, game in iterparse(file_path, events=("end",)):
                if game.tag != "game":
                    continue

                processed_in_file += 1
                gid = (game.attrib.get("id", "-") or "-").strip()
                raw_title = safe_text(game, "name", "") or safe_text(game, "path", "")
                if not raw_title:
                    game.clear()
                    continue

                hidden = is_true(safe_text(game, "hidden", ""))
                favorite = is_true(safe_text(game, "favorite", ""))
                if hidden:
                    stats["hidden"] += 1
                    game.clear()
                    continue
                if not all_visible and not favorite:
                    stats["not_favorite"] += 1
                    game.clear()
                    continue

                md5 = safe_text(game, "md5", "") or ""
                if md5.lower() == DUMMY_MD5:
                    md5 = ""

                playcount = safe_int(game, "playcount", 0)
                gametime = safe_int(game, "gametime", 0)
                game_path = safe_text(game, "path", "")
                xml_dir = os.path.dirname(file_path)
                real_target = resolve_game_path(game_path, xml_dir)

                if is_ps3_squashfs(real_target):
                    effective_path = real_target
                else:
                    ps3_root = extract_ps3_root(real_target)
                    effective_path = ps3_root if ps3_root else real_target

                if not path_exists(effective_path):
                    stats["missing_file"] += 1
                    game.clear()
                    print_line(progress_line(processed_in_file, candidates, stats["kept"], duplicate_count, stats["md5_computed"]))
                    continue

                if md5_enabled and not md5 and os.path.isfile(effective_path):
                    computed = file_md5(effective_path)
                    if computed:
                        md5 = computed
                        stats["md5_computed"] += 1
                    else:
                        stats["md5_compute_failed"] += 1

                norm_title = normalize_title_strict(raw_title)

                if md5:
                    primary_key = (system_name, "MD5", md5)
                    key_strategy_counts["MD5"] += 1
                elif gid and gid != "-":
                    primary_key = (system_name, "ID", gid)
                    key_strategy_counts["ID"] += 1
                elif effective_path:
                    primary_key = (system_name, "PATH", effective_path)
                    key_strategy_counts["PATH"] += 1
                else:
                    primary_key = (system_name, "TITLE", norm_title)
                    key_strategy_counts["TITLE"] += 1

                row = {
                    "title": raw_title,
                    "system": system_name,
                    "id": gid,
                    "md5": md5,
                    "path": effective_path,
                    "norm_title": norm_title,
                    "playcount": playcount,
                    "gametime": gametime,
                    "favorite": favorite or all_visible,
                    "hidden": hidden,
                    "file_exists": True,
                }

                if primary_key in entries_by_key:
                    chosen = better_row(entries_by_key[primary_key], row)
                    if chosen is not entries_by_key[primary_key]:
                        entries_by_key[primary_key] = chosen
                    else:
                        duplicate_count += 1
                else:
                    entries_by_key[primary_key] = row
                    stats["kept"] += 1
                    kept_in_file += 1

                print_line(progress_line(processed_in_file, candidates, stats["kept"], duplicate_count, stats["md5_computed"]))
                game.clear()

        except ET.ParseError as error:
            println(f"\n[WARN] Skipping broken XML: {file_path} ({error})")
            continue

        elapsed = time.time() - start
        println(f"   done: kept {kept_in_file} of {candidates} ({elapsed:.2f}s)")

    stats["duplicates"] = duplicate_count
    stats["key_strategy_counts"] = key_strategy_counts
    return entries_by_key, stats


def second_pass_dedupe(entries_by_key: dict[tuple[object, ...], dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    by_title_group: dict[tuple[object, ...], dict[str, object]] = {}
    duplicate_count = 0

    for key, row in entries_by_key.items():
        if has_real_id(row):
            by_title_group[key] = row
            continue

        group_key = ("NO_ID", row["system"], row["norm_title"])
        if group_key in by_title_group:
            chosen = better_row(by_title_group[group_key], row)
            if chosen is not by_title_group[group_key]:
                by_title_group[group_key] = chosen
            else:
                duplicate_count += 1
        else:
            by_title_group[group_key] = row

    rows = list(by_title_group.values())
    rows.sort(
        key=lambda item: (
            int(item.get("gametime") or 0),
            int(item.get("playcount") or 0),
            str(item.get("title") or "").casefold(),
        ),
        reverse=True,
    )
    return rows, duplicate_count


def write_database(
    db_path: Path,
    rows: list[dict[str, object]],
    *,
    reset_existing: bool,
    write_history: bool,
) -> Counter[str]:
    conn = arcade_db.connect_db(db_path)
    seen_at = arcade_db.now_ts()
    system_counter: Counter[str] = Counter()

    try:
        if reset_existing:
            conn.execute("UPDATE games SET favorite = 0, file_exists = 0, last_seen_at = ?", (seen_at,))

        for row in rows:
            title = clean_output_title(str(row.get("title") or ""))
            system = str(row.get("system") or "-")
            batocera_id = str(row.get("id") or "")
            md5 = str(row.get("md5") or "")
            path = str(row.get("path") or "")
            norm_title = str(row.get("norm_title") or normalize_title_strict(title))
            game_key = arcade_db.build_game_key(
                system=system,
                md5=md5,
                batocera_id=batocera_id,
                path=path,
                norm_title=norm_title,
            )

            arcade_db.upsert_game(
                conn,
                game_key=game_key,
                title=title,
                norm_title=norm_title,
                system=system,
                batocera_id=batocera_id,
                md5=md5,
                path=path,
                favorite=bool(row.get("favorite", True)),
                hidden=bool(row.get("hidden", False)),
                file_exists=bool(row.get("file_exists", True)),
                seen_at=seen_at,
            )
            arcade_db.upsert_playtime(
                conn,
                game_key=game_key,
                playcount=int(row.get("playcount") or 0),
                gametime=int(row.get("gametime") or 0),
                updated_at=seen_at,
                write_history=write_history,
            )
            system_counter[system] += 1

        arcade_db.update_usage_scores(conn)
        arcade_db.update_overall_scores(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return system_counter


def main() -> int:
    args = parse_args()
    root_path = Path(args.root).expanduser().resolve()
    db_path = Path(args.db).expanduser().resolve()

    println(f"MD5 computation: {'enabled' if args.md5 else 'disabled'}")
    println(f"Scan root: {root_path}")
    println(f"Database: {db_path}")
    println(f"Mode: {'all visible games' if args.all_visible else 'favorites only'}")

    if not root_path.exists():
        println(f"[ERROR] Scan root does not exist: {root_path}")
        return 1

    entries_by_key, stats = scan_entries(root_path, all_visible=args.all_visible, md5_enabled=args.md5)
    rows, second_pass_duplicates = second_pass_dedupe(entries_by_key)
    system_counter = write_database(
        db_path,
        rows,
        reset_existing=not args.no_reset,
        write_history=not args.no_history,
    )

    println("")
    println(f"Imported/updated games: {len(rows)}")
    println(f"Second-pass duplicates merged: {second_pass_duplicates}")
    println(f"Database updated: {db_path}")
    println(f"Systems: {dict(sorted(system_counter.items(), key=lambda item: item[0].casefold()))}")
    println(f"Scan stats: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
