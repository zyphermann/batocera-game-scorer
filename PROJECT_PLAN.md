# Perfect Arcade Games - Projektplan

## Ziel

Wir wollen aus einer Batocera-41-Spielesammlung automatisch eine kuratierte Bestenliste erzeugen. Die Grundlage sind zuerst die lokalen Nutzungsdaten aus Batocera, vor allem:

- Favoritenstatus
- Spieltitel
- System/Plattform
- Batocera-/ScreenScraper-ID, falls vorhanden
- MD5, falls vorhanden oder optional berechnet
- Playcount
- Gametime

Danach sollen externe Qualitätsdaten ergänzt werden, vor allem der MobyGames-Score. Am Ende soll eine Export-Logik daraus z. B. die besten 250 Spiele nach frei gewichtbaren Kriterien erzeugen.

## Aktueller Stand

Aktuell ist der Ablauf noch CSV-basiert:

1. `buildxmlfavorites_by_playtime.py`
   - scannt alle `gamelist.xml` Dateien unterhalb des aktuellen Ordners
   - filtert auf `favorite == true` und `hidden != true`
   - prueft, ob die ROM-/Game-Datei existiert
   - dedupliziert Spiele ueber MD5, ID, Pfad oder normalisierten Titel
   - schreibt eine Zeitstempel-CSV, z. B. `1782635849.csv`
   - sortiert nach Spielzeit, Playcount und Titel

2. `add_usage_score.mjs`
   - liest `1782635849.csv`
   - erzeugt `1782635849_with_usage_score.csv`
   - berechnet `usage_score` aus `playcount` und `gametime_hours`

3. `enrich_mobygames.py`
   - liest `1782635849_with_usage_score.csv`
   - fragt die MobyGames-API ab
   - cached API-Antworten in `mobygames_cache.sqlite`
   - schreibt `1782635849_with_mobygames.csv`
   - ist resume-faehig: bereits bearbeitete Zeilen werden erkannt und uebersprungen

## Problem mit dem CSV-Ablauf

Die CSV-Dateien sind als Zwischenschritt praktisch, werden aber schnell unhandlich:

- mehrere Dateien mit fast gleichem Inhalt
- kein zentraler Zustand
- Resume-Logik muss ueber CSV-Spalten rekonstruiert werden
- Score-Berechnungen und externe Daten liegen nicht sauber getrennt vor
- Exporte sind schwer reproduzierbar, wenn mehrere CSV-Generationen existieren

Langfristig sollte die CSV nur noch ein Exportformat sein, nicht mehr der zentrale Datenspeicher.

## Zielarchitektur: eine SQLite-Datenbank

Ja: `buildxmlfavorites_by_playtime.py` kann so umgebaut werden, dass es nicht mehr primaer CSV schreibt, sondern dieselbe SQLite-Datenbank aktualisiert. Dann brauchen wir keine Kette von CSV-Dateien mehr.

Vorgeschlagene Datenbank:

```text
perfect_arcade_games.sqlite
```

Der MobyGames-Cache kann entweder in diese Datenbank integriert oder aus `mobygames_cache.sqlite` migriert werden. Sinnvoll ist langfristig eine einzige Datenbank.

## Tabellenvorschlag

### `games`

Ein Datensatz pro eindeutig erkanntem Spiel in der lokalen Sammlung.

```sql
CREATE TABLE games (
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
```

`game_key` sollte aus der bestehenden Dedupe-Logik kommen:

- bevorzugt `system + md5`
- danach `system + id`
- danach `system + path`
- danach `system + norm_title`

### `playtime`

Aktueller Stand der Batocera-Nutzungsdaten pro Spiel.

```sql
CREATE TABLE playtime (
  game_key TEXT PRIMARY KEY,
  playcount INTEGER NOT NULL DEFAULT 0,
  gametime INTEGER NOT NULL DEFAULT 0,
  gametime_hours REAL NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (game_key) REFERENCES games(game_key)
);
```

### `playtime_history`

Optional, aber sehr nuetzlich: Snapshots pro Scan. Damit koennen wir spaeter Trends erkennen.

```sql
CREATE TABLE playtime_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game_key TEXT NOT NULL,
  playcount INTEGER NOT NULL,
  gametime INTEGER NOT NULL,
  gametime_hours REAL NOT NULL,
  scanned_at INTEGER NOT NULL,
  FOREIGN KEY (game_key) REFERENCES games(game_key)
);
```

### `scores`

Berechnete interne Scores.

