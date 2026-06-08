#!/usr/bin/env python3
"""
=============================================================
 Divera 24/7 – Probealarm Auto-Closer
 Raspberry Pi 3 Server Script
=============================================================
 Funktion:
   - Pollt regelmäßig die Divera 24/7 API auf neue Alarmierungen
   - Erkennt automatisch Probealarme anhand konfigurierbarer Keywords
   - Schließt/archiviert Probealarme automatisch nach 5 Minuten
   - Loggt alle Aktionen in eine Logdatei

 Voraussetzungen (Pi):
   sudo apt-get update
   sudo apt-get install python3-pip
   pip3 install requests

 Einrichtung:
   1. API-Key in DIVERA_API_KEY eintragen
   2. Script starten: python3 divera_probealarm_watcher.py
   3. Optional: Als systemd-Service einrichten (siehe unten)
=============================================================
"""

import requests
import time
import logging
import sys
import json
import os
from datetime import datetime

# ─────────────────────────────────────────────
# KONFIGURATION – hier anpassen!
# ─────────────────────────────────────────────

# Deinen Divera 24/7 API-Key eintragen
# Zu finden unter: Verwaltung → Schnittstellen → API-Key
DIVERA_API_KEY = "hier_muss_der_Systemkey_rein"

# Wie oft die API abgefragt wird (in Sekunden)
# Hinweis: FREE-Version erlaubt nur 1 Request alle 5 Minuten!
# ALARM/PRO-Version: 30-60 Sekunden empfohlen
POLL_INTERVAL_SECONDS = 60

# Nach wie vielen Minuten soll der Probealarm automatisch geschlossen werden?
AUTO_CLOSE_AFTER_MINUTES = 5

# Keywords, die einen Probealarm identifizieren (Groß-/Kleinschreibung egal)
# Wird in: Stichwort, Schlagwort, Meldebild geprüft
PROBEALARM_KEYWORDS = [
    "probe",
    "probealarm",
    "test",
    "testalarm",
    "übung",
    "uebung",
    "probeauslösung",
]

# Logdatei Pfad
LOG_FILE = "/home/pi/divera_watcher.log"

# Zähler-Datei (bleibt auch nach Neustart erhalten)
COUNTER_FILE = "/home/pi/divera_counter.json"

# API Basis-URL
DIVERA_BASE_URL = "https://www.divera247.com/api"

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),  # Auch auf der Konsole anzeigen
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ZUSTAND (im Speicher)
# ─────────────────────────────────────────────

# Speichert erkannte Probealarme: { alarm_id: datetime_erkannt }
pending_probe_alarms: dict = {}

# Bereits verarbeitete/geschlossene Alarm-IDs (verhindert Doppelverarbeitung)
already_closed: set = set()

# Bereits mit foreign_id versehene Alarm-IDs (verhindert doppeltes Setzen)
already_tagged: set = set()


# ─────────────────────────────────────────────
# ZÄHLER FUNKTIONEN
# ─────────────────────────────────────────────

def load_counters() -> dict:
    """Lädt die Zähler aus der JSON-Datei. Gibt Standardwerte zurück falls nicht vorhanden."""
    if os.path.exists(COUNTER_FILE):
        try:
            with open(COUNTER_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"einsatz": 0, "probe": 0}


def save_counters(counters: dict):
    """Speichert die Zähler in die JSON-Datei."""
    try:
        with open(COUNTER_FILE, "w") as f:
            json.dump(counters, f)
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Zähler: {e}")


def next_foreign_id(typ: str) -> str:
    """
    Erzeugt die nächste fortlaufende foreign_id.
    typ: 'E' für Einsatz, 'P' für Probealarm
    Format: E-0001-2026-06-05-22:58 / P-0001-2026-06-05-22:58
    """
    counters = load_counters()
    key = "einsatz" if typ == "E" else "probe"
    counters[key] += 1
    save_counters(counters)
    ts = datetime.now().strftime("%Y-%m-%d-%H:%M")
    return f"{typ}-{counters[key]:04d}-{ts}"


