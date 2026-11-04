"""Tableau de bord local (navigateur) de ProspectIA Outreach.

Lancement : python -m prospectia_outreach dashboard   ->  http://127.0.0.1:8001
Il n'écoute QUE sur votre ordinateur (127.0.0.1) : il n'est pas visible depuis Internet,
même quand le tunnel des webhooks est ouvert.
"""
from __future__ import annotations

import csv
import io
import threading
from datetime import datetime
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from . import db, guards
from .config import Settings
from .engine import OutreachEngine

STATUS_LABELS = {
    db.NEW: ("Nouveau", "grey"), db.TO_REVIEW: ("À vérifier", "amber"), db.APPROVED: ("Validé", "blue"),
    db.INVITED: ("Invité", "blue"), db.CONNECTED: ("Connecté", "teal"), db.MESSAGED: ("Message envoyé", "teal"),
    db.REPLIED: ("A répondu", "green"), db.NO_RESPONSE: ("Sans réponse", "grey"), db.OPTED_OUT: ("STOP", "red"),
    db.DONE: ("Séquence finie", "grey"), db.ERROR: ("Erreur", "red"), db.SKIPPED: ("Ignoré", "grey"),
}
EVENT_LABELS = {
    "invite_sent": "Invitation envoyée", "message_sent": "Message envoyé", "inmail_sent": "InMail envoyé",
    "connected": "Invitation acceptée", "replied": "Réponse reçue", "opt_out": "Désinscription (STOP)",
    "error": "Erreur", "search": "Recherche LinkedIn", "paused": "Pause", "resumed": "Reprise", "invite_expired": "Invitation expirée",
}
STEP_LABELS = {0: "—", 1: "1er message", 2: "Relance 1", 3: "Relance 2"}
MSG_FIELDS = (("note", "Note d’invitation"), ("first", "1er message (après acceptation)"),
              ("followup1", "Relance 1"), ("followup2", "Relance 2"))

CSS = """
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
color:#1d1d1f;background:#f5f5f7}header{background:#fff;border-bottom:1px solid #e5e5ea;padding:16px 28px;display:flex;
align-items:center;gap:16px;flex-wrap:wrap}h1{font-size:19px;margin:0 auto 0 0}h2{font-size:15px;margin:0 0 12px}
main{max-width:1240px;margin:0 auto;padding:22px 28px}.card{background:#fff;border:1px solid #e5e5ea;border-radius:12px;
padding:18px;margin-bottom:18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:12px;
margin-bottom:18px}.kpi{background:#fff;border:1px solid #e5e5ea;border-radius:12px;padding:14px 16px}
.kpi b{display:block;font-size:24px;font-weight:650}.kpi span{color:#6e6e73;font-size:12.5px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.grey{background:#f0f0f2;color:#55555a}.amber{background:#fff4d6;color:#8a5a00}.blue{background:#e6f0ff;color:#1452b8}
.teal{background:#e0f5f2;color:#0b6b5e}.green{background:#e3f6e8;color:#17703a}.red{background:#fde8e8;color:#a3201d}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #f0f0f2;
vertical-align:top}th{font-size:12px;color:#6e6e73;font-weight:600}td small{color:#6e6e73}
button,.btn{font:inherit;border:1px solid #d2d2d7;background:#fff;border-radius:8px;padding:6px 12px;cursor:pointer;
color:#1d1d1f;text-decoration:none}button.primary{background:#0071e3;border-color:#0071e3;color:#fff}
button.danger{border-color:#e0a3a1;color:#a3201d}form.inline{display:inline}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}select,input[type=text]{font:inherit;padding:6px 9px;
border:1px solid #d2d2d7;border-radius:8px;background:#fff}.banner{padding:12px 16px;border-radius:10px;margin-bottom:18px}
.banner.sim{background:#e6f0ff;color:#1452b8}.banner.pause{background:#fde8e8;color:#a3201d}
.banner.ok{background:#e3f6e8;color:#17703a}details summary{cursor:pointer;color:#0071e3}
.msg{white-space:pre-wrap;background:#f9f9fb;border-radius:8px;padding:8px 10px;margin:6px 0;font-size:13px}
.cols{display:grid;grid-template-columns:2fr 1fr;gap:18px}@media(max-width:900px){.cols{grid-template-columns:1fr}}
.muted{color:#6e6e73}a{color:#0071e3}
.msgform label{display:block;margin:6px 0}.msgform textarea{width:100%;min-width:260px;font:inherit;font-size:13px;padding:6px 8px;border:1px solid #d2d2d7;border-radius:8px}
"""


