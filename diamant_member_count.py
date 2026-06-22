#!/usr/bin/env python3
"""
DIAMANT Mitgliederzaehler -> Discord Kanalname

Liest die oeffentliche Mitgliederzahl von der RSI-Orgseite
(robertsspaceindustries.com/en/orgs/diamant) aus und schreibt sie
in den Namen eines Discord-Kanals (am besten ein Voice-Kanal).

Gedacht zum Laufen in GitHub Actions (siehe .github/workflows/
diamant-member-count.yml) - DISCORD_BOT_TOKEN und DISCORD_CHANNEL_ID
werden dort als Repository Secrets gesetzt und als Umgebungsvariablen
hereingereicht. Kein eigener Server noetig.
"""

import os
import re
import sys
import requests

# --- Konfiguration -----------------------------------------------------
ORG_URL = "https://robertsspaceindustries.com/en/orgs/diamant"
# Token & Channel-ID kommen aus Umgebungsvariablen (z.B. GitHub Secrets),
# damit sie nicht im Code/Repo landen.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")
CHANNEL_NAME_TEMPLATE = "👥 Mitglieder: {count}"
# ------------------------------------------------------------------------

HEADERS_RSI = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DiamantMemberBot/1.0"
}


def get_member_count() -> int:
    resp = requests.get(ORG_URL, headers=HEADERS_RSI, timeout=15)
    resp.raise_for_status()
    match = re.search(
        r'<span class="count">\s*([\d.,]+)\s*members?</span>', resp.text
    )
    if not match:
        raise RuntimeError(
            "Mitgliederzahl nicht gefunden - hat sich das HTML der RSI-Seite geändert?"
        )
    raw = match.group(1).replace(".", "").replace(",", "")
    return int(raw)


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
    if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
        print(
            "DISCORD_BOT_TOKEN und/oder DISCORD_CHANNEL_ID sind nicht gesetzt. "
            "Als Umgebungsvariablen bzw. GitHub Secrets hinterlegen.",
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
