"""Commandes de ProspectIA Outreach.

    python -m prospectia_outreach init
    python -m prospectia_outreach import prospects.csv
    python -m prospectia_outreach status
    python -m prospectia_outreach run-once
    python -m prospectia_outreach run --every 15
    python -m prospectia_outreach webhook --port 8000
    python -m prospectia_outreach setup-webhooks https://mon-adresse-publique
    python -m prospectia_outreach dashboard        (tableau de bord dans le navigateur)
    python -m prospectia_outreach replies
    python -m prospectia_outreach optout https://www.linkedin.com/in/jean-dupont
    python -m prospectia_outreach pause "raison" | resume
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import sys
import time
from pathlib import Path

from . import db, guards
from .config import Settings, load_settings
from .engine import OutreachEngine
from .unipile_client import DryRunClient, UnipileClient, UnipileV2Client

MESSAGE_COLUMNS = ("note", "first", "followup1", "followup2")


def make_client(s: Settings, real: bool = False):
    """real=True : vrai client même en mode simulation (pour les vérifications en lecture seule)."""
    if s.dry_run and not real:
        return DryRunClient()
    if s.unipile_version == "v1":
        return UnipileClient(s.unipile_dsn, s.unipile_api_key, s.unipile_account_id)
    return UnipileV2Client(s.unipile_api_key, s.unipile_account_id, s.unipile_base_url, s.unipile_inbox_id)


def set_env_value(key: str, value: str, path: str = ".env") -> None:
    """Écrit/remplace CLE=valeur dans .env sans toucher au reste."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    lines = [l for l in lines if not l.strip().startswith(f"{key}=")] + [f"{key}={value}"]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler("prospectia_outreach.log", encoding="utf-8")])


def _truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "oui", "vrai", "x"}