```sql
CREATE TABLE scores (
  game_key TEXT PRIMARY KEY,
  usage_score REAL,
  moby_score_100 REAL,
  overall_score REAL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (game_key) REFERENCES games(game_key)
);
```

`usage_score` ersetzt die aktuelle extra CSV-Spalte. Die Berechnung kann direkt im Import-Skript oder in einem separaten Score-Skript laufen.

### `mobygames_matches`

Das Ergebnis des MobyGames-Matchings pro lokalem Spiel.

```sql
CREATE TABLE mobygames_matches (
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
  FOREIGN KEY (game_key) REFERENCES games(game_key)
);
```

### `api_cache`

Diese Tabelle existiert heute schon in `mobygames_cache.sqlite`.

```sql
CREATE TABLE api_cache (
  cache_key TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  status INTEGER NOT NULL,
  response TEXT NOT NULL,
  fetched_at INTEGER NOT NULL
);
```

Sie kann unveraendert in die zentrale Datenbank uebernommen werden.

## Neuer Ablauf

Zielzustand:

1. Batocera-Scan

   ```bash
   python3 scan_batocera_playtime.py
   ```

   Aktualisiert:

   - `games`
   - `playtime`
   - optional `playtime_history`
   - `scores.usage_score`

2. MobyGames-Enrichment

   ```bash
   python3 enrich_mobygames.py
   ```

   Aktualisiert:

   - `api_cache`
   - `mobygames_matches`
   - `scores.moby_score_100`

3. Gesamtscore berechnen

   ```bash
   python3 update_scores.py
   ```

   Beispiel:

   ```text
   overall_score =
     0.45 * usage_score +
     0.35 * moby_score_100 +
     0.20 * manual_score
   ```

4. Export

   ```bash
   python3 export_games.py --top 250 --format csv
   ```

   Exportiert nur noch aus der Datenbank, z. B.:

   - `top_250_games.csv`
   - `top_250_games.xml`
   - spaeter optional Batocera-kompatible Favoritenlisten

## Wichtige Designentscheidung

Die Datenbank sollte der einzige dauerhafte Zustand sein. CSV-Dateien sind dann nur noch Exporte.

Das bedeutet:

- Batocera-XML wird gescannt und in SQLite aktualisiert
- MobyGames wird gecached und in SQLite gematcht
- Scores werden in SQLite gespeichert oder per SQL berechnet
- Exporte werden jederzeit reproduzierbar aus SQLite erzeugt

## Naechste konkrete Schritte

1. Datenbankschema in einem neuen Modul anlegen: `arcade_db.py`. Status: erledigt.
2. SQLite-Scanner aus `buildxmlfavorites_by_playtime.py` ableiten: `scan_batocera_playtime.py`. Status: erledigt.
3. `buildxmlfavorites_by_playtime.py` bleibt vorerst als CSV-Fallback erhalten.
4. Bestehende CSVs einmalig in die neue Datenbank importieren, falls wir alte Enrichment-Ergebnisse uebernehmen wollen.
5. `enrich_mobygames.py` so umbauen, dass es direkt aus `games`/`playtime` liest und nach `mobygames_matches` schreibt. Status: erledigt.
6. `export_games.py` bauen, das Toplisten nach Kriterien exportiert.

Der neue Scanner enthaelt aktuell:

   - XML-Scanner/Dedupe-Logik
   - Datenbank-Writer
   - Playtime-Update
   - optionalen `playtime_history` Snapshot
   - automatische `usage_score` Aktualisierung

## Gemeinsames DB-Modul

`arcade_db.py` ist die zentrale SQLite-Schicht. Neue Skripte sollen nicht direkt eigenes Schema anlegen, sondern dieses Modul verwenden.

Wichtige Funktionen:

- `connect_db(path="perfect_arcade_games.sqlite")`
- `build_game_key(system, md5, batocera_id, path, norm_title)`
- `upsert_game(...)`
- `upsert_playtime(...)`
- `update_usage_scores(...)`
- `upsert_mobygames_match(...)`
- `update_overall_scores(...)`
- `cache_get(...)` / `cache_put(...)`
- `top_games(...)`

Damit koennen Scanner, MobyGames-Enrichment und Exporte dieselbe Datenbank verwenden.

## MobyGames-Enrichment

`enrich_mobygames.py` liest jetzt direkt aus `perfect_arcade_games.sqlite`.

Default auf Batocera:

```bash
cd /userdata/scoring
python3 enrich_mobygames.py --limit 250
```

