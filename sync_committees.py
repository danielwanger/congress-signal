"""
CongressSignal — Committee-Sync
----------------------------------
Lädt die aktuellen Ausschuss-Mitgliedschaften des Repräsentantenhauses
von der öffentlichen, community-gepflegten Quelle
unitedstates/congress-legislators (GitHub, Public Domain) und filtert
sie auf die Ausschüsse, für die wir ein Branchen-Mapping haben
(siehe data/committee_sector_map.json).

Läuft unabhängig vom Haupt-Tracker, empfohlen: wöchentlich per Cronjob,
da sich Ausschusszugehörigkeiten nur selten ändern.

Output: data/member_committees.json
  { "Dan Crenshaw": ["HSIF", "HSAS"], ... }

Bekannte Einschränkung: Matching erfolgt über den vollen Namen
("Vorname Nachname") aus der YAML-Quelle gegen den Namen aus den
PTR-PDFs. Abweichende Schreibweisen (Spitznamen, Suffixe wie "Jr.")
können zu verpassten Matches führen — kein Bioguide-ID-Abgleich, da
die PTR-PDFs keine Bioguide-IDs enthalten.
"""

import json
import sys
from pathlib import Path

import requests
import yaml

MEMBERSHIP_URL = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/committee-membership-current.yaml"

DATA_DIR = Path(__file__).parent / "data"
SECTOR_MAP_FILE = DATA_DIR / "committee_sector_map.json"
OUTPUT_FILE = DATA_DIR / "member_committees.json"


def load_tracked_committee_ids() -> set:
    if not SECTOR_MAP_FILE.exists():
        sys.exit(f"Fehlt: {SECTOR_MAP_FILE} — bitte zuerst anlegen.")
    with open(SECTOR_MAP_FILE) as f:
        sector_map = json.load(f)
    return set(sector_map.keys())


def fetch_membership() -> dict:
    response = requests.get(MEMBERSHIP_URL, timeout=30)
    response.raise_for_status()
    return yaml.safe_load(response.text)


def build_member_committees(membership: dict, tracked_ids: set) -> dict:
    """
    Baut eine Zuordnung Name -> Liste von Committee-IDs, aber NUR für
    die Ausschüsse aus tracked_ids (unsere gemappten Ausschüsse) und
    NUR für Voll-Ausschüsse (keine Unterausschüsse — deren IDs sind
    länger als 4 Zeichen, z.B. "HSIF14" statt "HSIF").
    """
    result: dict[str, list[str]] = {}
    for committee_id, members in membership.items():
        if committee_id not in tracked_ids:
            continue  # nicht gemappter Ausschuss oder Unterausschuss
        for member in members:
            name = member.get("name")
            if not name:
                continue
            result.setdefault(name, []).append(committee_id)
    return result


def main() -> None:
    tracked_ids = load_tracked_committee_ids()
    membership = fetch_membership()
    member_committees = build_member_committees(membership, tracked_ids)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(member_committees, indent=2, ensure_ascii=False))

    print(f"{len(member_committees)} Mitglieder in getrackten Ausschüssen gefunden.")
    print(f"Geschrieben nach: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()