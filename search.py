"""Recherche de prospects sur LinkedIn via Unipile (lecture seule : rien n'est envoyé).

Le résultat est un fichier CSV au format d'import de ProspectIA. Les messages sont vides et le score à 0 :
une fois importés, ces prospects restent « à vérifier » tant que ProspectIA (ou vous) n'a pas rédigé
les messages et validé le poste. Aucun envoi automatique ne part d'une simple recherche.

RGPD : on ne garde que le nom, le titre affiché, le lieu et l'URL du profil (minimisation),
avec la source « recherche LinkedIn » et la date. Jamais d'email ni de téléphone.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db

CSV_COLUMNS = ["full_name", "title", "company", "project", "score", "role_verified", "linkedin_url", "email",
               "source_label", "channel", "note", "first", "followup1", "followup2", "location", "network_distance",
               "provider_id"]

_COMPANY_RE = re.compile(r"\s(?:chez|at|@|-|–|—|\|)\s+(.+)$", re.I)


def company_from_headline(headline: str) -> str:
    """« Directrice RSE chez Alpha » -> « Alpha ». Vide si rien de fiable."""
    m = _COMPANY_RE.search(headline or "")
    return m.group(1).strip()[:80] if m else ""


def _first(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def _items(res: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "data", "results", "people"):
        if isinstance(res.get(key), list):
            return res[key]
    return []


def _next_cursor(res: dict[str, Any]) -> str | None:
    paging = res.get("paging") if isinstance(res.get("paging"), dict) else {}
    return _first(res.get("next_cursor"), res.get("cursor"), paging.get("cursor"))


def to_prospect(item: dict[str, Any]) -> dict[str, Any] | None:
    """Transforme un résultat Unipile en ligne de prospect. Renvoie None si ce n'est pas une personne exploitable."""
    provider_id = _first(item.get("id"), item.get("provider_id"))
    url = _first(item.get("public_profile_url"), item.get("profile_url"), item.get("url"))
    slug = _first(item.get("public_identifier"), item.get("public_id"), db.public_identifier_from_url(url))
    if not slug and provider_id:
        slug = provider_id
    if not slug:
        return None
    name = _first(item.get("display_name"), item.get("name"),
                  " ".join(x for x in (item.get("first_name"), item.get("last_name")) if x))
    if not name or name.strip().lower() in {"linkedin member", "membre de linkedin", "utilisateur linkedin"}:
        return None  # profils masqués : on ne les garde pas
    current = item.get("current_position") if isinstance(item.get("current_position"), dict) else {}
    headline = str(_first(current.get("role"), item.get("headline"), ""))
    loc = item.get("location")
    return {
        "full_name": name.strip(),
        "title": headline,
        "company": str(_first(current.get("company"), item.get("company"), company_from_headline(headline), "")),
        "linkedin_url": f"https://www.linkedin.com/in/{slug}/",
        "location": loc if isinstance(loc, str) else (loc or {}).get("name", "") if isinstance(loc, dict) else "",
        "network_distance": str(_first(item.get("network_distance"), item.get("distance"), "")),
        "provider_id": provider_id or "",
    }


def run_search(client, conn: sqlite3.Connection, *, url: str | None, keywords: str | None, limit: int,
               project: str, out_dir: str = ".") -> dict[str, Any]:
    found: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []
    cursor, pages = None, 0
    note = ""
    while len(found) < limit and pages < 10:
        try:
            res = client.search_people(url=url, keywords=keywords, cursor=cursor)
        except Exception as exc:  # pagination non supportée, etc. : on garde ce qu'on a
            if pages == 0:
                raise
            note = f"pagination arrêtée : {exc}"
            break
        raw_pages.append(res)
        pages += 1
        for item in _items(res):
            p = to_prospect(item)
            if p and all(p["linkedin_url"] != f["linkedin_url"] for f in found):
                found.append(p)
        cursor = _next_cursor(res)
        if not cursor or not _items(res):
            break
    found = found[:limit]

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    Path(out_dir, "recherche_brute.json").write_text(json.dumps(raw_pages, ensure_ascii=False, indent=1), "utf-8")

    rows, already, blocked = [], 0, 0
    for p in found:
        slug = db.public_identifier_from_url(p["linkedin_url"])
        if db.is_suppressed(conn, linkedin_slug=slug):
            blocked += 1          # liste STOP : on ne garde même pas la ligne
            continue
        if conn.execute("SELECT 1 FROM prospects WHERE public_identifier = ?", (slug,)).fetchone():
            already += 1
            continue
        rows.append({**{c: "" for c in CSV_COLUMNS}, **{k: v for k, v in p.items() if k in CSV_COLUMNS},
                     "project": project, "score": 0, "role_verified": "non", "channel": "invite",
                     "source_label": f"recherche LinkedIn du {datetime.now():%d/%m/%Y}"})
    out = Path(out_dir, f"resultats_recherche_{stamp}.csv")
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    db.log_event(conn, "search", detail=f"{len(found)} trouvés, {len(rows)} nouveaux ({url or keywords})"[:300])
    return {"found": found, "new": rows, "already": already, "blocked": blocked, "file": str(out),
            "pages": pages, "note": note}