Der API-Key kann in `/userdata/scoring/mobygames_api_key.txt` stehen. Diese Datei soll nur den Key enthalten, ohne weitere Formatierung.

Alternativen:

```bash
export MOBYGAMES_API_KEY="..."
python3 enrich_mobygames.py --limit 250
```

oder:

```bash
python3 enrich_mobygames.py --api-key "..." --limit 250
```

Das Skript aktualisiert:

- `api_cache`
- `mobygames_matches`
- `scores.moby_score_100`
- `scores.overall_score`

## Bedienungsanleitung auf Batocera

### 1. Dateien ablegen

Alle Projektdateien liegen auf Batocera in:

```text
/userdata/scoring
```

Mindestens benoetigt:

```text
/userdata/scoring/arcade_db.py
/userdata/scoring/scan_batocera_playtime.py
/userdata/scoring/enrich_mobygames.py
/userdata/scoring/export_games.py
```

Die zentrale Datenbank entsteht ebenfalls dort:

```text
/userdata/scoring/perfect_arcade_games.sqlite
```

### 2. MobyGames API-Key hinterlegen

Im Ordner `/userdata/scoring` eine Datei anlegen:

```bash
cd /userdata/scoring
nano mobygames_api_key.txt
```

In diese Datei nur den API-Key schreiben, ohne Anfuehrungszeichen und ohne weitere Zeilen.

Die Datei liegt danach hier:

```text
/userdata/scoring/mobygames_api_key.txt
```

Alternative ohne Datei:

```bash
export MOBYGAMES_API_KEY="DEIN_KEY"
```

### 3. Spielzeiten aus Batocera aktualisieren

Immer wenn neue Spielzeiten aus Batocera uebernommen werden sollen:

```bash
cd /userdata/scoring
python3 scan_batocera_playtime.py
```

Das Skript:

- scannt `/userdata/roms`
- liest alle `gamelist.xml`
- importiert Favoriten, die nicht versteckt sind
- aktualisiert `games`
- aktualisiert `playtime`
- schreibt einen Snapshot nach `playtime_history`
- berechnet `usage_score`
- aktualisiert `overall_score`

Wenn alle sichtbaren Spiele statt nur Favoriten importiert werden sollen:

```bash
python3 scan_batocera_playtime.py --all-visible
```

Wenn kein History-Snapshot geschrieben werden soll:

```bash
python3 scan_batocera_playtime.py --no-history
```

Nur fuer Tests mit Teilordnern:

```bash
python3 scan_batocera_playtime.py --root /pfad/zum/test --no-reset
```

Wichtig: Ohne `--no-reset` markiert der Scanner alte DB-Spiele zuerst als nicht aktuell vorhanden. Das ist fuer den normalen Vollscan unter `/userdata/roms` richtig.

### 4. MobyGames weiterlaufen lassen

Vor einem echten Lauf kann man pruefen, welche Spiele als naechstes drankommen:

```bash
cd /userdata/scoring
python3 enrich_mobygames.py --dry-run --limit 20
```

Danach z. B. 250 weitere Spiele anreichern:

```bash
python3 enrich_mobygames.py --limit 250
```

Oder einfach eine groessere Session:

```bash
python3 enrich_mobygames.py --limit 1000
```

Das Skript:

- liest offene Spiele aus `perfect_arcade_games.sqlite`
- sortiert nach `usage_score`
- ueberspringt bereits bearbeitete Spiele
- nutzt `api_cache`, damit gleiche API-Abfragen nicht erneut gesendet werden
- schreibt Ergebnisse nach `mobygames_matches`
- schreibt den MobyGames-Score nach `scores.moby_score_100`
- aktualisiert `scores.overall_score`

Das Skript kann jederzeit mit `Ctrl+C` beendet werden. Beim naechsten Start macht es bei den noch offenen Spielen weiter.

Wenn die MobyGames-API das Stundenlimit meldet (`HTTP 429`), wartet das Skript automatisch die von der API gemeldete Zeit plus kleinen Puffer ab und versucht denselben Request erneut. Der Fehler wird nicht im Cache gespeichert, weil es nur ein temporaeres Rate-Limit ist.

Beispielmeldung:

```text
Rate limit reached.
Waiting 1359 seconds before retrying...
Waiting 1349 seconds before retrying...
Waiting 1339 seconds before retrying...
```

Das ist kein Fehler. Das Skript gibt waehrend der Wartezeit alle 10 Sekunden den aktuellen Rest-Countdown aus und macht danach automatisch weiter.