def _pill(status: str) -> str:
    label, color = STATUS_LABELS.get(status, (status, "grey"))
    return f'<span class="pill {color}">{escape(label)}</span>'


def _when(value: str | None, tz: str) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(ZoneInfo(tz)).strftime("%d/%m %H:%M")


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)} %" if b else "—"


def create_dashboard(settings: Settings, client_factory, search_client_factory=None) -> FastAPI:
    app = FastAPI(title="ProspectIA Outreach — tableau de bord")
    s = settings
    run_lock = threading.Lock()
    state = {"last_run": None, "search": None}

    def conn():
        return db.connect(s.db_path)

    @app.get("/", response_class=HTMLResponse)
    def home(status: str = "", project: str = "", q: str = "", flash: str = "") -> str:
        c = conn()
        try:
            return _render(c, status, project, q, flash)
        finally:
            c.close()

    def _render(c, f_status: str, f_project: str, q: str, flash: str) -> str:
        now = db.utcnow()
        paused, reason = db.is_paused(c)
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM prospects GROUP BY status")}
        total = sum(counts.values())
        ev = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) n FROM events GROUP BY kind")}
        invited = ev.get("invite_sent", 0)
        accepted = ev.get("connected", 0)
        contacted = c.execute("SELECT COUNT(DISTINCT prospect_id) n FROM events "
                              "WHERE kind IN ('message_sent','inmail_sent')").fetchone()["n"]
        replied = c.execute("SELECT COUNT(DISTINCT prospect_id) n FROM events WHERE kind IN ('replied','opt_out')").fetchone()["n"]

        # --- bandeau d'état ---
        if paused:
            banner = (f'<div class="banner pause"><b>En pause</b> — {escape(reason)}. Vérifiez le compte LinkedIn '
                      f'à la main avant de reprendre.</div>')
        elif s.dry_run:
            banner = ('<div class="banner sim"><b>Mode SIMULATION</b> — rien n’est envoyé sur LinkedIn. '
                      'Passez <code>DRY_RUN=false</code> dans .env pour le mode réel.</div>')
        else:
            banner = '<div class="banner ok"><b>Mode RÉEL</b> — les envois partent vraiment sur LinkedIn.</div>'
        if flash:
            banner += f'<div class="banner ok">{escape(flash)}</div>'

        kpis = [
            (total, "prospects en base"), (counts.get(db.TO_REVIEW, 0), "à vérifier par un humain"),
            (invited, "invitations envoyées"), (_pct(accepted, invited), "taux d’acceptation"),
            (contacted, "personnes contactées"), (_pct(replied, contacted), "taux de réponse"),
            (counts.get(db.REPLIED, 0), "réponses à traiter"), (counts.get(db.OPTED_OUT, 0), "STOP / désinscrits"),
        ]
        kpi_html = "".join(f'<div class="kpi"><b>{escape(str(v))}</b><span>{escape(l)}</span></div>' for v, l in kpis)

        quota = (f'<span class="muted">Aujourd’hui : {guards.invites_remaining(c, now, s)} invitations '
                 f'et {guards.messages_remaining(c, now, s)} messages restants · '
                 f'{"heures ouvrées" if guards.in_working_hours(now, s) else "hors heures ouvrées (rien ne part)"}</span>')
        pause_btn = ('<form class="inline" method="post" action="/resume"><button class="primary">Reprendre</button></form>'
                     if paused else
                     '<form class="inline" method="post" action="/pause"><button class="danger">Pause d’urgence</button></form>')
        run_note = f'<span class="muted">Dernier passage : {escape(state["last_run"])}</span>' if state["last_run"] else ""

        # --- réponses à traiter ---
        reps = c.execute("""SELECT p.*, (SELECT detail FROM events e WHERE e.prospect_id=p.id AND e.kind='replied'
                             ORDER BY e.id DESC LIMIT 1) AS msg FROM prospects p WHERE status=? ORDER BY replied_at DESC""",
                         (db.REPLIED,)).fetchall()
        rep_html = "".join(
            f'<tr><td><b>{escape(r["full_name"])}</b><br><small>{escape(r["title"] or "")} · {escape(r["company"] or "")}</small></td>'
            f'<td><div class="msg">{escape(r["msg"] or "")}</div></td><td>{_when(r["replied_at"], s.timezone)}</td>'
            f'<td><a href="{escape(r["linkedin_url"])}" target="_blank">Répondre sur LinkedIn</a></td></tr>'
            for r in reps) or '<tr><td colspan="4" class="muted">Aucune réponse en attente.</td></tr>'

        # --- liste des prospects ---
        sql, args = "SELECT * FROM prospects WHERE 1=1", []
        if f_status:
            sql += " AND status = ?"; args.append(f_status)
        if f_project:
            sql += " AND project = ?"; args.append(f_project)
        if q:
            sql += " AND (full_name LIKE ? OR company LIKE ? OR title LIKE ?)"; args += [f"%{q}%"] * 3
        rows = c.execute(sql + " ORDER BY updated_at DESC LIMIT 300", args).fetchall()
        msgs: dict[int, dict[str, str]] = {}
        for m in c.execute("SELECT * FROM messages"):
            msgs.setdefault(m["prospect_id"], {})[m["kind"]] = m["body"]

        def actions(p) -> str:
            out = []
            if p["status"] in (db.TO_REVIEW, db.ERROR):
                out.append(f'<form class="inline" method="post" action="/approve/{p["id"]}"><button>Valider</button></form>')
            if p["status"] not in (db.OPTED_OUT,):
                out.append(f'<form class="inline" method="post" action="/optout/{p["id"]}" '
                           f'onsubmit="return confirm(\'Ne plus jamais contacter cette personne ?\')">'
                           f'<button class="danger">STOP</button></form>')
            return " ".join(out)

        def detail(p) -> str:
            m = msgs.get(p["id"], {})
            err = f'<div class="msg" style="color:#a3201d">{escape(p["error"])}</div>' if p["error"] else ""
            label = "Messages" if m.get("first") else "Écrire les messages"
            if p["status"] not in (db.NEW, db.TO_REVIEW, db.APPROVED, db.ERROR):
                parts = "".join(f'<div><small>{lbl}</small><div class="msg">{escape(m[k])}</div></div>'
                                for k, lbl in MSG_FIELDS if m.get(k))
                return f'<details><summary>{label}</summary>{err}{parts}</details>'
            fields = ""
            for k, lbl in MSG_FIELDS:
                extra = f' maxlength="{s.note_max_chars}"' if k == "note" else ""
                hint = f" ({s.note_max_chars} caractères max)" if k == "note" else ""
                rows_n = 2 if k == "note" else 3
                fields += (f'<label><small>{lbl}{hint}</small><textarea name="{k}" rows="{rows_n}"{extra}>'
                           f'{escape(m.get(k, ""))}</textarea></label>')
            checked = "checked" if p["role_verified"] else ""
            return (f'<details><summary>{label}</summary>{err}'
                    f'<form method="post" action="/messages/{p["id"]}" class="msgform">{fields}'
                    f'<label><small>Score /10</small> <input type="text" name="score" value="{p["score"]}" size="3"></label>'
                    f'<label><input type="checkbox" name="role_verified" value="1" {checked}> poste vérifié</label>'
                    f'<button class="primary">Enregistrer</button></form></details>')

        body_rows = "".join(
            f'<tr><td><b>{escape(p["full_name"])}</b><br><small>{escape(p["title"] or "")}</small>'
            f'<br><a href="{escape(p["linkedin_url"])}" target="_blank"><small>Profil LinkedIn</small></a></td>'
            f'<td>{escape(p["company"] or "")}<br><small>Projet {escape(p["project"])} · '
            f'{"mécénat" if p["project"] == "A" else "corners/partenariats"}</small></td>'
            f'<td>{p["score"]}/10<br><small>{"poste vérifié" if p["role_verified"] else "poste non vérifié"}</small></td>'
            f'<td>{_pill(p["status"])}<br><small>{STEP_LABELS.get(p["step"], "")}</small></td>'
            f'<td><small>Invité {_when(p["invited_at"], s.timezone)}<br>Dernier msg {_when(p["last_message_at"], s.timezone)}</small></td>'
            f'<td>{detail(p)}</td><td>{actions(p)}</td></tr>'
            for p in rows) or '<tr><td colspan="7" class="muted">Aucun prospect. Importez un fichier CSV ci-dessus.</td></tr>'

        status_opts = "".join(f'<option value="{k}" {"selected" if k == f_status else ""}>{escape(v[0])}</option>'
                              for k, v in STATUS_LABELS.items())
        proj_opts = "".join(f'<option value="{k}" {"selected" if k == f_project else ""}>{l}</option>'
                            for k, l in (("A", "A · mécénat"), ("B", "B · partenariats")))

        # --- recherche LinkedIn ---
        searches_today = db.count_events(c, ("search",), guards.start_of_day(now, s))
        sr = state["search"]
        res_html = ""
        if sr and sr.get("error"):
            res_html = f'<div class="banner pause">{escape(sr["error"])}</div>'
        elif sr:
            lines = "".join(
                f'<tr><td><input type="checkbox" name="url" value="{escape(r["linkedin_url"])}" checked></td>'
                f'<td><b>{escape(r["full_name"])}</b><br><small>{escape(str(r["title"])[:90])}</small></td>'
                f'<td><small>{escape(str(r.get("location") or ""))}</small></td>'
                f'<td><a href="{escape(r["linkedin_url"])}" target="_blank"><small>Profil</small></a></td></tr>'
                for r in sr["new"])
            res_html = (f'<p class="muted">{len(sr["found"])} profils trouvés · {len(sr["new"])} nouveaux · '
                        f'{sr["already"]} déjà en base · {sr["blocked"]} en liste STOP (écartés).</p>')
            if sr["new"]:
                res_html += (f'<form method="post" action="/search/add"><input type="hidden" name="project" '
                             f'value="{escape(sr["project"])}"><table>{lines}</table>'
                             f'<button class="primary">Ajouter la sélection</button> <span class="muted">'
                             f'→ statut « À vérifier » : rien ne part avant messages + validation.</span></form>')
        search_html = f"""<div class="card"><h2>Chercher sur LinkedIn</h2>
<form method="post" action="/search" class="bar">
<input type="text" name="keywords" placeholder="ex. directeur RSE" style="flex:1;min-width:180px">
<select name="project"><option value="A">A · mécénat</option><option value="B">B · partenariats</option></select>
<select name="limit"><option>10</option><option selected>25</option><option>50</option></select>
<button class="primary">Rechercher</button></form>
<details><summary>Filtres avancés : coller l’adresse d’une recherche LinkedIn</summary>
<form method="post" action="/search" class="bar"><input type="text" name="url" style="flex:1"
placeholder="https://www.linkedin.com/search/results/people/?...">
<select name="project"><option value="A">A</option><option value="B">B</option></select>
<input type="hidden" name="limit" value="25"><button>Rechercher</button></form></details>
<p class="muted">Lecture seule, rien n’est envoyé · {searches_today}/{s.max_searches_per_day} recherches aujourd’hui</p>
{res_html}</div>"""

        # --- activité récente ---
        evs = c.execute("""SELECT e.*, p.full_name FROM events e LEFT JOIN prospects p ON p.id = e.prospect_id
                           ORDER BY e.id DESC LIMIT 25""").fetchall()
        ev_html = "".join(
            f'<tr><td><small>{_when(e["at"], s.timezone)}</small></td><td>{escape(EVENT_LABELS.get(e["kind"], e["kind"]))}'
            f'{" <small>(simulation)</small>" if e["dry_run"] else ""}</td><td>{escape(e["full_name"] or "")}</td></tr>'
            for e in evs) or '<tr><td class="muted">Rien pour l’instant.</td></tr>'

        refresh = "" if sr else '<meta http-equiv="refresh" content="60">'
        return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>ProspectIA — tableau de bord</title><style>{CSS}</style></head><body>
