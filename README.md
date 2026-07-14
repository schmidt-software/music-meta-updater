# Music Metadata Updater

Durchsucht rekursiv einen (via S3 gemounteten) Musik-Ordner und aktualisiert
bei allen Dateien ohne Cover oder Metadaten diese automatisch aus dem
Internet (MusicBrainz + Cover Art Archive via `beets`). Läuft vollständig
non-interaktiv.

## Dateien

- `update_music_metadata.sh` – Hauptskript. Scannt mit Python/mutagen alle
  Audiodateien auf fehlende Tags (Titel/Interpret/Album) oder fehlendes
  Cover, und lässt nur diese Dateien von `beets` automatisch taggen/covern.
  Verändert keine bereits vollständigen Dateien und verschiebt/benennt
  nichts um (bestehende Ordnerstruktur bleibt erhalten).
- `Dockerfile` – Image mit allen Abhängigkeiten (python3, chromaprint/fpcalc,
  ffmpeg, beets, mutagen, pyacoustid).
- `docker-compose.yml` – Mountet den Musik-Ordner nach `/music` sowie ein
  persistentes Volume `/data` für die beets-Datenbank und Logs.
- `.env.example` – Vorlage für Host-Pfad und AcoustID-Key.

## Setup

```bash
cp .env.example .env
# .env anpassen: MUSIC_HOST_PATH=<S3-Mount-Pfad>, ACOUSTID_API_KEY=<Key>
docker compose up --build
```

`ACOUSTID_API_KEY` ist optional aber empfohlen (kostenlos unter
acoustid.org) – ohne Key kann bei komplett fehlenden Tags nur anhand des
Dateinamens geraten werden, was deutlich unzuverlässiger ist.

## Offene Punkte / mögliche nächste Schritte

- Wiederkehrende Ausführung (Cron im Container, oder Scheduling extern)
- Feinere Kontrolle über Matching-Schwellenwerte in der beets-Config
  (`match.strong_rec_thresh` etc.), falls Fehlzuordnungen auftreten
- Genauere Fehlerbehandlung/Reporting (aktuell nur Logfile unter
  `/data/update.log` bzw. `/data/beets_import.log`)
- Tests mit echten Beispieldateien aus dem S3-Mount, bevor auf die
  gesamte Bibliothek losgelassen wird (`MUSIC_HOST_PATH` testweise auf
  einen kleinen Unterordner zeigen lassen)