Bei schwachem WLAN koennen ausserdem Timeouts, TLS-Fehler oder abgebrochene HTTP-Antworten auftreten. Diese werden ebenfalls nicht als endgueltiger Fehler behandelt. Das Skript wartet zuerst 30 Sekunden, danach bei wiederholten Netzwerkproblemen mit Backoff bis maximal 300 Sekunden, und versucht denselben Request erneut.

Beispiel:

```text
Network error: TimeoutError: ...
Waiting 30 seconds before retrying...
Waiting 20 seconds before retrying...
Waiting 10 seconds before retrying...
```

Fuer lange 24/7-Laeufe sollte das Skript nicht in einer normalen SSH-Session ohne Schutz laufen, weil der Prozess beim Schliessen der Verbindung beendet werden kann. Besser mit `tmux`, falls auf Batocera verfuegbar:

```bash
tmux new -s moby
cd /userdata/scoring
python3 enrich_mobygames.py
```

`tmux`-Session verlassen, ohne das Skript zu stoppen:

```text
Ctrl+B, dann D
```

Wieder verbinden:

```bash
tmux attach -t moby
```

Falls `tmux` nicht installiert ist, kann alternativ `screen` funktionieren:

```bash
screen -S moby
cd /userdata/scoring
python3 enrich_mobygames.py
```

`screen` verlassen, ohne zu stoppen:

```text
Ctrl+A, dann D
```

Wieder verbinden:

```bash
screen -r moby
```

Wenn ein bereits gematchtes Spiel bewusst neu verarbeitet werden soll:

```bash
python3 enrich_mobygames.py --limit 100 --redo
```

### 5. Datenbank kontrollieren

SQLite oeffnen:

```bash
cd /userdata/scoring
sqlite3 perfect_arcade_games.sqlite
```

Tabellen anzeigen:

```sql
.tables
```

Anzahl Spiele:

```sql
SELECT COUNT(*) FROM games;
```

Anzahl MobyGames-Matches:

```sql
SELECT COUNT(*) FROM mobygames_matches;
```

Top 20 nach Usage-Score:

```sql
SELECT g.title, g.system, p.playcount, p.gametime_hours, s.usage_score
FROM games g
JOIN playtime p ON p.game_key = g.game_key
LEFT JOIN scores s ON s.game_key = g.game_key
WHERE g.favorite = 1 AND g.hidden = 0 AND g.file_exists = 1
ORDER BY s.usage_score DESC
LIMIT 20;
```

Top 20 mit MobyGames-Score:

```sql
SELECT g.title, g.system, s.usage_score, s.moby_score_100, s.overall_score,
       m.match_status, m.match_confidence, m.moby_title
FROM games g
LEFT JOIN scores s ON s.game_key = g.game_key
LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE g.favorite = 1 AND g.hidden = 0 AND g.file_exists = 1
ORDER BY s.overall_score DESC
LIMIT 20;
```

SQLite verlassen:

```sql
.quit
```

Direkt aus der Shell geht es auch:

```bash
sqlite3 -header -column perfect_arcade_games.sqlite "
SELECT g.title, g.system, p.playcount, p.gametime_hours, s.usage_score
FROM games g
JOIN playtime p ON p.game_key = g.game_key
LEFT JOIN scores s ON s.game_key = g.game_key
ORDER BY s.usage_score DESC
LIMIT 20;
"
```

### 6. Typischer Arbeitsablauf

Regelmaessig, wenn neue Spielzeiten dazukommen:

```bash
cd /userdata/scoring
python3 scan_batocera_playtime.py
```

Wenn Zeit fuer MobyGames-Abfragen ist:

```bash
cd /userdata/scoring
python3 enrich_mobygames.py --limit 250
```

Zwischenstand pruefen:

```bash
sqlite3 perfect_arcade_games.sqlite "SELECT COUNT(*) AS games FROM games;"
sqlite3 perfect_arcade_games.sqlite "SELECT COUNT(*) AS moby_matches FROM mobygames_matches;"
```

### 7. CSV exportieren

Verfuegbare Export-Schemata anzeigen:

```bash
cd /userdata/scoring
python3 export_games.py --list-schemas
```

Aktuelle Schemata:

- `local_usage`: Mix aus Starts und Spielzeit.
- `quality_mix`: Mix aus Starts, Spielzeit und MobyGames-Score.
- `playtime_focus`: primaer nach Spielzeit.
- `replay_focus`: primaer nach Anzahl der Starts.
- `critic_favorites`: MobyGames-Score dominiert, lokale Nutzung bricht Gleichstaende.
- `catchup`: Nachholbedarf, also hoher MobyGames-Score bei niedriger eigener Nutzung.
- `balanced`: ausgewogener Mix aus `usage_score` und `moby_score_100`.

