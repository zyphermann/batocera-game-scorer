#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import fnmatch
import time
import csv
import re
import sys
import shutil
import unicodedata
import hashlib
import argparse
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import iterparse
from collections import Counter
from pathlib import Path

# ============== CLI-Argumente ==============
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ROMS_ROOT = Path("/userdata/roms")
DEFAULT_OUTPUT_DIR = SCRIPT_DIR

ap = argparse.ArgumentParser(description="Batocera gamelist Favoriten scannen und CSV exportieren")
ap.add_argument("-md5", "--md5", action="store_true",
                help="fehlende MD5-Hashes für Dateien berechnen (langsamer)")
ap.add_argument("--root", default=str(DEFAULT_ROMS_ROOT),
                help="Root-Ordner der Batocera-ROMs/gamelist.xml Dateien (Default: /userdata/roms)")
ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                help="Ordner fuer CSV-Export (Default: Ordner dieses Skripts, z.B. /userdata/scoring)")
args = ap.parse_args()
MD5_ENABLED = bool(args.md5)
ROMS_ROOT = Path(args.root).expanduser().resolve()
OUTPUT_DIR = Path(args.output_dir).expanduser().resolve()

# ============== Status/UI ==============
BAR_WIDTH_MIN = 20
console_width = shutil.get_terminal_size(fallback=(100, 20)).columns

def format_bar(pct: float, width: int) -> str:
    width = max(width, BAR_WIDTH_MIN)
    fill = int(max(0.0, min(1.0, pct)) * width)
    return "[" + "#" * fill + "-" * (width - fill) + "]"

def print_line(msg: str):
    if len(msg) > console_width - 1:
        msg = msg[:console_width - 2] + "…"
    sys.stdout.write("\r" + msg)
    sys.stdout.flush()

def println(msg: str = ""):
    sys.stdout.write("\n" + msg + "\n")
    sys.stdout.flush()

# ============== Konstante / Heuristiken ==============
DUMMY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"
ONLY_DIGITS = re.compile(r"^\d+$")

NOISE_RX = re.compile(
    r"[\[(][^)\]]*[\])]|\bdisc\s*\d+\b|\brev\s*[a-z0-9]+\b|\b(ntsc|pal)\b",
    re.IGNORECASE,
)

def is_true(v):
    return (v or "").strip().lower() == "true"

def safe_text(elem, tag, default=""):
    node = elem.find(tag)
    return (node.text if node is not None and node.text is not None else default).strip()

def safe_int(elem, tag, default=0):
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
    p = os.path.realpath(os.path.abspath(os.path.normpath(os.path.expanduser(game_path))))
    return os.path.normcase(p)

def is_ps3_squashfs(path: str) -> bool:
    return path.lower().endswith(".ps3.squashfs")

PS3_ROOT_RX = re.compile(r"^(?P<root>.+?\.ps3)(?:[\\/].*)?$", re.IGNORECASE)

def extract_ps3_root(path: str):
    m = PS3_ROOT_RX.match(path)
    return m.group("root") if m else None

def strip_invisibles(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch) not in ("Cf", "Cc"))

def normalize_title_strict(title: str) -> str:
    t = unicodedata.normalize("NFKC", title)
    t = strip_invisibles(t)
    t = t.replace("™", "").replace("®", "")
    t = t.lower().strip()
    t = NOISE_RX.sub("", t)
    t = re.sub(r"[-_/]+", " ", t)
    return " ".join(t.split())

def clean_output_title(title: str) -> str:
    return strip_invisibles(title).strip()

def protect_for_numbers(title: str) -> str:
    t = title.strip()
    if ONLY_DIGITS.match(t):
        if len(title) >= 2:
            return title[0] + "\u200A" + title[1:]
        else:
            return title + "\u200A"
    return title

def path_exists(p: str) -> bool:
    try:
        return bool(p) and os.path.exists(p)
    except Exception:
        return False

def has_real_id(row) -> bool:
    if row.get("md5"):
        if row["md5"].lower() == DUMMY_MD5:
            return False
        return True
    return bool(row.get("id") and row["id"] != "-")

