#!/usr/bin/env python3
"""
DIAMANT Mitgliederliste mit Discord-Nickname-Abgleich

Holt die komplette RSI-Mitgliederliste der Org DIAMANT, vergleicht sie
mit dem letzten bekannten Stand (neue/ausgetretene Mitglieder), gleicht
sie optional mit den echten Discord-Servermitgliedern ab (Fuzzy-Match
auf den Nickname >= 70%) und postet:
  1. eine Zusammenfassung (Embed) + vollstaendige CSV als Anhang
  2. eine oder mehrere Monospace-Tabellen-Nachrichten mit allen
     RSI<->Discord-Verknuepfungen (manuell oder vorgeschlagen)
in einen (idealerweise privaten) Discord-Textkanal via Webhook. Alle
Nachrichten werden bei jedem Lauf bearbeitet statt neu gepostet.

Zusaetzlich: fuer jeden manuell bestaetigten (= eindeutig aufgeloesten)
Discord-Nickname wird automatisch eine "Zertifiziert"-Rolle vergeben,
und bei Wegfall der Zuordnung wieder entzogen.

ALLES in EINER Datei: data/members.csv
  Spalten: rsi_handle, display, rank, roles,
           discord_nickname_manual, discord_nickname_suggested,
           match_prozent, zertifiziert

  - discord_nickname_manual wird NUR vom Menschen gepflegt (direkt in
    GitHub editieren). Das Skript liest diese Spalte vor jedem Lauf ein
    und schreibt sie unveraendert zurueck - sie wird NIE automatisch
    ueberschrieben.
  - Alle anderen Spalten werden bei jedem Lauf frisch berechnet.

Automatisch generiert/aktualisiert: data/state.json, data/members.csv

Benoetigte Secrets (Umgebungsvariablen):
  SC_API_KEY                        - starcitizen-api.com Key
  DISCORD_BOT_TOKEN                 - fuer das Lesen der Discord-Mitgliederliste
  DISCORD_GUILD_ID                  - die DIAMANT Server-ID
  DISCORD_MEMBERLIST_WEBHOOK_URL     - Webhook des Ziel-Textkanals
  DISCORD_VERIFIED_ROLE_ID           - optional, ID der "Zertifiziert"-Rolle

Wichtig: Fuer den Discord-Mitgliederabgleich muss im Developer Portal
unter Bot -> "Server Members Intent" aktiviert sein. Fuer die Rollen-
vergabe braucht der Bot zusaetzlich "Rollen verwalten" und seine eigene
Rolle muss in der Hierarchie UEBER der Zertifiziert-Rolle stehen.
"""