Top 250 mit dem Standard-Schema `quality_mix` exportieren:

```bash
cd /userdata/scoring
python3 export_games.py --top 250 --output top_250_games.csv
```

Top 250 nach lokaler Nutzung exportieren:

```bash
python3 export_games.py --top 250 --schema local_usage --output top_250_local_usage.csv
```

Top 250 mit MobyGames-lastigem Schema exportieren:

```bash
python3 export_games.py --top 250 --schema critic_favorites --output top_250_critic_favorites.csv
```

Top 250 nach Spielzeit:

```bash
python3 export_games.py --top 250 --schema playtime_focus --output top_250_playtime.csv
```

Top 250 nach Starts:

```bash
python3 export_games.py --top 250 --schema replay_focus --output top_250_replay.csv
```

Top 250 nach Nachholbedarf:

```bash
python3 export_games.py --top 250 --schema catchup --refresh-scores --output top_250_catchup.csv
```

Der Nachholbedarf wird in `scores.catchup_score` gespeichert:

```text
catchup_score = moby_score_100 * (1 - usage_score / 100)
```

Interpretation:

- hoher MobyGames-Score
- niedriger eigener `usage_score`
- ergibt hohen Nachholbedarf

Bereits viel gespielte Spiele fallen automatisch nach unten, auch wenn sie sehr gut bewertet sind.

Direkt per SQLite:

```sql
SELECT
  g.title,
  g.system,
  s.usage_score,
  s.moby_score_100,
  s.catchup_score,
  m.moby_title,
  m.moby_url
FROM games g
JOIN scores s ON s.game_key = g.game_key
LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND s.catchup_score IS NOT NULL
ORDER BY s.catchup_score DESC, s.moby_score_100 DESC, g.title COLLATE NOCASE ASC
LIMIT 100;
```

Top 250 nach Nachholbedarf direkt aus der Shell:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  p.playcount,
  p.gametime_hours,
  s.usage_score,
  s.moby_score_100,
  s.catchup_score,
  m.moby_title,
  m.moby_url
FROM games g
JOIN playtime p ON p.game_key = g.game_key
JOIN scores s ON s.game_key = g.game_key
LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND s.catchup_score IS NOT NULL
ORDER BY s.catchup_score DESC, s.moby_score_100 DESC, g.title COLLATE NOCASE ASC
LIMIT 250;
"
```

Standardmaessig werden MobyGames-Matches mit Status `review` nicht exportiert. Wenn diese trotzdem in der CSV stehen sollen:

```bash
python3 export_games.py --top 250 --include-review --output top_250_games.csv
```

Wenn nur sicher gematchte MobyGames-Spiele exportiert werden sollen:

```bash
python3 export_games.py --top 250 --matched-only --output top_250_matched.csv
```

Die CSV enthaelt unter anderem:

- `rank`
- `export_schema`
- `export_score`
- `title`
- `system`
- `playcount`
- `gametime_hours`
- `usage_score`
- `moby_score_100`
- `overall_score`
- `moby_match_status`
- `moby_match_confidence`
- `moby_title`
- `moby_url`

### 8. Fehlerfaelle

Wenn `enrich_mobygames.py` meldet, dass der API-Key fehlt:

```bash
cd /userdata/scoring
ls -l mobygames_api_key.txt
```

Falls die Datei fehlt:

```bash
nano mobygames_api_key.txt
```

Wenn `enrich_mobygames.py` meldet, dass die Datenbank fehlt:

```bash
python3 scan_batocera_playtime.py
```

Wenn keine Spiele gefunden werden:

```bash
ls /userdata/roms
find /userdata/roms -name gamelist.xml | head
```

Wenn ein Testscan mit Teilordnern gemacht wird, immer `--no-reset` verwenden, damit der echte Bestand nicht temporaer inaktiv markiert wird:

```bash
python3 scan_batocera_playtime.py --root /pfad/zum/test --no-reset
```

## Lokaler Webserver

`web_dashboard.py` stellt ein kleines lokales Dashboard bereit, um die SQLite-Datenbank im Browser anzuschauen und Aktionen zu starten.

Start auf Batocera:

```bash
cd /userdata/scoring
python3 web_dashboard.py
```

Start ueber das Autostart-Helferskript:

```bash
cd /userdata/scoring
chmod +x dashboard_autostart.sh
./dashboard_autostart.sh start
```

Status pruefen:

```bash
./dashboard_autostart.sh status
```

Stoppen:

```bash
./dashboard_autostart.sh stop
```

Logs:

```bash
tail -f /userdata/scoring/logs/web_dashboard.log
```

### Dashboard beim Batocera-Start automatisch starten

Batocera kann eigene Boot-Kommandos ueber `/userdata/system/custom.sh` ausfuehren. Dazu das Autostart-Skript aus `/userdata/scoring` dort einhaengen:

```bash
mkdir -p /userdata/system
cat >> /userdata/system/custom.sh <<'EOF'