<header><h1>ProspectIA · envois LinkedIn</h1>{quota}{pause_btn}
<form class="inline" method="post" action="/run" onsubmit="return confirm('Lancer un passage maintenant ?')">
<button class="primary">Lancer un passage</button></form></header>
<main>{banner}<div class="grid">{kpi_html}</div>
<div class="cols"><div>
{search_html}
<div class="card"><h2>Réponses à traiter ({len(reps)})</h2><table>{rep_html}</table></div>
<div class="card"><h2>Prospects</h2>
<form class="bar" method="get" action="/"><input type="text" name="q" value="{escape(q)}" placeholder="Nom, entreprise, poste…">
<select name="status"><option value="">Tous les statuts</option>{status_opts}</select>
<select name="project"><option value="">Tous les projets</option>{proj_opts}</select>
<button>Filtrer</button> <a class="btn" href="/">Réinitialiser</a></form>
<table><tr><th>Personne</th><th>Organisation</th><th>Score</th><th>Statut</th><th>Dates</th><th></th><th></th></tr>
{body_rows}</table></div></div>
<div><div class="card"><h2>Importer des prospects</h2>
<form method="post" action="/import" enctype="multipart/form-data" class="bar">
<input type="file" name="file" accept=".csv" required><button class="primary">Importer</button></form>
<p class="muted">Format : voir <code>exemple_prospects.csv</code>. Doublons et STOP ignorés.</p></div>
<div class="card"><h2>Activité récente</h2><table>{ev_html}</table>{run_note}</div></div></div>
</main></body></html>"""

    def back(msg: str = "") -> RedirectResponse:
        return RedirectResponse("/?" + urlencode({"flash": msg}) if msg else "/", status_code=303)

    @app.post("/pause")
    def do_pause():
        c = conn(); db.pause(c, "pause manuelle (tableau de bord)"); c.close()
        return back("Pause activée : plus rien ne part.")

    @app.post("/resume")
    def do_resume():
        c = conn(); db.resume(c); c.close()
        return back("Reprise.")

    @app.post("/approve/{pid}")
    def approve(pid: int):
        c = conn()
        try:
            if not db.get_message(c, pid, "first"):
                return back("Impossible de valider : écrivez d’abord le 1er message (« Écrire les messages »).")
            db.update_prospect(c, pid, status=db.APPROVED, error=None)
        finally:
            c.close()
        return back("Prospect validé : il partira au prochain passage.")

    @app.post("/messages/{pid}")
    async def save_messages(pid: int, request: Request):
        form = await request.form()
        c = conn()
        try:
            for kind, _ in MSG_FIELDS:
                body = str(form.get(kind) or "").strip()
                if kind == "note":
                    body = body[: s.note_max_chars]
                c.execute("DELETE FROM messages WHERE prospect_id=? AND kind=?", (pid, kind))
                if body:
                    c.execute("INSERT INTO messages (prospect_id, kind, body) VALUES (?,?,?)", (pid, kind, body))
            try:
                score = max(0, min(10, int(str(form.get("score") or 0))))
            except ValueError:
                score = 0
            db.update_prospect(c, pid, score=score, role_verified=int(bool(form.get("role_verified"))), error=None)
        finally:
            c.close()
        return back("Messages enregistrés. Cliquez sur « Valider » pour autoriser l’envoi.")

    @app.post("/search")
    async def do_search(request: Request):
        from .search import run_search
        form = await request.form()
        url = str(form.get("url") or "").strip()
        kw = str(form.get("keywords") or "").strip()
        project = form.get("project") if form.get("project") in ("A", "B") else "A"
        try:
            limit = max(1, min(int(str(form.get("limit") or 25)), 100))
        except ValueError:
            limit = 25
        if not (url or kw):
            return back("Tapez des mots-clés (ou collez l’adresse d’une recherche LinkedIn).")
        if url and "linkedin.com/search/results/people" not in url:
            return back("L’adresse doit être une page de résultats de recherche de PERSONNES LinkedIn.")
        if search_client_factory is None:
            return back("Recherche indisponible : client Unipile non configuré.")
        c = conn()
        try:
            if db.count_events(c, ("search",), guards.start_of_day(db.utcnow(), s)) >= s.max_searches_per_day:
                return back(f"Quota de recherches atteint pour aujourd’hui ({s.max_searches_per_day}).")
            try:
                r = run_search(search_client_factory(), c, url=url or None, keywords=kw or None,
                               limit=limit, project=project)
                state["search"] = {**r, "project": project}
            except Exception as exc:  # clé absente, erreur Unipile...
                state["search"] = {"error": f"La recherche a échoué : {exc}"}
        finally:
            c.close()
        return back()

    @app.post("/search/add")
    async def search_add(request: Request):
        form = await request.form()
        chosen = set(form.getlist("url"))
        project = form.get("project") if form.get("project") in ("A", "B") else "A"
        sr = state["search"] or {}
        c = conn()
        added = 0
        try:
            for r in sr.get("new", []):
                if r["linkedin_url"] in chosen:
                    pid = db.add_prospect(c, full_name=r["full_name"], project=project,
                                          linkedin_url=r["linkedin_url"], title=str(r["title"]),
                                          company=str(r.get("company") or ""), score=0, role_verified=False,
                                          source_label=r["source_label"], messages={},
                                          provider_id=r.get("provider_id") or None)
                    added += bool(pid)
            OutreachEngine(c, s, None).auto_review()
        finally:
            c.close()
        state["search"] = None
        return back(f"{added} prospects ajoutés en « À vérifier ». Écrivez leurs messages puis validez-les.")

    @app.post("/search/clear")
    def search_clear():
        state["search"] = None
        return back()

    @app.post("/optout/{pid}")
    def optout(pid: int):
        c = conn()
        p = c.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
        if p:
            db.suppress(c, linkedin_slug=p["public_identifier"], email=p["email"], reason="STOP manuel (tableau de bord)")
            db.update_prospect(c, pid, status=db.OPTED_OUT)
            db.log_event(c, "opt_out", pid, "manuel")
        c.close()
        return back("Ajouté à la liste « ne plus contacter ».")

    @app.post("/run")
    def run():
        if not run_lock.acquire(blocking=False):
            return back("Un passage est déjà en cours.")

        def work():
            c = db.connect(s.db_path)
            try:
                rep = OutreachEngine(c, s, client_factory()).run_once()
                done = ", ".join(rep["actions"]) or (rep["skipped_reason"] or "rien à faire")
                state["last_run"] = f'{datetime.now(ZoneInfo(s.timezone)).strftime("%H:%M")} — {done}'
            finally:
                c.close(); run_lock.release()

        threading.Thread(target=work, daemon=True).start()
        return back("Passage lancé. Les envois sont espacés : rafraîchissez dans quelques minutes.")

    @app.post("/import")
    async def do_import(file: UploadFile = File(...)):
        from .__main__ import MESSAGE_COLUMNS, _truthy
        text = (await file.read()).decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;")
        added = skipped = bad = 0
        c = conn()
        for row in csv.DictReader(io.StringIO(text), dialect=dialect):
            try:
                pid = db.add_prospect(
                    c, full_name=row["full_name"].strip(), project=(row.get("project") or "A").strip().upper(),
                    linkedin_url=row["linkedin_url"].strip(), title=row.get("title", ""), company=row.get("company", ""),
                    score=int(float(row.get("score") or 0)), role_verified=_truthy(row.get("role_verified", "")),
                    email=(row.get("email") or "").strip(), source_label=row.get("source_label", ""),
                    channel=(row.get("channel") or "invite").strip().lower(),
                    messages={k: row.get(k, "") for k in MESSAGE_COLUMNS})
                added += bool(pid); skipped += not pid
            except (KeyError, ValueError):
                bad += 1
        OutreachEngine(c, s, None).auto_review()
        c.close()
        return back(f"Import : {added} ajoutés, {skipped} doublons/STOP, {bad} lignes invalides.")

    return app