import csv
import difflib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Konfiguration -------------------------------------------------------
ORG_SID = "DIAMANT"
SC_API_KEY = os.environ.get("SC_API_KEY", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
WEBHOOK_URL = os.environ.get("DISCORD_MEMBERLIST_WEBHOOK_URL", "")
CERTIFIED_ROLE_ID = os.environ.get("DISCORD_VERIFIED_ROLE_ID", "")

FUZZY_THRESHOLD = 0.60


def round_to_5(ratio: float) -> int:
    """Rundet einen Match-Anteil (0.0-1.0) auf die naechsten 5 Prozentpunkte."""
    return round(ratio * 100 / 5) * 5

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "state.json"
MEMBERS_FILE = DATA_DIR / "members.csv"
MAX_SHOWN_IN_EMBED = 10
TABLE_CHUNK_MAX_CHARS = 1850  # Sicherheitsabstand zum 2000-Zeichen-Limit
CSV_FIELDS = [
    "rsi_handle", "display", "rank", "roles",
    "discord_nickname_manual", "discord_nickname_suggested",
    "match_prozent", "zertifiziert",
]
# --------------------------------------------------------------------------


def fetch_rsi_members() -> dict:
    """Komplette RSI-Mitgliederliste, paginiert. Rueckgabe: handle -> info."""
    members = {}
    page = 1
    while True:
        url = (
            f"https://api.starcitizen-api.com/{SC_API_KEY}"
            f"/v1/live/organization_members/{ORG_SID}"
        )
        resp = requests.get(
            url,
            params={"page": page},
            headers={"Accept": "application/json"},
            timeout=45,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") != 1:
            raise RuntimeError(f"starcitizen-api.com meldet einen Fehler: {data}")
        page_members = data.get("data") or []
        if not page_members:
            break
        for m in page_members:
            handle = m.get("handle")
            if not handle:
                continue
            members[handle] = {
                "display": m.get("display") or handle,
                "rank": m.get("rank") or "",
                "roles": m.get("roles") or [],
            }
        page += 1
        if page > 60:  # Sicherheitsbremse
            break
        time.sleep(1)  # kleine Pause nach JEDER Seite (gegen RSI-Blocks)
        if len(members) % 300 == 0:
            time.sleep(3)  # zusaetzliche Sicherheitspause alle ~300 Eintraege
    return members


def fetch_org_total_count() -> int:
    """Holt die offizielle Gesamtmitgliederzahl zur Plausibilitaetskontrolle
    (separat von der paginierten Liste, gleicher Endpunkt wie der
    stuendliche Mitgliederzaehler)."""
    url = f"https://api.starcitizen-api.com/{SC_API_KEY}/v1/live/organization/{ORG_SID}"
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=45)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success") != 1:
        raise RuntimeError(f"starcitizen-api.com meldet einen Fehler: {data}")
    return int(data["data"]["members"])


def fetch_discord_members() -> list:
    """Alle echten Discord-Servermitglieder mit Anzeigenamen und Rollen.

    Benoetigt die GUILD_MEMBERS privileged intent (Developer Portal).
    Gibt bei fehlender Berechtigung eine leere Liste zurueck (Skript
    laeuft dann ohne Discord-Abgleich weiter).
    """
    if not DISCORD_BOT_TOKEN or not DISCORD_GUILD_ID:
        return []

    members = []
    after = "0"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    while True:
        url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/members"
        resp = requests.get(
            url, headers=headers, params={"limit": 1000, "after": after}, timeout=30
        )
        if not resp.ok:
            print(
                f"Warnung: Discord-Mitgliederliste konnte nicht geladen werden "
                f"({resp.status_code}: {resp.text}). Pruefe, ob 'Server Members "
                f"Intent' im Developer Portal aktiviert ist. Fahre ohne "
                f"Discord-Abgleich fort.",
                file=sys.stderr,
            )
            return []
        batch = resp.json()
        if not batch:
            break
        for m in batch:
            user = m.get("user") or {}
            names = [
                n
                for n in [m.get("nick"), user.get("global_name"), user.get("username")]
                if n
            ]
            if user.get("id"):
                members.append({
                    "id": user["id"],
                    "names": names,
                    "roles": m.get("roles") or [],
                })
        after = batch[-1]["user"]["id"]
        if len(batch) < 1000:
            break
    return members


def _read_text_robust(path: Path) -> str:
    """Liest eine Textdatei robust ein, auch wenn sie z.B. von Excel
    versehentlich in Windows-1252/ANSI statt UTF-8 gespeichert wurde."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def load_previous_manual_nicknames() -> dict:
    """Liest NUR die manuelle Spalte aus der bestehenden Datei.
    Wird unveraendert in den naechsten Lauf uebernommen."""
    mapping = {}
    if MEMBERS_FILE.exists():
        text = _read_text_robust(MEMBERS_FILE)
        for row in csv.DictReader(text.splitlines(), delimiter=";"):
            handle = (row.get("rsi_handle") or "").strip()
            nick = (row.get("discord_nickname_manual") or "").strip()
            if handle and nick:
                mapping[handle] = nick
    return mapping


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def best_fuzzy_match(candidates: list, discord_members: list):
    best = None
    best_ratio = 0.0
    for dm in discord_members:
        for name in dm["names"]:
            for cand in candidates:
                ratio = difflib.SequenceMatcher(None, cand.lower(), name.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = {"discord_id": dm["id"], "name": name, "match": round(ratio, 2)}
    if best and best_ratio >= FUZZY_THRESHOLD:
        return best
    return None


def resolve_manual_discord_ids(manual_nicknames: dict, discord_members: list) -> dict:
    """Loest manuelle Nickname-Eintraege zu eindeutigen Discord-User-IDs auf.
    Nur bei GENAU EINEM Treffer wird aufgeloest - sonst Warnung, keine Rolle."""
    name_index: dict = {}
    for dm in discord_members:
        for name in dm["names"]:
            name_index.setdefault(name.lower(), set()).add(dm["id"])

    resolved = {}
    for handle, nickname in manual_nicknames.items():
        ids = name_index.get(nickname.lower())
        if not ids:
            print(
                f"Warnung: Discord-Nickname '{nickname}' (fuer {handle}) aktuell "
                f"nicht im Server gefunden - Zertifiziert-Rolle wird nicht vergeben.",
                file=sys.stderr,
            )
        elif len(ids) > 1:
            print(
                f"Warnung: Discord-Nickname '{nickname}' (fuer {handle}) ist nicht "
                f"eindeutig ({len(ids)} Treffer) - Zertifiziert-Rolle wird nicht vergeben.",
                file=sys.stderr,
            )
        else:
            resolved[handle] = next(iter(ids))
    return resolved


def sync_certified_role(resolved_ids: dict, discord_members: list, previous_certified: list) -> list:
    """Vergibt/entzieht die Zertifiziert-Rolle anhand der aufgeloesten IDs.
    Gibt NUR die Discord-IDs zurueck, die die Rolle TATSAECHLICH (jetzt
    bestaetigt) tragen - nicht bloss "Name war eindeutig aufloesbar"."""
    if not CERTIFIED_ROLE_ID:
        if resolved_ids:
            print(
                "Warnung: DISCORD_VERIFIED_ROLE_ID ist nicht gesetzt - "
                "Zertifiziert-Rolle wird nicht vergeben/geprueft.",
                file=sys.stderr,
            )
        return previous_certified  # Feature nicht konfiguriert, nichts tun

    current_roles = {dm["id"]: set(dm.get("roles") or []) for dm in discord_members}
    target_ids = set(resolved_ids.values())
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    actually_certified = set()

    for discord_id in target_ids:
        if CERTIFIED_ROLE_ID in current_roles.get(discord_id, set()):
            actually_certified.add(discord_id)
            continue
        resp = _request_with_retry(
            "PUT",
            f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}"
            f"/members/{discord_id}/roles/{CERTIFIED_ROLE_ID}",
            headers=headers,
        )
        if resp.ok:
            actually_certified.add(discord_id)
        else:
            print(
                f"Warnung: Rolle konnte nicht vergeben werden ({discord_id}): "
                f"{resp.status_code} {resp.text}",
                file=sys.stderr,
            )
        time.sleep(3)

    for discord_id in previous_certified:
        if discord_id not in target_ids:
            resp = _request_with_retry(
                "DELETE",
                f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}"
                f"/members/{discord_id}/roles/{CERTIFIED_ROLE_ID}",
                headers=headers,
            )
            if not resp.ok and resp.status_code != 404:
                print(
                    f"Warnung: Rolle konnte nicht entzogen werden ({discord_id}): "
                    f"{resp.status_code} {resp.text}",
                    file=sys.stderr,
                )
            time.sleep(3)

    return sorted(actually_certified)


def write_members_csv(rsi_members: dict, manual_nicknames: dict, suggestions: dict,
                       resolved_discord_ids: dict, certified_ids: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    certified_set = set(certified_ids)
    with open(MEMBERS_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(CSV_FIELDS)
        for handle in sorted(rsi_members.keys(), key=str.lower):
            info = rsi_members[handle]
            manual = manual_nicknames.get(handle, "")
            suggestion = suggestions.get(handle)
            suggested_name = suggestion["name"] if suggestion else ""
            match_pct = f"{round_to_5(suggestion['match'])}%" if suggestion else ""
            zertifiziert = "✓" if resolved_discord_ids.get(handle) in certified_set else ""
            writer.writerow([
                handle, info["display"], info["rank"], ";".join(info["roles"]),
                manual, suggested_name, match_pct, zertifiziert,
            ])


def build_embed(total, joined, left, linked_count, open_count, reported_total=None) -> dict:
    fields = [
        {"name": "Gesamt (öffentlich sichtbar)", "value": str(total), "inline": True},
        {"name": "Mit Discord verknüpft", "value": str(linked_count), "inline": True},
        {"name": "📝 Noch offen (unbestätigt)", "value": str(open_count), "inline": True},
    ]
    if reported_total is not None and reported_total > total:
        hidden = reported_total - total
        fields.append({
            "name": "Hinweis",
            "value": f"{hidden} weitere Mitglieder haben ihre Org-Zugehörigkeit "
                     f"privat gestellt (offiziell {reported_total} gesamt) und "
                     f"erscheinen nie in dieser Liste.",
            "inline": False,
        })
    if joined:
        shown = ", ".join(joined[:MAX_SHOWN_IN_EMBED])
        if len(joined) > MAX_SHOWN_IN_EMBED:
            shown += f" (+{len(joined) - MAX_SHOWN_IN_EMBED} weitere)"
        fields.append({"name": f"🆕 Neu beigetreten ({len(joined)})", "value": shown, "inline": False})
    if left:
        shown = ", ".join(left[:MAX_SHOWN_IN_EMBED])
        if len(left) > MAX_SHOWN_IN_EMBED:
            shown += f" (+{len(left) - MAX_SHOWN_IN_EMBED} weitere)"
        fields.append({"name": f"👋 Ausgetreten ({len(left)})", "value": shown, "inline": False})

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "title": "📋 DIAMANT Mitgliederliste",
        "description": (
            f"Stand: {now}\nVollständige Liste in `data/members.csv` im Repo.\n"
            f"Noch offene Vorschläge (unbestätigt) in der Tabelle unten ⬇️"
        ),
        "color": 0x4DABF7,
        "fields": fields,
    }


def build_linked_rows(rsi_members: dict, manual_nicknames: dict, suggestions: dict) -> list:
    """Nur OFFENE Vorschlaege - bereits manuell bestaetigte Mitglieder
    werden hier ausgeblendet (stehen weiterhin in der CSV), damit die
    Discord-Tabelle sich auf das fokussiert, was noch eine Entscheidung
    braucht."""
    rows = []
    for handle in sorted(rsi_members.keys(), key=str.lower):
        if handle in manual_nicknames:
            continue  # schon geklaert - nicht mehr "offen"
        if handle in suggestions:
            s = suggestions[handle]
            rows.append((handle, s["name"], f"~{round_to_5(s['match'])}% Vorschlag"))
    return rows


def chunk_table(rows: list) -> list:
    """Teilt die Tabelle in mehrere Discord-Nachrichten-kompatible Bloecke."""
    if not rows:
        return ["_Noch keine Discord-Verknüpfungen vorhanden._"]

    header = f"{'RSI-Handle':<22}{'Discord-Nickname':<24}{'Status'}\n" + "-" * 64 + "\n"
    chunks = []
    current = header
    for handle, nickname, status in rows:
        line = f"{handle:<22}{nickname:<24}{status}\n"
        if len(current) + len(line) > TABLE_CHUNK_MAX_CHARS:
            chunks.append(current)
            current = header
        current += line
    chunks.append(current)
    return chunks


def _request_with_retry(method: str, url: str, max_retries: int = 5, **kwargs):
    """Fuehrt einen Request aus und wartet/wiederholt automatisch bei 429."""
    for attempt in range(max_retries):
        resp = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 1)
            time.sleep(retry_after + 0.1)
            continue
        return resp
    return resp  # letzter Versuch, auch wenn wieder 429


def post_or_edit_message(payload: dict, message_id, files=None):
    """Postet eine neue Nachricht oder bearbeitet eine bestehende per ID.
    Gibt die (ggf. neue) Message-ID zurueck. Wartet automatisch bei
    Rate-Limits (429) statt sofort abzubrechen."""
    data = {"payload_json": json.dumps(payload)} if files else None
    json_body = None if files else payload

    if message_id:
        resp = _request_with_retry(
            "PATCH", f"{WEBHOOK_URL}/messages/{message_id}",
            data=data, json=json_body, files=files,
        )
        if resp.status_code == 404:
            message_id = None
        elif not resp.ok:
            raise RuntimeError(f"Discord Webhook Fehler {resp.status_code}: {resp.text}")
        else:
            return message_id

    resp = _request_with_retry(
        "POST", f"{WEBHOOK_URL}?wait=true", data=data, json=json_body, files=files,
    )
    if not resp.ok:
        raise RuntimeError(f"Discord Webhook Fehler {resp.status_code}: {resp.text}")
    return resp.json()["id"]


def sync_table_messages(chunks: list, previous_ids: list) -> list:
    new_ids = []
    for i, chunk in enumerate(chunks):
        chunk_content = f"```\n{chunk}\n```"
        existing = previous_ids[i] if i < len(previous_ids) else None
        msg_id = post_or_edit_message({"content": chunk_content}, existing)
        new_ids.append(msg_id)
        time.sleep(3)  # Sicherheitsabstand gegen Discord-Rate-Limits

    for old_id in previous_ids[len(chunks):]:
        try:
            _request_with_retry("DELETE", f"{WEBHOOK_URL}/messages/{old_id}")
        except requests.RequestException:
            pass
        time.sleep(3)
    return new_ids


def main() -> int:
    missing = [
        n for n, v in [("SC_API_KEY", SC_API_KEY), ("DISCORD_MEMBERLIST_WEBHOOK_URL", WEBHOOK_URL)]
        if not v
    ]
    if missing:
        print(f"Folgende Secrets fehlen: {', '.join(missing)}", file=sys.stderr)
        return 1

    manual_nicknames = load_previous_manual_nicknames()

    try:
        rsi_members = fetch_rsi_members()
    except Exception as exc:
        print(f"Fehler beim Abrufen der RSI-Mitgliederliste: {exc}", file=sys.stderr)
        return 1

    # Offizielle Gesamtzahl ist NUR zur Information gedacht (Org-Mitglieder
    # koennen ihre Zugehoerigkeit privat stellen - die erscheinen NIE in der
    # paginierten Liste, das ist eine dauerhafte RSI-Eigenheit, kein Fehler).
    try:
        reported_total = fetch_org_total_count()
    except Exception as exc:
        print(f"Warnung: Gesamtzahl zur Information nicht abrufbar: {exc}", file=sys.stderr)
        reported_total = None

    # Plausibilitaetscheck stattdessen gegen den letzten ERFOLGREICHEN Lauf:
    # ein ploetzlicher starker Einbruch (z.B. RSI hat den Abruf zwischendurch
    # geblockt) faellt damit auf, ohne durch die strukturelle Luecke zur
    # offiziellen Gesamtzahl falsch auszuloesen.
    previous_state_for_check = load_state()
    previous_count = len(previous_state_for_check.get("handles", []))
    if previous_count > 0 and len(rsi_members) < previous_count * 0.90:
        print(
            f"Fehler: Nur {len(rsi_members)} Mitglieder abgerufen, letzter "
            f"erfolgreicher Lauf hatte {previous_count} (>10% Einbruch). "
            f"Vermutlich ein unvollstaendiger Abruf (RSI hat zwischendurch "
            f"geblockt). Breche ab, OHNE etwas zu speichern oder zu posten. "
            f"Bitte den Lauf einfach erneut starten.",
            file=sys.stderr,
        )
        return 1

    discord_members = fetch_discord_members()

    # Discord-Konten, die schon eindeutig manuell zugeordnet sind, fliegen
    # aus dem Vorschlagspool - derselbe Discord-Account soll nicht gleichzeitig
    # als Vorschlag fuer einen ANDEREN RSI-Handle auftauchen.
    resolved_discord_ids = resolve_manual_discord_ids(manual_nicknames, discord_members)
    claimed_ids = set(resolved_discord_ids.values())
    unclaimed_discord_members = [dm for dm in discord_members if dm["id"] not in claimed_ids]

    suggestions = {}
    for handle, info in rsi_members.items():
        if handle in manual_nicknames:
            continue
        match = best_fuzzy_match([handle, info["display"]], unclaimed_discord_members)
        if match:
            suggestions[handle] = match

    state = load_state()
    previous_handles = set(state.get("handles", []))
    current_handles = set(rsi_members.keys())
    joined = sorted(current_handles - previous_handles, key=str.lower)
    left = sorted(previous_handles - current_handles, key=str.lower)
    linked_count = sum(1 for h in rsi_members if h in manual_nicknames or h in suggestions)

    certified_ids = sync_certified_role(
        resolved_discord_ids, discord_members, state.get("certified_discord_ids", [])
    )

    write_members_csv(rsi_members, manual_nicknames, suggestions, resolved_discord_ids, certified_ids)
    rows = build_linked_rows(rsi_members, manual_nicknames, suggestions)
    embed = build_embed(len(rsi_members), joined, left, linked_count, len(rows), reported_total)

    try:
        summary_message_id = post_or_edit_message(
            {"embeds": [embed]},
            state.get("summary_message_id"),
        )

        chunks = chunk_table(rows)
        table_message_ids = sync_table_messages(chunks, state.get("table_message_ids", []))
    except Exception as exc:
        print(f"Fehler beim Posten in Discord: {exc}", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "handles": sorted(current_handles),
                "summary_message_id": summary_message_id,
                "table_message_ids": table_message_ids,
                "certified_discord_ids": certified_ids,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"Fertig: {len(rsi_members)} Mitglieder, {len(joined)} neu, "
        f"{len(left)} ausgetreten, {linked_count} mit Discord verknüpft, "
        f"{len(certified_ids)} zertifiziert, {len(table_message_ids)} Tabellen-Nachricht(en)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
