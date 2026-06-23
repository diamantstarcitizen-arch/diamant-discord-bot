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

Manuelle Nickname-Zuordnungen: data/manual_nicknames.csv von Hand pflegen
(Spalten: rsi_handle, discord_nickname). Diese Datei wird von diesem
Skript nur GELESEN, niemals automatisch ueberschrieben.

Automatisch generiert/aktualisiert: data/state.json, data/member_list.csv

Benoetigte Secrets (Umgebungsvariablen):
  SC_API_KEY                        - starcitizen-api.com Key
  DISCORD_BOT_TOKEN                 - fuer das Lesen der Discord-Mitgliederliste
  DISCORD_GUILD_ID                  - die DIAMANT Server-ID
  DISCORD_MEMBERLIST_WEBHOOK_URL    - Webhook des Ziel-Textkanals

Wichtig: Fuer den Discord-Mitgliederabgleich muss im Developer Portal
unter Bot -> "Server Members Intent" aktiviert sein, sonst schlaegt der
Discord-Teil mit einem Fehler fehl (der RSI-Teil funktioniert trotzdem).
"""

import csv
import difflib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Konfiguration -------------------------------------------------------
ORG_SID = "DIAMANT"
SC_API_KEY = os.environ.get("SC_API_KEY", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
WEBHOOK_URL = os.environ.get("DISCORD_MEMBERLIST_WEBHOOK_URL", "")

FUZZY_THRESHOLD = 0.70

DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "state.json"
MANUAL_FILE = DATA_DIR / "manual_nicknames.csv"
CSV_EXPORT_FILE = DATA_DIR / "member_list.csv"
MAX_SHOWN_IN_EMBED = 10
TABLE_CHUNK_MAX_CHARS = 1850  # Sicherheitsabstand zum 2000-Zeichen-Limit
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
    return members


def fetch_discord_members() -> list:
    """Alle echten Discord-Servermitglieder mit ihren Anzeigenamen.

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
                members.append({"id": user["id"], "names": names})
        after = batch[-1]["user"]["id"]
        if len(batch) < 1000:
            break
    return members


def load_manual_nicknames() -> dict:
    mapping = {}
    if MANUAL_FILE.exists():
        with open(MANUAL_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                handle = (row.get("rsi_handle") or "").strip()
                nick = (row.get("discord_nickname") or "").strip()
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


def write_csv_export(rsi_members: dict, manual_nicknames: dict, suggestions: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CSV_EXPORT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["rsi_handle", "display", "rank", "roles", "discord_nickname",
             "discord_match_typ", "discord_match_prozent"]
        )
        for handle in sorted(rsi_members.keys(), key=str.lower):
            info = rsi_members[handle]
            manual = manual_nicknames.get(handle)
            suggestion = suggestions.get(handle)
            if manual:
                nickname, match_typ, match_pct = manual, "manuell", ""
            elif suggestion:
                nickname = suggestion["name"]
                match_typ = "vorgeschlagen"
                match_pct = f"{round(suggestion['match'] * 100)}%"
            else:
                nickname, match_typ, match_pct = "", "", ""
            writer.writerow(
                [handle, info["display"], info["rank"], ";".join(info["roles"]),
                 nickname, match_typ, match_pct]
            )


def build_embed(total, joined, left, linked_count) -> dict:
    fields = [
        {"name": "Gesamt", "value": str(total), "inline": True},
        {"name": "Mit Discord verknüpft", "value": str(linked_count), "inline": True},
    ]
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
        "description": f"Stand: {now}\nVollständige Liste im Anhang als CSV.\nVerknüpfte Discord-Nicknames in der Tabelle unten ⬇️",
        "color": 0x4DABF7,
        "fields": fields,
    }