# Start local scoring dashboard
/userdata/scoring/dashboard_autostart.sh start
EOF
chmod +x /userdata/system/custom.sh
```

Danach beim naechsten Batocera-Start automatisch erreichbar:

```text
http://BATOCERA-IP:8765
```

Wenn der Port geaendert werden soll, kann man Umgebungsvariablen in `custom.sh` setzen:

```bash
DASHBOARD_PORT=8080 /userdata/scoring/dashboard_autostart.sh start
```

Danach im Browser eines Rechners im selben Netzwerk oeffnen:

```text
http://BATOCERA-IP:8765
```

Beispiel:

```text
http://192.168.1.50:8765
```

Die IP-Adresse von Batocera kann man z. B. so anzeigen:

```bash
ip addr
```

Oder im Batocera-Menue unter den Netzwerkeinstellungen nachsehen.

Das Dashboard kann:

- Statistik-Karten anzeigen
- Views wie `v_all_games`, `v_top_by_catchup`, `v_top_by_playtime`, `v_top_by_moby_score` und `v_mobygames_pending` anzeigen
- Spalten per Klick auf die Tabellenkoepfe sortieren
- seitenweise durch lange Tabellen blaettern
- per Suchfeld nach `title` und `system` filtern
- Spiele ueber Batoceras `configgen.emulatorlauncher` starten
- laufende Emulatorprozesse per Stop-Button beenden
- Spielzeiten aktualisieren
- Scores neu berechnen
- MobyGames-Enrichment mit Limit im Hintergrund starten
- Job-Logs anzeigen

Fuer laengeren Betrieb am besten in `tmux` starten:

```bash
tmux new -s scoring-web
cd /userdata/scoring
python3 web_dashboard.py
```

`tmux` verlassen, ohne den Webserver zu stoppen:

```text
Ctrl+B, dann D
```

Wieder verbinden:

```bash
tmux attach -t scoring-web
```

Alternative mit anderem Port:

```bash
python3 web_dashboard.py --port 8080
```

Nur lokal auf Batocera binden, nicht im Netzwerk:

```bash
python3 web_dashboard.py --host 127.0.0.1
```

Dashboard-URL mit Suche, Sortierung und Pagination:

```text
http://BATOCERA-IP:8765/?view=v_all_games&q=switch&sort=playcount&dir=desc&page=1&limit=100
```

Das Suchfeld `q` wirkt auf:

- `title`
- `system`

### Spiele aus dem Dashboard starten

Pro Tabellenzeile gibt es einen `Launch`-Button. Dieser nutzt auf Batocera den gleichen Launcher-Pfad, der auch aus SSH erfolgreich getestet wurde:

```bash
DISPLAY=:0 /usr/bin/python /usr/bin/emulatorlauncher -system SYSTEM -rom ROM_PATH
```

Das Dashboard startet den Prozess im Hintergrund, damit die Weboberflaeche weiter erreichbar bleibt.

Ein laufendes Spiel kann ueber den Button `Stop Game` beendet werden. Intern wird zuerst `SIGINT` an die Launcher-Prozessgruppe geschickt. Das entspricht dem sauberen Abbruch aus dem SSH-Terminal und gibt Batocera die Chance, Emulator, `evmapy` und Aufloesung korrekt aufzuraeumen.

Nur wenn der Launcher nicht reagiert, folgen Fallbacks mit `SIGTERM` und danach typische Emulatorprozesse per `pkill -f`, z. B.:

```text
retroarch
duckstation
pcsx2
ppsspp
dolphin-emu
mupen64plus
ryujinx
yuzu
suyu
eden
citron
```

Normaler Batocera-Weg zum Beenden bleibt weiterhin:

```text
Hotkey + Start
```

Wenn ein Emulator hart beendet wird, kann Batocera gelegentlich mit falscher Aufloesung zu EmulationStation zurueckkehren. Der Stop-Button versucht deshalb nach dem Beenden einen Display-Reset:

```bash
batocera-resolution setMode max-1920x1080
```

Fallback:

```bash
batocera-resolution setMode max
```

Manuelle Recovery per SSH:

```bash
DISPLAY=:0 batocera-resolution setMode max-1920x1080
```

oder:

```bash
DISPLAY=:0 batocera-resolution setMode max
```

### Switch-Launch-Fehler: no generator found

Wenn ein Switch-Spiel mit einem Fehler wie diesem sofort beendet wird:

```text
Exception: no generator found for emulator ryujinx-continuous
```

dann kommt der Fehler aus Batoceras Emulator-Konfiguration, nicht aus dem Dashboard. Im Log sieht man dann z. B.:

```text
Settings: {'emulator': 'ryujinx-continuous', 'core': 'ryujinx-continuous', ...}
ModuleNotFoundError: No module named 'configgen.generators.ryujinx-continuous'
```

Batocera versucht in diesem Fall einen Generator namens `ryujinx-continuous` zu laden, der auf dem System nicht vorhanden ist.

Pruefen:

```bash
grep -n "switch.*emulator\\|switch.*core" /userdata/system/batocera.conf
ls /usr/lib/python3.11/site-packages/configgen/generators | grep -i "ryu\\|yuzu\\|switch"
```

Danach in EmulationStation fuer Switch einen vorhandenen Emulator/Core setzen oder die betreffenden `switch.emulator` / `switch.core` Eintraege in `/userdata/system/batocera.conf` korrigieren. Nach einer Aenderung EmulationStation neu starten oder Batocera rebooten.

```bash
/etc/init.d/S31emulationstation restart
```

Sicherheitshinweis: Der Webserver kann lokal Programme starten und beenden. Er sollte nur im vertrauenswuerdigen Heimnetz laufen und nicht ins Internet weitergeleitet werden.

## Beispiel-Queries

Alle Queries koennen direkt auf Batocera ausgefuehrt werden.

Die wichtigsten Abfragen werden zusaetzlich als SQLite-Views angelegt, sobald ein Skript `arcade_db.py` laedt, z. B.:

```bash
cd /userdata/scoring
python3 export_games.py --list-schemas
```

Danach sind diese Views verfuegbar:

- `v_active_games`
- `v_all_games`
- `v_top_by_usage`
- `v_top_by_playtime`
- `v_top_by_moby_score`
- `v_top_by_combined_60_40`
- `v_top_by_catchup`
- `v_mobygames_pending`
- `v_mobygames_review`

### Views verwenden

Alle aktiven Spiele:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  playcount,
  gametime_hours,
  usage_score,
  moby_score_100,
  catchup_score,
  overall_score,
  moby_match_status
FROM v_all_games
LIMIT 250;
"
```