def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    with path.open(encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        return list(csv.DictReader(f, dialect=dialect))


def cmd_import(conn, s: Settings, path: str) -> None:
    added = dup = bad = 0
    for row in read_rows(Path(path)):
        try:
            pid = db.add_prospect(
                conn,
                full_name=row["full_name"].strip(),
                project=row.get("project", "A").strip().upper(),
                linkedin_url=row["linkedin_url"].strip(),
                title=row.get("title", ""), company=row.get("company", ""),
                score=int(float(row.get("score") or 0)), role_verified=_truthy(row.get("role_verified", "")),
                email=(row.get("email") or "").strip(), source_label=row.get("source_label", ""),
                channel=(row.get("channel") or "invite").strip().lower(),
                messages={k: row.get(k, "") for k in MESSAGE_COLUMNS},
                provider_id=(row.get("provider_id") or "").strip() or None,
            )
            if pid:
                added += 1
            else:
                dup += 1
        except (KeyError, ValueError) as exc:
            bad += 1
            print(f"  ligne ignorée ({exc}) : {row.get('full_name', '?')}")
    print(f"Import terminé : {added} ajoutés, {dup} doublons ou liste STOP, {bad} lignes invalides.")


def cmd_status(conn, s: Settings) -> None:
    now = db.utcnow()
    paused, reason = db.is_paused(conn)
    print(f"Mode : {'SIMULATION (rien n’est envoyé)' if s.dry_run else 'RÉEL'}")
    print(f"État : {'EN PAUSE — ' + reason if paused else 'actif'}")
    print(f"Heures ouvrées maintenant : {'oui' if guards.in_working_hours(now, s) else 'non'}")
    print(f"Invitations restantes aujourd'hui : {guards.invites_remaining(conn, now, s)} "
          f"(limite du jour avec montée en charge : {guards.invite_limit_today(conn, now, s)})")
    print(f"Messages restants aujourd'hui : {guards.messages_remaining(conn, now, s)}")
    print("\nProspects par statut :")
    for r in conn.execute("SELECT status, COUNT(*) n FROM prospects GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<12} {r['n']}")


def cmd_replies(conn) -> None:
    rows = conn.execute("SELECT p.full_name, p.company, p.linkedin_url, p.replied_at, "
                        "(SELECT detail FROM events e WHERE e.prospect_id = p.id AND e.kind='replied' "
                        " ORDER BY e.id DESC LIMIT 1) AS msg FROM prospects p WHERE status = ? "
                        "ORDER BY replied_at DESC", (db.REPLIED,)).fetchall()
    if not rows:
        print("Aucune réponse en attente.")
    for r in rows:
        print(f"- {r['full_name']} ({r['company']}) — {r['replied_at']}\n  {r['linkedin_url']}\n  « {r['msg']} »\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prospectia_outreach")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p_chk = sub.add_parser("check", help="vérifie la connexion Unipile (lecture seule)")
    p_chk.add_argument("identifier", nargs="?", help="fin de l'URL d'un profil LinkedIn (optionnel)")
    p_imp = sub.add_parser("import"); p_imp.add_argument("file")
    sub.add_parser("status")
    p_src = sub.add_parser("search", help="cherche des profils sur LinkedIn (lecture seule)")
    p_src.add_argument("--url", help="adresse d'une page de résultats de recherche LinkedIn (entre guillemets)")
    p_src.add_argument("--keywords", help="mots-clés, ex. \"directeur RSE\"")
    p_src.add_argument("--limit", type=int, default=25, help="nombre max de profils (25 par défaut, 100 max)")
    p_src.add_argument("--project", default="A", choices=["A", "B"])
    sub.add_parser("run-once")
    p_run = sub.add_parser("run"); p_run.add_argument("--every", type=int, default=15, help="minutes")
    p_web = sub.add_parser("webhook"); p_web.add_argument("--port", type=int, default=8000)
    p_set = sub.add_parser("setup-webhooks"); p_set.add_argument("public_url")
    sub.add_parser("replies")
    p_dash = sub.add_parser("dashboard"); p_dash.add_argument("--port", type=int, default=8001)
    p_opt = sub.add_parser("optout"); p_opt.add_argument("target", help="URL LinkedIn ou email")
    p_pause = sub.add_parser("pause"); p_pause.add_argument("reason", nargs="?", default="pause manuelle")
    sub.add_parser("resume")
    args = parser.parse_args(argv)

    setup_logging()
    s = load_settings()
    conn = db.connect(s.db_path)

    if args.cmd == "init":
        print(f"Base prête : {s.db_path}. Mode {'SIMULATION' if s.dry_run else 'RÉEL'}.")
    elif args.cmd == "search":
        from .search import run_search
        if not (args.url or args.keywords):
            sys.exit("Donnez --url \"https://www.linkedin.com/search/results/people/?...\" ou --keywords \"...\"")
        if args.url and "linkedin.com/search/results/people" not in args.url:
            sys.exit("L'adresse doit être une page de résultats de recherche de PERSONNES LinkedIn "
                     "(elle contient linkedin.com/search/results/people).")
        done_today = db.count_events(conn, ("search",), guards.start_of_day(db.utcnow(), s))
        if done_today >= s.max_searches_per_day:
            sys.exit(f"Quota atteint : {done_today} recherches aujourd'hui (MAX_SEARCHES_PER_DAY={s.max_searches_per_day}).")
        r = run_search(make_client(s, real=True), conn, url=args.url, keywords=args.keywords,
                       limit=max(1, min(args.limit, 100)), project=args.project)
        print(f"\n{len(r['found'])} profils trouvés ({r['pages']} page(s)) :\n")
        for i, p in enumerate(r["found"], 1):
            print(f"{i:>3}. {p['full_name']} — {p['title'][:70]}  [{p['location']}]")
            print(f"     {p['linkedin_url']}")
        print(f"\n➡️  {len(r['new'])} nouveaux enregistrés dans : {r['file']}")
        print(f"   ({r['already']} déjà dans la base, {r['blocked']} en liste STOP, ignorés)")
        if r["note"]:
            print(f"   Remarque : {r['note']}")
        print("   Rien n'a été envoyé. Ces profils ne partiront qu'après rédaction des messages et validation.")
        if not r["found"]:
            print("   Aucun résultat lisible : envoyez-moi le fichier recherche_brute.json pour diagnostic.")
    elif args.cmd == "check":
        client = make_client(s, real=True)
        if hasattr(client, "get_account"):
            acc = client.get_account()
            print("✅ Connexion Unipile OK. Compte :", acc.get("id", s.unipile_account_id),
                  "| type :", acc.get("type", acc.get("provider", "?")), "| statut :", acc.get("status", "?"))
        if args.identifier:
            p = client.get_profile(args.identifier)
            print("✅ Profil lu :", p.get("first_name"), p.get("last_name"), "| id :", p.get("provider_id"))
    elif args.cmd == "import":
        cmd_import(conn, s, args.file)
    elif args.cmd == "status":
        cmd_status(conn, s)
    elif args.cmd == "run-once":
        print(OutreachEngine(conn, s, make_client(s)).run_once())
    elif args.cmd == "run":
        client = make_client(s)
        print(f"Moteur lancé (toutes les ~{args.every} min). Ctrl+C pour arrêter.")
        while True:
            report = OutreachEngine(conn, s, client).run_once()
            logging.getLogger("prospectia").info("Passage : %s", report)
            time.sleep(args.every * 60 * random.uniform(0.8, 1.2))
    elif args.cmd == "webhook":
        import uvicorn
        from .webhook_server import create_app
        uvicorn.run(create_app(s), host="0.0.0.0", port=args.port)
    elif args.cmd == "setup-webhooks":
        client = make_client(s, real=True)
        url = args.public_url.rstrip("/") + "/webhooks/unipile"
        if s.unipile_version == "v1":
            if not s.webhook_secret:
                sys.exit("Renseignez WEBHOOK_SECRET dans .env d'abord.")
            print(client.create_webhook("users", url, "ProspectIA nouvelles relations", s.webhook_secret, ["new_relation"]))
            print(client.create_webhook("messaging", url, "ProspectIA messages", s.webhook_secret, ["message_received"]))
        else:
            res = client.create_webhook_endpoint(url, ["relation.new", "message.new"])
            secret = res.get("secret")
            if not secret:
                sys.exit(f"Unipile n'a pas renvoyé de secret : {res}")
            set_env_value("WEBHOOK_SECRET", secret)
            print(f"✅ Webhook créé ({res.get('id')}). Secret enregistré automatiquement dans .env.")
            print("➡️  Redémarrez la commande « webhook » pour qu'elle prenne en compte le secret.")
    elif args.cmd == "dashboard":
        import threading, webbrowser
        import uvicorn
        from .dashboard import create_dashboard
        url = f"http://127.0.0.1:{args.port}"
        print(f"Tableau de bord : {url}  (Ctrl+C pour arrêter)")
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        uvicorn.run(create_dashboard(s, lambda: make_client(s), lambda: make_client(s, real=True)),
                    host="127.0.0.1", port=args.port, log_level="warning")
    elif args.cmd == "replies":
        cmd_replies(conn)
    elif args.cmd == "optout":
        t = args.target.strip()
        slug = db.public_identifier_from_url(t)
        db.suppress(conn, linkedin_slug=slug, email=None if slug else t, reason="opt-out manuel")
        if slug:
            conn.execute("UPDATE prospects SET status = ? WHERE public_identifier = ?", (db.OPTED_OUT, slug))
        else:
            conn.execute("UPDATE prospects SET status = ? WHERE lower(email) = ?", (db.OPTED_OUT, t.lower()))
        conn.commit()
        print("Ajouté à la liste « ne plus contacter ».")
    elif args.cmd == "pause":
        db.pause(conn, args.reason); print("Pause activée.")
    elif args.cmd == "resume":
        db.resume(conn); print("Reprise.")


if __name__ == "__main__":
    main()