def build_linked_rows(rsi_members: dict, manual_nicknames: dict, suggestions: dict) -> list:
    rows = []
    for handle in sorted(rsi_members.keys(), key=str.lower):
        if handle in manual_nicknames:
            rows.append((handle, manual_nicknames[handle], "✓ manuell"))
        elif handle in suggestions:
            s = suggestions[handle]
            rows.append((handle, s["name"], f"~{round(s['match'] * 100)}% Vorschlag"))
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


def post_or_edit_message(payload: dict, message_id, files=None):
    """Postet eine neue Nachricht oder bearbeitet eine bestehende per ID.
    Gibt die (ggf. neue) Message-ID zurueck."""
    data = {"payload_json": json.dumps(payload)} if files else None
    json_body = None if files else payload

    if message_id:
        resp = requests.patch(
            f"{WEBHOOK_URL}/messages/{message_id}",
            data=data, json=json_body, files=files, timeout=30,
        )
        if resp.status_code == 404:
            message_id = None
        elif not resp.ok:
            raise RuntimeError(f"Discord Webhook Fehler {resp.status_code}: {resp.text}")
        else:
            return message_id

    resp = requests.post(
        f"{WEBHOOK_URL}?wait=true", data=data, json=json_body, files=files, timeout=30
    )
    if not resp.ok:
        raise RuntimeError(f"Discord Webhook Fehler {resp.status_code}: {resp.text}")
    return resp.json()["id"]


def sync_table_messages(chunks: list, previous_ids: list) -> list:
    new_ids = []
    for i, chunk in enumerate(chunks):
        content = f"```\n{chunk}\n```"
        existing = previous_ids[i] if i < len(previous_ids) else None
        msg_id = post_or_edit_message({"content": content}, existing)
        new_ids.append(msg_id)

    # Ueberzaehlige alte Tabellen-Nachrichten loeschen (Liste ist geschrumpft)
    for old_id in previous_ids[len(chunks):]:
        try:
            requests.delete(f"{WEBHOOK_URL}/messages/{old_id}", timeout=15)
        except requests.RequestException:
            pass
    return new_ids


def main() -> int:
    missing = [
        n for n, v in [("SC_API_KEY", SC_API_KEY), ("DISCORD_MEMBERLIST_WEBHOOK_URL", WEBHOOK_URL)]
        if not v
    ]
    if missing:
        print(f"Folgende Secrets fehlen: {', '.join(missing)}", file=sys.stderr)
        return 1

    try:
        rsi_members = fetch_rsi_members()
    except Exception as exc:
        print(f"Fehler beim Abrufen der RSI-Mitgliederliste: {exc}", file=sys.stderr)
        return 1

    discord_members = fetch_discord_members()
    manual_nicknames = load_manual_nicknames()

    suggestions = {}
    for handle, info in rsi_members.items():
        if handle in manual_nicknames:
            continue
        match = best_fuzzy_match([handle, info["display"]], discord_members)
        if match:
            suggestions[handle] = match

    state = load_state()
    previous_handles = set(state.get("handles", []))
    current_handles = set(rsi_members.keys())
    joined = sorted(current_handles - previous_handles, key=str.lower)
    left = sorted(previous_handles - current_handles, key=str.lower)
    linked_count = sum(1 for h in rsi_members if h in manual_nicknames or h in suggestions)

    write_csv_export(rsi_members, manual_nicknames, suggestions)
    embed = build_embed(len(rsi_members), joined, left, linked_count)

    try:
        with open(CSV_EXPORT_FILE, "rb") as f:
            csv_bytes = f.read()
        summary_message_id = post_or_edit_message(
            {"embeds": [embed]},
            state.get("summary_message_id"),
            files={"file": ("diamant_mitglieder.csv", csv_bytes, "text/csv")},
        )

        rows = build_linked_rows(rsi_members, manual_nicknames, suggestions)
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
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"Fertig: {len(rsi_members)} Mitglieder, {len(joined)} neu, "
        f"{len(left)} ausgetreten, {linked_count} mit Discord verknüpft, "
        f"{len(table_message_ids)} Tabellen-Nachricht(en)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