def set_foreign_id(alarm_id: str, foreign_id: str) -> bool:
    """Setzt die foreign_id eines Alarms per PATCH."""
    try:
        payload = {"Alarm": {"foreign_id": foreign_id}}
        response = requests.patch(
            f"{DIVERA_BASE_URL}/v2/alarms/{alarm_id}",
            params={"accesskey": DIVERA_API_KEY},
            json=payload,
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            logger.info(f"🏷️  foreign_id '{foreign_id}' gesetzt für Alarm {alarm_id}.")
            return True
        else:
            logger.warning(f"foreign_id setzen fehlgeschlagen für Alarm {alarm_id}: {data}")
            return False
    except Exception as e:
        logger.error(f"Fehler beim Setzen der foreign_id für Alarm {alarm_id}: {e}")
        return False


# ─────────────────────────────────────────────
# API FUNKTIONEN
# ─────────────────────────────────────────────

def get_active_alarms() -> list:
    """
    Ruft alle aktiven Alarmierungen von der Divera API ab.
    Endpoint: GET /api/v2/pull/alarm
    Gibt eine Liste von Alarm-Objekten zurück, oder [] bei Fehler.
    """
    url = f"{DIVERA_BASE_URL}/v2/pull/all"
    params = {
        "accesskey": DIVERA_API_KEY,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        # Divera API antwortet mit: { "success": true, "data": { "alarming": { "items": { id: {...} } } } }
        if data.get("success") and "data" in data:
            # Alarme stecken unter "alarm" -> "items" -> { id: {...} }
            alarm_section = data["data"].get("alarm", {})
            alarms_raw = alarm_section.get("items", {})

            # Sicherstellen dass es ein Dict ist und kein anderer Typ
            if not isinstance(alarms_raw, dict):
                alarms_raw = {}

            if not alarms_raw:
                logger.info("Keine aktiven Alarmierungen gefunden.")
                return []

            logger.info(f"📋 {len(alarms_raw)} aktive Alarmierung(en) gefunden.")
            # Alarm-Dict in Liste umwandeln und ID als Feld hinzufügen
            alarms = []
            for alarm_id, alarm_data in alarms_raw.items():
                if isinstance(alarm_data, dict):
                    alarm_data["id"] = alarm_id
                    # Debug: Felder ausgeben damit wir die Struktur sehen
                    logger.info(f"🔎 Alarm {alarm_id} Felder: { {k: v for k, v in alarm_data.items() if k in ['id','title','text','keyword','type','de_titel','de_text','foreign_id']} }")
                    alarms.append(alarm_data)
            return alarms
        else:
            logger.warning(f"API-Antwort ohne Erfolg: {data}")
            return []

    except requests.exceptions.ConnectionError:
        logger.error("Netzwerkfehler – keine Verbindung zur Divera API.")
        return []
    except requests.exceptions.Timeout:
        logger.error("Timeout bei der Divera API Anfrage.")
        return []
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP-Fehler von der API: {e}")
        return []
    except Exception as e:
        logger.error(f"Unbekannter Fehler beim Abrufen der Alarme: {e}")
        return []


def close_alarm(alarm_id: str) -> bool:
    """
    Schließt und archiviert eine Alarmierung über die Divera API.
    Schritt 1: Alarm schließen (closed: True)
    Schritt 2: Alarm archivieren (archived: True)
    """
    # Erst den Alarm laden um die originalen Empfänger zu übernehmen
    try:
        r = requests.get(f"{DIVERA_BASE_URL}/v2/alarms/{alarm_id}", params={"accesskey": DIVERA_API_KEY}, timeout=15)
        original = r.json().get("data", {})
    except Exception:
        original = {}

    empfaenger = {
        "notification_type": original.get("notification_type", 4),
        "user_cluster_relation": original.get("user_cluster_relation", []),
        "group": original.get("group", []),
        "cluster": original.get("cluster", []),
    }

    # ── Schritt 1: Schließen ──
    try:
        payload_close = {"Alarm": {"closed": True, **empfaenger}}
        response = requests.patch(f"{DIVERA_BASE_URL}/v2/alarms/{alarm_id}", params={"accesskey": DIVERA_API_KEY}, json=payload_close, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            logger.warning(f"Schließen fehlgeschlagen für Alarm {alarm_id}: {data}")
            return False
        logger.info(f"✅ Alarm {alarm_id} erfolgreich geschlossen.")
    except Exception as e:
        logger.error(f"Fehler beim Schließen von Alarm {alarm_id}: {e}")
        return False

    # ── Schritt 2: Archivieren ──
    try:
        response = requests.post(f"{DIVERA_BASE_URL}/v2/alarms/archive/{alarm_id}", params={"accesskey": DIVERA_API_KEY}, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            logger.warning(f"Archivieren fehlgeschlagen für Alarm {alarm_id}: {data}")
            return False
        logger.info(f"🗂️  Alarm {alarm_id} erfolgreich archiviert.")
    except Exception as e:
        logger.error(f"Fehler beim Archivieren von Alarm {alarm_id}: {e}")
        return False

    return True


# ─────────────────────────────────────────────
# PROBEALARM-ERKENNUNG
# ─────────────────────────────────────────────

def is_probe_alarm(alarm: dict) -> bool:
    """
    Prüft anhand von Keywords, ob es sich um einen Probealarm handelt.
    Durchsucht: Stichwort, Schlagwort, Meldebild, Text/Beschreibung.
    """
    # Alle relevanten Felder sammeln und zu einem String zusammenführen
    search_fields = [
        alarm.get("title", ""),            # Stichwort / Titel
        alarm.get("text", ""),             # Freitext / Beschreibung
        alarm.get("keyword", ""),          # Einsatz-Stichwort (ältere API-Versionen)
        alarm.get("address", ""),          # Adresse (selten relevant, aber dabei)
        alarm.get("de_titel", ""),         # Alternativer Titel
        alarm.get("de_text", ""),          # Alternativer Text
    ]

    combined_text = " ".join(str(f) for f in search_fields).lower()

    for keyword in PROBEALARM_KEYWORDS:
        if keyword.lower() in combined_text:
            logger.info(
                f"🔍 Probealarm erkannt (Keyword: '{keyword}') "
                f"in Alarm ID {alarm.get('id', '?')} | Titel: '{alarm.get('title', '-')}'"
            )
            return True

    return False


# ─────────────────────────────────────────────
# HAUPTLOGIK
# ─────────────────────────────────────────────

def process_alarms():
    """
    Hauptfunktion: Alarme abrufen, Probealarme erkennen,
    nach Ablauf der Wartezeit automatisch schließen.
    """
    alarms = get_active_alarms()

    if not alarms:
        logger.debug("Keine aktiven Alarmierungen gefunden.")
        return

    logger.info(f"📋 {len(alarms)} aktive Alarmierung(en) gefunden.")

    now = datetime.now()

    for alarm in alarms:
        alarm_id = str(alarm.get("id", ""))

        if not alarm_id:
            continue

        # Bereits geschlossene Alarme überspringen
        if alarm_id in already_closed:
            continue

        # foreign_id setzen falls leer
        foreign_id_aktuell = str(alarm.get("foreign_id", "")).strip()

        # Ist es ein Probealarm?
        if is_probe_alarm(alarm):
            # foreign_id setzen falls noch nicht vorhanden
            if not foreign_id_aktuell and alarm_id not in already_tagged:
                fid = next_foreign_id("P")
                if set_foreign_id(alarm_id, fid):
                    already_tagged.add(alarm_id)

            if alarm_id not in pending_probe_alarms:
                # Erstmalig erkannt → Zeitstempel speichern
                pending_probe_alarms[alarm_id] = now
                logger.info(
                    f"⏳ Probealarm {alarm_id} vorgemerkt. "
                    f"Wird in {AUTO_CLOSE_AFTER_MINUTES} Minuten automatisch geschlossen."
                )
            else:
                # Bereits vorgemerkt → Zeit prüfen
                erkannt_um = pending_probe_alarms[alarm_id]
                minuten_vergangen = (now - erkannt_um).total_seconds() / 60

                logger.info(
                    f"⏱️  Probealarm {alarm_id} wartet seit "
                    f"{minuten_vergangen:.1f} Minuten (Limit: {AUTO_CLOSE_AFTER_MINUTES} Min.)"
                )

                if minuten_vergangen >= AUTO_CLOSE_AFTER_MINUTES:
                    logger.info(f"🚨 Schließe Probealarm {alarm_id} automatisch...")
                    success = close_alarm(alarm_id)
                    if success:
                        already_closed.add(alarm_id)
                        del pending_probe_alarms[alarm_id]
        else:
            # Echter Alarm – foreign_id setzen falls leer, dann nur loggen
            if not foreign_id_aktuell and alarm_id not in already_tagged:
                fid = next_foreign_id("E")
                if set_foreign_id(alarm_id, fid):
                    already_tagged.add(alarm_id)
            logger.info(
                f"🚒 Echter Einsatz erkannt: ID {alarm_id} | "
                f"Titel: '{alarm.get('title', '-')}' → wird nicht automatisch geschlossen."
            )


def cleanup_pending():
    """
    Entfernt veraltete Einträge aus pending_probe_alarms,
    falls ein Alarm zwischenzeitlich manuell geschlossen wurde.
    """
    if not pending_probe_alarms:
        return
    # Wird bei jeder Runde mit get_active_alarms verglichen – hier einfach loggen
    logger.debug(f"Aktuell ausstehende Probealarme: {list(pending_probe_alarms.keys())}")


# ─────────────────────────────────────────────
# HAUPTPROGRAMM
# ─────────────────────────────────────────────

def main():
    logger.info("=" * 55)
    logger.info("  Divera 24/7 – Probealarm Auto-Closer gestartet")
    logger.info(f"  Poll-Intervall:     {POLL_INTERVAL_SECONDS} Sekunden")
    logger.info(f"  Auto-Close nach:    {AUTO_CLOSE_AFTER_MINUTES} Minuten")
    logger.info(f"  Probe-Keywords:     {', '.join(PROBEALARM_KEYWORDS)}")
    logger.info("=" * 55)

    # Prüfen ob API-Key gesetzt wurde
    if DIVERA_API_KEY == "DEIN_API_KEY_HIER":
        logger.error("❌ Kein API-Key konfiguriert! Bitte DIVERA_API_KEY in der Konfiguration eintragen.")
        sys.exit(1)

    while True:
        try:
            process_alarms()
            cleanup_pending()
        except KeyboardInterrupt:
            logger.info("Script durch Benutzer beendet (CTRL+C).")
            break
        except Exception as e:
            logger.error(f"Unerwarteter Fehler in der Hauptschleife: {e}")

        logger.debug(f"Warte {POLL_INTERVAL_SECONDS} Sekunden bis zur nächsten Abfrage...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