def better_row(existing, candidate):
    ex_has = has_real_id(existing)
    ca_has = has_real_id(candidate)
    if ex_has != ca_has:
        return existing if ex_has else candidate

    ex_exists = path_exists(existing.get("path", ""))
    ca_exists = path_exists(candidate.get("path", ""))
    if ex_exists != ca_exists:
        return existing if ex_exists else candidate

    # Bei Duplikaten Eintrag mit mehr Spielzeit bevorzugen
    if candidate.get("gametime", 0) > existing.get("gametime", 0):
        return candidate
    if candidate.get("playcount", 0) > existing.get("playcount", 0):
        return candidate

    return existing

def file_md5(path: str, chunk_size: int = 4 * 1024 * 1024) -> str:
    try:
        if not os.path.isfile(path):
            return ""
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

println(f"⚙️ MD5-Berechnung: {'AKTIV' if MD5_ENABLED else 'deaktiviert'}")
println(f"📁 Scan-Root: {ROMS_ROOT}")
println(f"📁 Output-Ordner: {OUTPUT_DIR}")

# ============== gamelist.xml finden ==============
println("🔎 Suche gamelist.xml …")
seen_dirs = set()
gamelists = []
seen_gamelist_real = set()
duplicates_skipped = []

for root, dirnames, filenames in os.walk(ROMS_ROOT, followlinks=True):
    for d in list(dirnames):
        real = os.path.realpath(os.path.join(root, d))
        if real in seen_dirs:
            dirnames.remove(d)
        else:
            seen_dirs.add(real)

    for filename in fnmatch.filter(filenames, "gamelist.xml"):
        p = os.path.join(root, filename)
        real_p = os.path.realpath(p)
        if real_p in seen_gamelist_real:
            duplicates_skipped.append((p, real_p))
            continue
        seen_gamelist_real.add(real_p)
        gamelists.append(p)

println(f"✅ Gefunden: {len(gamelists)} eindeutige gamelist.xml")
println("")

# ============== Sammelstrukturen ==============
entries_by_key = {}
dup_count = 0
key_strategy_counts = {"MD5": 0, "ID": 0, "PATH": 0, "TITLE": 0}
stats = {
    "hidden": 0,
    "not_favorite": 0,
    "missing_file": 0,
    "kept": 0,
    "md5_computed": 0,
    "md5_compute_failed": 0,
}
total_processed = 0

def count_candidates(xml_path: str) -> int:
    count = 0
    try:
        for _, elem in iterparse(xml_path, events=("end",)):
            if elem.tag != "game":
                continue
            if is_true(safe_text(elem, "hidden", "")):
                elem.clear()
                continue
            if not is_true(safe_text(elem, "favorite", "")):
                elem.clear()
                continue
            count += 1
            elem.clear()
    except ET.ParseError:
        return 0
    return count

def progress_line(proc_in_file, cand, kept, dup, md5_plus):
    pct_file = (proc_in_file / cand) if cand else 0.0
    bar = format_bar(pct_file, 30)
    return f"   {bar} {proc_in_file}/{cand} | kept:{kept} dup:{dup} md5+:{md5_plus}"

