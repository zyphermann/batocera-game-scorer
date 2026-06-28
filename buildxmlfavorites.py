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

# ============== CLI-Argumente ==============
ap = argparse.ArgumentParser(description="Batocera gamelist Favoriten scannen und CSV exportieren")
ap.add_argument("-md5", "--md5", action="store_true",
                help="fehlende MD5-Hashes für Dateien berechnen (langsamer)")
args = ap.parse_args()
MD5_ENABLED = bool(args.md5)

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

ONLY_DIGITS = re.compile(r"^\d+$")  # für Numbers/Excel-Schutz

# Titel, die wir zum Dedupe normalisieren (Noise entfernen)
NOISE_RX = re.compile(
    r"[\[(][^)\]]*[\])]|\bdisc\s*\d+\b|\brev\s*[a-z0-9]+\b|\b(ntsc|pal)\b",
    re.IGNORECASE,
)

def is_true(v): return (v or "").strip().lower() == "true"

def safe_text(elem, tag, default=""):
    node = elem.find(tag)
    return (node.text if node is not None and node.text is not None else default).strip()

def find_system_name_from_path(file_path: str) -> str:
    return os.path.basename(os.path.dirname(file_path)) or "-"

def resolve_game_path(game_path: str, xml_dir: str) -> str:
    if not game_path:
        return ""
    if not os.path.isabs(game_path):
        game_path = os.path.join(xml_dir, game_path)
    p = os.path.realpath(os.path.abspath(os.path.normpath(os.path.expanduser(game_path))))
    p = os.path.normcase(p)
    return p

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
    """
    Wenn der gesamte Titel nur aus Ziffern besteht (z.B. '005', '1942'),
    mit führendem Apostroph schreiben, damit Numbers/Excel Text erzwingen.
    """
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
    # 1) bevorzugt echten Identifikator (md5 != Dummy oder echte id)
    ex_has = has_real_id(existing)
    ca_has = has_real_id(candidate)
    if ex_has != ca_has:
        return existing if ex_has else candidate
    # 2) bevorzugt existierenden Pfad
    ex_exists = path_exists(existing.get("path", ""))
    ca_exists = path_exists(candidate.get("path", ""))
    if ex_exists != ca_exists:
        return existing if ex_exists else candidate
    # sonst: behalten
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

# ============== gamelist.xml finden (mit Realpath-Dedupe) ==============
println("🔎 Suche gamelist.xml …")
seen_dirs = set()
gamelists = []
seen_gamelist_real = set()
duplicates_skipped = []

for root, dirnames, filenames in os.walk("./", followlinks=True):
    # Symlink-Schleifen vermeiden
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
if duplicates_skipped:
    sample = duplicates_skipped[:10]
    println(f"↪️  Duplikate übersprungen (gleicher Realpfad): {len(duplicates_skipped)}")
    for src, real in sample:
        println(f"   • skip: {src}  →  {real}")
    if len(duplicates_skipped) > len(sample):
        println(f"   … weitere {len(duplicates_skipped) - len(sample)} ausgelassen …")
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

# ============== Helpers: lokale Kandidatenzahl pro Datei ==============
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

# ============== Verarbeitung pro Datei (mit Fortschritt) ==============
def progress_line(proc_in_file, cand, kept, dup, md5_plus):
    pct_file = (proc_in_file / cand) if cand else 0.0
    bar = format_bar(pct_file, 30)
    return f"   {bar} {proc_in_file}/{cand} | kept:{kept} dup:{dup} md5+:{md5_plus}"

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

            # Filter
            if is_true(safe_text(game, "hidden", "")):
                stats["hidden"] += 1
                game.clear()
                continue
            if not is_true(safe_text(game, "favorite", "")):
                stats["not_favorite"] += 1
                game.clear()
                continue

            md5 = (safe_text(game, "md5", "") or "")
            if md5.lower() == DUMMY_MD5:
                md5 = ""

            gpath = safe_text(game, "path", "")
            xml_dir = os.path.dirname(file_path)
            real_target = resolve_game_path(gpath, xml_dir)

            # PS3: Unterpfade auf "<spiel>.ps3" zusammenklappen; .ps3.squashfs als Datei akzeptieren
            if is_ps3_squashfs(real_target):
                effective_path = real_target
            else:
                ps3_root = extract_ps3_root(real_target)
                effective_path = ps3_root if ps3_root else real_target

            # Existenz prüfen
            if not path_exists(effective_path):
                stats["missing_file"] += 1
                game.clear()
                print_line(progress_line(processed_in_file, cand, stats["kept"], dup_count, stats["md5_computed"]))
                continue

            # Optional MD5 für Dateien
            if MD5_ENABLED and (not md5) and os.path.isfile(effective_path):
                computed = file_md5(effective_path)
                if computed:
                    md5 = computed
                    stats["md5_computed"] += 1
                else:
                    stats["md5_compute_failed"] += 1

            norm_title = normalize_title_strict(raw_title)

            # Primärschlüssel nach Verlässlichkeit
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

# ============== Zweite Dedupe-Runde (titelbasiert, falls kein echter Identifikator) ==============
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

# ============== Materialisieren & CSV schreiben ==============
rows = []
system_counter = Counter()
for r in by_title_group.values():
    out_title = clean_output_title(r["title"])
    rows.append((out_title, r["system"], r["id"], r["md5"]))
    system_counter[r["system"]] += 1

rows.sort(key=lambda x: x[0].casefold())
rows_with_index = [(i, *row) for i, row in enumerate(rows)]

timestamp = int(time.time())
csv_filename = f"{timestamp}.csv"

with open(csv_filename, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["index", "title", "system", "id", "md5", "stats_system", "stats_total"])
    for rec in rows_with_index:
        i, title, system, gid, md5 = rec
        safe_title = protect_for_numbers(title)  # <— hier passiert der Numbers-Schutz automatisch
        w.writerow([i, safe_title, system, gid, md5, "", ""])
    for system in sorted(system_counter.keys(), key=str.casefold):
        w.writerow(["", "", "", "", "", system, system_counter[system]])

println(f"📄 Exportiert {len(rows_with_index)} eindeutige Favoriten nach {csv_filename}")
println(f"♻️  Duplikate übersprungen/zusammengeführt: {dup_count}")
println(f"🔑 Key-Strategie: {key_strategy_counts}")
println(f"🧮 Filter-Stats: {stats}")