Top 250 nach Nachholbedarf:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  playcount,
  gametime_hours,
  usage_score,
  moby_score_100,
  catchup_score,
  moby_title,
  moby_url
FROM v_top_by_catchup
LIMIT 250;
"
```

Top 250 nach Spielzeit:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  gametime_hours,
  playcount,
  usage_score
FROM v_top_by_playtime
LIMIT 250;
"
```

Top 100 nach MobyGames-Score:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  moby_score_100,
  moby_title,
  moby_match_confidence,
  moby_url
FROM v_top_by_moby_score
LIMIT 100;
"
```

Top 100 nach kombinierter Formel 60/40:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  usage_score,
  moby_score_100,
  combined_score
FROM v_top_by_combined_60_40
LIMIT 100;
"
```

Noch nicht mit MobyGames bearbeitete Spiele:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  usage_score,
  playcount,
  gametime_hours
FROM v_mobygames_pending
LIMIT 100;
"
```

Unsichere MobyGames-Matches:

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  rank,
  title,
  system,
  moby_match_status,
  moby_match_confidence,
  moby_title,
  moby_url
FROM v_mobygames_review
LIMIT 100;
"
```

### Anzahl Spiele

```bash
sqlite3 /userdata/scoring/perfect_arcade_games.sqlite "
SELECT COUNT(*) AS games
FROM games
WHERE favorite = 1
  AND hidden = 0
  AND file_exists = 1;
"
```

### Anzahl MobyGames-Matches

```bash
sqlite3 /userdata/scoring/perfect_arcade_games.sqlite "
SELECT COUNT(*) AS moby_matches
FROM mobygames_matches;
"
```

### Top 100 nach MobyGames-Score

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  s.moby_score_100,
  m.moby_title,
  m.match_confidence,
  m.moby_url