# ============== Verarbeitung ==============
for fi, file_path in enumerate(gamelists, start=1):
    system_name = find_system_name_from_path(file_path)
    println(f"📄 [{fi}/{len(gamelists)}] {file_path}  (System: {system_name})")
    t0 = time.time()

    cand = count_candidates(file_path)
    println(f"   → Kandidaten in dieser Datei: {cand} (favorite & nicht hidden). Starte Verarbeitung …")

    processed_in_file = 0
    kept_in_file = 0

    try:
        for _, game in iterparse(file_path, events=("end",)):
            if game.tag != "game":
                continue

            processed_in_file += 1
            total_processed += 1

            gid = (game.attrib.get("id", "-") or "-").strip()
            raw_title = safe_text(game, "name", "") or safe_text(game, "path", "")
            if not raw_title:
                game.clear()
                continue

            if is_true(safe_text(game, "hidden", "")):
                stats["hidden"] += 1
                game.clear()
                continue

            if not is_true(safe_text(game, "favorite", "")):
                stats["not_favorite"] += 1
                game.clear()
                continue

            md5 = safe_text(game, "md5", "") or ""
            if md5.lower() == DUMMY_MD5:
                md5 = ""

            playcount = safe_int(game, "playcount", 0)
            gametime = safe_int(game, "gametime", 0)

            gpath = safe_text(game, "path", "")
            xml_dir = os.path.dirname(file_path)
            real_target = resolve_game_path(gpath, xml_dir)

            if is_ps3_squashfs(real_target):
                effective_path = real_target
            else:
                ps3_root = extract_ps3_root(real_target)
                effective_path = ps3_root if ps3_root else real_target

            if not path_exists(effective_path):
                stats["missing_file"] += 1
                game.clear()
                print_line(progress_line(processed_in_file, cand, stats["kept"], dup_count, stats["md5_computed"]))
                continue

            if MD5_ENABLED and (not md5) and os.path.isfile(effective_path):
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
            }

            if primary_key in entries_by_key:
                chosen = better_row(entries_by_key[primary_key], row)
                if chosen is not entries_by_key[primary_key]:
                    entries_by_key[primary_key] = chosen
                else:
                    dup_count += 1
            else:
                entries_by_key[primary_key] = row
                stats["kept"] += 1
                kept_in_file += 1

            print_line(progress_line(processed_in_file, cand, stats["kept"], dup_count, stats["md5_computed"]))
            game.clear()

    except ET.ParseError as e:
        println(f"\n[WARN] Überspringe defektes XML: {file_path} ({e})")
        continue

    dt = time.time() - t0
    println(f"   ✓ Done: kept {kept_in_file} aus {cand}  ({dt:.2f}s)")

# ============== Zweite Dedupe-Runde ==============
by_title_group = {}
for key, r in entries_by_key.items():
    if has_real_id(r):
        by_title_group[key] = r
    else:
        gkey = ("NO_ID", r["system"], r["norm_title"])
        if gkey in by_title_group:
            chosen = better_row(by_title_group[gkey], r)
            if chosen is not by_title_group[gkey]:
                by_title_group[gkey] = chosen
            else:
                dup_count += 1
        else:
            by_title_group[gkey] = r

# ============== CSV schreiben ==============
rows = []
system_counter = Counter()

for r in by_title_group.values():
    out_title = clean_output_title(r["title"])
    gametime = r.get("gametime", 0)
    gametime_hours = round(gametime / 3600, 2)

    rows.append((
        out_title,
        r["system"],
        r["id"],
        r["md5"],
        r.get("playcount", 0),
        gametime,
        gametime_hours,
        0,  # ranking 0..100
    ))
    system_counter[r["system"]] += 1

# Sortierung: höchste Spielzeit zuerst, dann Playcount, dann Titel
rows.sort(key=lambda x: (x[5], x[4], x[0].casefold()), reverse=True)

rows_with_index = [(i, *row) for i, row in enumerate(rows)]

timestamp = int(time.time())
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
csv_path = OUTPUT_DIR / f"{timestamp}.csv"

with open(csv_path, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow([
        "index",
        "title",
        "system",
        "id",
        "md5",
        "playcount",
        "gametime",
        "gametime_hours",
         "ranking",
        "stats_system",
        "stats_total",
    ])

    for rec in rows_with_index:
        i, title, system, gid, md5, playcount, gametime, gametime_hours, ranking = rec
        safe_title = protect_for_numbers(title)
        w.writerow([
            i,
            safe_title,
            system,
            gid,
            md5,
            playcount,
            gametime,
            gametime_hours,
            ranking,
            "",
            "",
        ])

    for system in sorted(system_counter.keys(), key=str.casefold):
        w.writerow(["", "", "", "", "", "", "", "", "", system, system_counter[system]])

println(f"📄 Exportiert {len(rows_with_index)} eindeutige Favoriten nach {csv_path}")
println(f"♻️  Duplikate übersprungen/zusammengeführt: {dup_count}")
println(f"🔑 Key-Strategie: {key_strategy_counts}")
println(f"🧮 Filter-Stats: {stats}")
