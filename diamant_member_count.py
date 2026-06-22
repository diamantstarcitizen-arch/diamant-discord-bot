#!/usr/bin/env python3
"""
DIAMANT Mitgliederzaehler -> Discord Kanalname

Liest die Mitgliederzahl der Org DIAMANT ueber die starcitizen-api.com
ab (statt die RSI-Seite direkt zu scrapen - die blockt Anfragen aus
Rechenzentrums-IP-Bereichen wie GitHub Actions per Cloudflare) und
schreibt sie in den Namen eines Discord-Kanals (am besten ein Voice-Kanal).

Gedacht zum Laufen in GitHub Actions (siehe .github/workflows/
diamant-member-count.yml) - SC_API_KEY, DISCORD_BOT_TOKEN und
DISCORD_CHANNEL_ID werden dort als Repository Secrets gesetzt und als
Umgebungsvariablen hereingereicht. Kein eigener Server noetig.
"""

import os
import sys
import requests

# --- Konfiguration -----------------------------------------------------
ORG_SID = "DIAMANT"
# API-Key & Discord-Zugangsdaten kommen aus Umgebungsvariablen
# (z.B. GitHub Secrets), damit sie nicht im Code/Repo landen.
SC_API_KEY = os.environ.get("SC_API_KEY", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
CHANNEL_NAME_TEMPLATE = "👥 Mitglieder: {count}"
# ------------------------------------------------------------------------


def get_member_count() -> int:
    url = f"https://api.starcitizen-api.com/{SC_API_KEY}/v1/live/organization/{ORG_SID}"
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success") != 1:
        raise RuntimeError(f"starcitizen-api.com meldet einen Fehler: {data}")
    return int(data["data"]["members"])


def update_discord_channel(name: str) -> None:
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}"
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.patch(url, headers=headers, json={"name": name}, timeout=15)
    if resp.status_code == 429:
        retry_after = resp.json().get("retry_after", "?")
        raise RuntimeError(f"Discord Rate-Limit erreicht, retry_after={retry_after}s")
    resp.raise_for_status()


def main() -> int:
    missing = [
        n
        for n, v in [
            ("SC_API_KEY", SC_API_KEY),
            ("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN),
            ("DISCORD_CHANNEL_ID", DISCORD_CHANNEL_ID),
        ]
        if not v
    ]
    if missing:
        print(
            f"Folgende Umgebungsvariablen/Secrets fehlen: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    try:
        count = get_member_count()
    except Exception as exc:
        print(f"Fehler beim Auslesen der Mitgliederzahl: {exc}", file=sys.stderr)
        return 1

    new_name = CHANNEL_NAME_TEMPLATE.format(count=count)

    try:
        update_discord_channel(new_name)
    except Exception as exc:
        print(f"Fehler beim Aktualisieren des Discord-Kanals: {exc}", file=sys.stderr)
        return 1

    print(f"Kanal aktualisiert: {new_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