FROM games g
JOIN scores s ON s.game_key = g.game_key
LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND s.moby_score_100 IS NOT NULL
ORDER BY s.moby_score_100 DESC, g.title COLLATE NOCASE ASC
LIMIT 100;
"
```

### Top 100 nach Usage-Score

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  p.playcount,
  p.gametime_hours,
  s.usage_score
FROM games g
JOIN playtime p ON p.game_key = g.game_key
JOIN scores s ON s.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND s.usage_score IS NOT NULL
ORDER BY s.usage_score DESC, g.title COLLATE NOCASE ASC
LIMIT 100;
"
```

### Top 100 nach kombinierter Formel

Formel:

```text
usage_score * 0.6 + moby_score_100 * 0.4
```

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  s.usage_score,
  s.moby_score_100,
  ROUND((s.usage_score * 0.6) + (s.moby_score_100 * 0.4), 2) AS combined_score
FROM games g
JOIN scores s ON s.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND s.usage_score IS NOT NULL
  AND s.moby_score_100 IS NOT NULL
ORDER BY combined_score DESC, g.title COLLATE NOCASE ASC
LIMIT 100;
"
```

### Top 250 nach Nachholbedarf

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  p.playcount,
  p.gametime_hours,
  s.usage_score,
  s.moby_score_100,
  s.catchup_score,
  m.moby_title,
  m.moby_url
FROM games g
JOIN playtime p ON p.game_key = g.game_key
JOIN scores s ON s.game_key = g.game_key
LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND s.catchup_score IS NOT NULL
ORDER BY s.catchup_score DESC, s.moby_score_100 DESC, g.title COLLATE NOCASE ASC
LIMIT 250;
"
```

### Noch nicht mit MobyGames bearbeitete Spiele

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  s.usage_score,
  p.playcount,
  p.gametime_hours
FROM games g
JOIN playtime p ON p.game_key = g.game_key
LEFT JOIN scores s ON s.game_key = g.game_key
LEFT JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE g.favorite = 1
  AND g.hidden = 0
  AND g.file_exists = 1
  AND m.game_key IS NULL
ORDER BY s.usage_score DESC, p.gametime DESC, g.title COLLATE NOCASE ASC
LIMIT 100;
"
```

### Unsichere MobyGames-Matches pruefen

```bash
sqlite3 -header -column /userdata/scoring/perfect_arcade_games.sqlite "
SELECT
  g.title,
  g.system,
  m.match_status,
  m.match_confidence,
  m.moby_title,
  m.moby_url
FROM games g
JOIN mobygames_matches m ON m.game_key = g.game_key
WHERE m.match_status = 'review'
ORDER BY m.match_confidence ASC, g.title COLLATE NOCASE ASC
LIMIT 100;
"
```

## Batocera-41-Kontext

Der Zielbetrieb ist Batocera 41. Typischerweise liegen die relevanten Daten unter:

```text
/userdata/roms/<system>/gamelist.xml
```

Die Scoring-Skripte sollen unter folgendem Ordner liegen:

```text
/userdata/scoring
```

Wichtige Pfadregel:

- Skripte, SQLite-Datenbank, Cache und Exporte liegen in `/userdata/scoring`.
- Gescannte Batocera-Daten liegen weiter unter `/userdata/roms`.

Das Scan-Skript verwendet daher standardmaessig:

```bash
python3 scan_batocera_playtime.py --root /userdata/roms
```

`buildxmlfavorites_by_playtime.py` wurde entsprechend vorbereitet:

```bash
python3 /userdata/scoring/buildxmlfavorites_by_playtime.py
```

Default-Verhalten:

- Scan-Root: `/userdata/roms`
- CSV-Output: Ordner des Skripts, also `/userdata/scoring`

Fuer lokale Tests kann der Root-Pfad ueberschrieben werden:

```bash
python3 buildxmlfavorites_by_playtime.py --root .
```

## Offene Fragen

- Sollen nur Favoriten importiert werden oder alle sichtbaren Spiele?
- Soll `playtime_history` bei jedem Scan wachsen oder nur auf Wunsch geschrieben werden?
- Soll ein niedriger MobyGames-Match-Confidence-Wert automatisch ausgeschlossen werden?
- Brauchen wir spaeter einen manuellen Override pro Spiel, z. B. `manual_score` oder `exclude_from_top250`?
- Soll der finale Export CSV, XML oder direkt eine Batocera-kompatible Collection/Favoritenliste sein?
