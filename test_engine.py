from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from prospectia_outreach import db
from prospectia_outreach.config import Settings
from prospectia_outreach.engine import OutreachEngine
from prospectia_outreach.unipile_client import DryRunClient, RateLimitError, UnipileClient, UnipileError
from prospectia_outreach.webhook_server import create_app

PARIS = ZoneInfo("Europe/Paris")
MSGS = {"note": "Bonjour, ravi d'échanger.", "first": "Merci d'avoir accepté !",
        "followup1": "Petite relance.", "followup2": "Dernier message."}


class Clock:
    def __init__(self, local_dt: datetime):
        self.t = local_dt.replace(tzinfo=PARIS).astimezone(timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env(tmp_path):
    s = Settings(db_path=str(tmp_path / "t.db"), min_delay_seconds=0, max_delay_seconds=0,
                 max_actions_per_run=50, webhook_secret="secret")
    conn = db.connect(s.db_path)
    clock = Clock(datetime(2026, 11, 3, 10, 0))  # mardi 10h, Paris
    client = DryRunClient()
    engine = OutreachEngine(conn, s, client, now=clock, sleep=lambda _: None)
    return s, conn, clock, client, engine


def add(conn, i, score=8, verified=True, **kw):
    return db.add_prospect(conn, full_name=f"P{i}", project="A", linkedin_url=f"https://www.linkedin.com/in/p-{i}/",
                           score=score, role_verified=verified, messages=kw.pop("messages", MSGS), **kw)


def status(conn, pid):
    return conn.execute("SELECT status FROM prospects WHERE id=?", (pid,)).fetchone()["status"]


def test_auto_review(env):
    s, conn, clock, client, engine = env
    good, low, unverified = add(conn, 1), add(conn, 2, score=5), add(conn, 3, verified=False)
    engine.auto_review()
    assert status(conn, good) == db.APPROVED
    assert status(conn, low) == db.TO_REVIEW
    assert status(conn, unverified) == db.TO_REVIEW


def test_duplicates_are_ignored(env):
    s, conn, *_ = env
    assert add(conn, 1) is not None
    assert add(conn, 1) is None


def test_nothing_sent_outside_working_hours(env):
    s, conn, clock, client, engine = env
    add(conn, 1)
    clock.t = datetime(2026, 11, 3, 20, 0, tzinfo=PARIS).astimezone(timezone.utc)  # mardi 20h
    assert engine.run_once()["skipped_reason"] == "hors heures ouvrées"
    clock.t = datetime(2026, 11, 7, 10, 0, tzinfo=PARIS).astimezone(timezone.utc)  # samedi
    assert engine.run_once()["skipped_reason"] == "hors heures ouvrées"
    assert client.sent == []


def test_full_sequence(env):
    s, conn, clock, client, engine = env
    pid = add(conn, 1, source_label="rapport RSE 2025")
    engine.run_once()
    assert status(conn, pid) == db.INVITED
    assert client.sent[0][0] == "invitation"

    print(engine.handle_new_relation({"event": "new_relation", "user_provider_id": "SIM_p-1",
                                      "user_public_identifier": "p-1"}))
    assert status(conn, pid) == db.CONNECTED

    engine.run_once()
    action, data = client.sent[-1]
    assert action == "premier_message"
    assert "rapport RSE 2025" in data["text"] and "STOP" in data["text"]
    assert status(conn, pid) == db.MESSAGED

    clock.advance(days=2); engine.run_once()                 # jeudi J+2
    assert client.sent[-1][0] == "premier_message"          # pas de relance avant J+5
    clock.advance(days=3); engine.run_once()                 # dimanche J+5 : week-end -> on attend
    assert client.sent[-1][0] == "premier_message"
    clock.advance(days=1); engine.run_once()                 # lundi : relance 1
    assert client.sent[-1][0] == "relance" and client.sent[-1][1]["text"] == "Petite relance."
    clock.advance(days=4); engine.run_once()                 # vendredi, seulement J+4 depuis la relance
    assert client.sent[-1][1]["text"] == "Petite relance."
    clock.advance(days=3); engine.run_once()                 # lundi suivant : relance 2
    assert client.sent[-1][1]["text"] == "Dernier message."
    assert status(conn, pid) == db.DONE
    n = len(client.sent); clock.advance(days=10); engine.run_once()
    assert len(client.sent) == n                              # plus rien après la dernière relance


def test_reply_stops_sequence(env):
    s, conn, clock, client, engine = env
    pid = add(conn, 1)
    engine.run_once()
    engine.handle_new_relation({"user_provider_id": "SIM_p-1"})
    engine.run_once()
    print(engine.handle_message({"event": "message_received", "chat_id": "c1", "message": "Avec plaisir, appelez-moi",
                                 "sender": {"attendee_provider_id": "SIM_p-1"}, "account_info": {"user_id": "ME"}}))
    assert status(conn, pid) == db.REPLIED
    n = len(client.sent); clock.advance(days=12); engine.run_once()
    assert len(client.sent) == n


def test_own_messages_ignored(env):
    s, conn, clock, client, engine = env
    add(conn, 1); engine.run_once(); engine.handle_new_relation({"user_provider_id": "SIM_p-1"})
    r = engine.handle_message({"message": "STOP", "sender": {"attendee_provider_id": "ME"}, "account_info": {"user_id": "ME"}})
    assert "ignoré" in r


def test_stop_opt_out_is_permanent(env):
    s, conn, clock, client, engine = env
    pid = add(conn, 1); engine.run_once(); engine.handle_new_relation({"user_provider_id": "SIM_p-1"})
    engine.handle_message({"message": "Stop merci", "sender": {"attendee_provider_id": "SIM_p-1"}})
    assert status(conn, pid) == db.OPTED_OUT
    conn.execute("DELETE FROM prospects"); conn.commit()       # même après suppression de la fiche…
    assert add(conn, 1) is None                                 # … impossible de le ré-importer


def test_warmup_limits_first_days(env):
    s, conn, clock, client, engine = env
    for i in range(30):
        add(conn, i)
    engine.run_once()
    assert sum(1 for a, _ in client.sent if a == "invitation") == 5      # jour 1 : 5
    clock.advance(days=1); engine.run_once()
    assert sum(1 for a, _ in client.sent if a == "invitation") == 5 + 7  # jour 2 : 7


def test_weekly_cap(env):
    s, conn, clock, client, engine = env
    s.warmup_start_invites, s.max_invites_per_day, s.max_invites_per_week = 50, 50, 12
    for i in range(30):
        add(conn, i)
    engine.run_once()
    assert sum(1 for a, _ in client.sent if a == "invitation") == 12


def test_rate_limit_pauses_everything(env):
    s, conn, clock, client, engine = env
    add(conn, 1)

    def boom(*a, **k):
        raise RateLimitError(429, "errors/limit_exceeded", "too many")
    client.send_invitation = boom
    report = engine.run_once()
    assert "pause automatique" in report["skipped_reason"]
    assert db.is_paused(conn)[0]
    assert "en pause" in engine.run_once()["skipped_reason"]


def test_api_error_marks_prospect_and_continues(env):
    s, conn, clock, client, engine = env
    p1, p2 = add(conn, 1, score=9), add(conn, 2, score=8)
    real = client.send_invitation

    def flaky(provider_id, message):
        if provider_id == "SIM_p-1":
            raise UnipileError(422, "errors/already_invited", "")
        return real(provider_id, message)
    client.send_invitation = flaky
    engine.run_once()
    assert status(conn, p1) == db.ERROR and status(conn, p2) == db.INVITED


def test_long_note_goes_to_review(env):
    s, conn, clock, client, engine = env
    pid = add(conn, 1, messages={**MSGS, "note": "x" * 250})
    engine.run_once()
    assert status(conn, pid) == db.TO_REVIEW and client.sent == []


def test_invitation_expiry(env):
    s, conn, clock, client, engine = env
    pid = add(conn, 1); engine.run_once()
    clock.advance(days=22); engine.run_once()
    assert status(conn, pid) == db.NO_RESPONSE


def test_inmail_channel(env):
    s, conn, clock, client, engine = env
    s.use_inmail = True
    pid = add(conn, 1, channel="inmail")
    engine.run_once()
    assert client.sent[0][0] == "inmail" and status(conn, pid) == db.MESSAGED


def test_webhook_server_token(env, monkeypatch):
    s, conn, clock, client, engine = env
    add(conn, 1); engine.run_once()
    api = TestClient(create_app(s))
    assert api.post("/webhooks/unipile", json={"event": "new_relation"}).status_code == 401
    r = api.post("/webhooks/unipile", headers={"X-Webhook-Token": "secret"},
                 json={"event": "new_relation", "user_provider_id": "SIM_p-1", "user_public_identifier": "p-1"})
    assert r.status_code == 200 and "connecté" in r.json()["result"]


def test_pause_only_between_sends(env):
    s, conn, clock, client, engine = env
    pauses = []
    engine.sleep = pauses.append
    s.min_delay_seconds = s.max_delay_seconds = 100
    add(conn, 1); add(conn, 2)
    engine.run_once()
    assert len(pauses) == 1          # 2 envois -> 1 seule pause, aucune à la fin
    pauses.clear(); engine.run_once()
    assert pauses == []              # rien à envoyer -> aucune attente


def test_dashboard_pages(env):
    from prospectia_outreach.dashboard import create_dashboard
    s, conn, clock, client, engine = env
    pid = add(conn, 1); add(conn, 2, score=4)
    engine.run_once()
    ui = TestClient(create_dashboard(s, lambda: DryRunClient()))
    html = ui.get("/").text
    assert "ProspectIA" in html and "P1" in html and "Invité" in html
    assert "<b>P1</b>" not in ui.get("/?status=to_review").text and "<b>P2</b>" in ui.get("/?status=to_review").text
    assert ui.post("/pause", follow_redirects=False).status_code == 303 and db.is_paused(conn)[0]
    ui.post("/resume"); assert not db.is_paused(conn)[0]
    ui.post(f"/optout/{pid}"); assert status(conn, pid) == db.OPTED_OUT
    csv_bytes = open("exemple_prospects.csv", "rb").read()
    r = ui.post("/import", files={"file": ("p.csv", csv_bytes, "text/csv")})
    assert "1 ajoutés" in r.text


# ------------------------- Unipile v2 -------------------------
import hashlib, hmac as _hmac, json as _json, time as _time
from prospectia_outreach.unipile_client import UnipileV2Client
from prospectia_outreach.webhook_server import normalize_v2, verify_v2_signature


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.content = _json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


def _v2_client(monkeypatch, responses):
    c = UnipileV2Client("KEY", "acc_123")
    calls = []

    def fake(method, url, timeout=None, **kw):
        calls.append((method, url, kw.get("json")))
        return responses.pop(0)
    monkeypatch.setattr(c.session, "request", fake)
    return c, calls


def test_v2_requests(monkeypatch):
    c, calls = _v2_client(monkeypatch, [
        _Resp(200, {"id": "ACoXYZ", "first_name": "Jean"}),
        _Resp(201, {"object": "RelationRequest"}),
        _Resp(200, {"chat_id": "chat_1"}),
        _Resp(200, {"id": "msg_1"}),
        _Resp(201, {"id": "we_1", "secret": "s3cr3t"}),
    ])
    assert c.session.headers["X-API-KEY"] == "KEY"
    assert c.get_profile("jean-dupont")["provider_id"] == "ACoXYZ"
    c.send_invitation("ACoXYZ", "Bonjour")
    assert c.start_chat("ACoXYZ", "Salut", inmail=True)["chat_id"] == "chat_1"
    c.send_message("chat_1", "Relance")
    assert c.create_webhook_endpoint("https://x.example/webhooks/unipile", ["relation.new", "message.new"])["secret"] == "s3cr3t"
    base = "https://api.unipile.com/v2"
    assert calls[0][:2] == ("GET", f"{base}/acc_123/users/jean-dupont")
    assert calls[1] == ("POST", f"{base}/acc_123/users/me/relation-requests", {"user_id": "ACoXYZ", "message": "Bonjour"})
    assert calls[2][1] == f"{base}/acc_123/inboxes/CLASSIC_PRIMARY/chats/send"
    assert calls[2][2] == {"users_ids": "ACoXYZ", "text": "Salut", "specifics": {"linkedin": {"classic": {"inmail": True}}}}
    assert calls[3] == ("POST", f"{base}/acc_123/chats/chat_1/messages/send", {"text": "Relance"})
    assert calls[4][1] == f"{base}/webhooks/endpoints"


def test_v2_errors_trigger_pause_types(monkeypatch):
    c, _ = _v2_client(monkeypatch, [_Resp(429, {"type": "errors/too_many_requests"}),
                                    _Resp(401, {"type": "errors/disconnected_account"})])
    from prospectia_outreach.unipile_client import AccountProblemError
    with pytest.raises(RateLimitError):
        c.send_invitation("x", None)
    with pytest.raises(AccountProblemError):
        c.send_invitation("x", None)


def test_v2_account_id_format():
    with pytest.raises(ValueError):
        UnipileV2Client("KEY", "wrong")


def _sign(secret, body: bytes, ts=None):
    ts = str(int(ts or _time.time()))
    sig = _hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v0={sig}"


def test_v2_signature():
    body = b'{"type":"message.new"}'
    assert verify_v2_signature("sec", _sign("sec", body), body)
    assert not verify_v2_signature("sec", _sign("autre", body), body)
    assert not verify_v2_signature("sec", _sign("sec", body), body + b" ")          # corps modifié
    assert not verify_v2_signature("sec", _sign("sec", body, ts=_time.time() - 3600), body)  # trop ancien


def test_normalize_v2_events():
    k, p = normalize_v2({"type": "relation.new", "payload": {"user": {"id": "ACo1", "public_identifier": "jean"}}})
    assert k == "new_relation" and p == {"user_provider_id": "ACo1", "user_public_identifier": "jean"}
    k, p = normalize_v2({"type": "message.new", "payload": {"chat_id": "c1", "text": "STOP", "sender_id": "ACo1"}})
    assert k == "message_received" and p["message"] == "STOP" and p["sender"]["attendee_provider_id"] == "ACo1"


def test_webhook_server_v2_signed(env, tmp_path, monkeypatch):
    s, conn, clock, client, engine = env
    monkeypatch.chdir(tmp_path)
    add(conn, 1); engine.run_once()
    api = TestClient(create_app(s))
    body = _json.dumps({"type": "relation.new", "payload": {"user": {"id": "SIM_p-1", "public_identifier": "p-1"}}}).encode()
    assert api.post("/webhooks/unipile", content=body, headers={"unipile-signature": _sign("faux", body)}).status_code == 401
    r = api.post("/webhooks/unipile", content=body,
                 headers={"unipile-signature": _sign("secret", body), "content-type": "application/json"})
    assert r.status_code == 200 and "connecté" in r.json()["result"]
    assert (tmp_path / "webhooks_recus.jsonl").exists()


def test_cli_make_client_v2_and_check(monkeypatch, capsys):
    from prospectia_outreach import __main__ as cli
    s = Settings(dry_run=True, unipile_api_key="KEY", unipile_account_id="acc_1")
    assert isinstance(cli.make_client(s), DryRunClient)
    c = cli.make_client(s, real=True)
    assert isinstance(c, UnipileV2Client)
    s.unipile_version = "v1"; s.unipile_dsn = "api1.unipile.com:1"
    assert isinstance(cli.make_client(s, real=True), UnipileClient)


# ------------------------- Recherche LinkedIn -------------------------
def test_search_to_csv(env, tmp_path):
    import csv as _csv
    from prospectia_outreach.search import run_search
    s, conn, clock, client, engine = env
    add(conn, 1)                                                            # p-1 déjà en base
    db.suppress(conn, linkedin_slug="stop-me")                              # en liste STOP

    class FakeSearch:
        calls = []
        def search_people(self, url=None, keywords=None, cursor=None):
            self.calls.append(cursor)
            if cursor is None:
                return {"items": [
                    {"name": "Claire Martin", "headline": "Directrice RSE chez Alpha", "public_identifier": "claire-m",
                     "location": "Paris, Île-de-France", "network_distance": "DISTANCE_2", "id": "ACo1"},
                    {"name": "LinkedIn Member", "public_identifier": "hidden"},
                    {"first_name": "P", "last_name": "1", "public_profile_url": "https://www.linkedin.com/in/p-1/"},
                    {"name": "Stop Me", "public_identifier": "stop-me"},
                ], "cursor": "page2"}
            return {"items": [{"name": "Hugo B", "headline": "Délégué général fondation", "public_identifier": "hugo-b"}],
                    "cursor": None}

    fs = FakeSearch()
    r = run_search(fs, conn, url="https://www.linkedin.com/search/results/people/?keywords=rse", keywords=None,
                   limit=10, project="A", out_dir=str(tmp_path))
    assert fs.calls == [None, "page2"]
    assert [p["full_name"] for p in r["found"]] == ["Claire Martin", "P 1", "Stop Me", "Hugo B"]  # membre masqué exclu
    assert r["already"] == 1 and r["blocked"] == 1
    rows = list(_csv.DictReader(open(r["file"], encoding="utf-8-sig")))
    assert [x["full_name"] for x in rows] == ["Claire Martin", "Hugo B"]
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/claire-m/" and rows[0]["first"] == ""
    assert "recherche LinkedIn" in rows[0]["source_label"]
    # ré-import : ces prospects restent « à vérifier » (pas de message, score 0) -> rien ne part
    from prospectia_outreach.__main__ import cmd_import
    cmd_import(conn, s, r["file"])
    engine.auto_review()
    assert {x["status"] for x in conn.execute("SELECT status FROM prospects WHERE public_identifier IN ('claire-m','hugo-b')")} == {db.TO_REVIEW}


def test_v2_search_requests(monkeypatch):
    c, calls = _v2_client(monkeypatch, [_Resp(200, {"items": []}), _Resp(200, {"items": []}), _Resp(200, {"items": []})])
    c.search_people(url="https://www.linkedin.com/search/results/people/?keywords=rse")
    c.search_people(keywords="directeur RSE")
    c.search_people(url="u", cursor="abc")
    base = "https://api.unipile.com/v2/acc_123"
    assert calls[0] == ("POST", f"{base}/linkedin/search", {"url": "https://www.linkedin.com/search/results/people/?keywords=rse"})
    assert calls[1] == ("POST", f"{base}/linkedin/search/people", {"keywords": "directeur RSE"})
    assert calls[2] == ("POST", f"{base}/linkedin/search", {"cursor": "abc"})


def test_dashboard_search_edit_approve_flow(env, tmp_path, monkeypatch):
    from prospectia_outreach.dashboard import create_dashboard
    s, conn, clock, client, engine = env
    monkeypatch.chdir(tmp_path)

    class FakeSearch:
        def search_people(self, url=None, keywords=None, cursor=None):
            return {"items": [{"name": "Claire Martin", "headline": "Directrice RSE", "public_identifier": "claire-m",
                               "location": "Paris"}]}
    ui = TestClient(create_dashboard(s, lambda: DryRunClient(), lambda: FakeSearch()))
    html = ui.post("/search", data={"keywords": "directeur RSE", "project": "A", "limit": "10"}).text
    assert "Claire Martin" in html and "Ajouter la sélection" in html and 'http-equiv="refresh"' not in html
    html = ui.post("/search/add", data={"url": "https://www.linkedin.com/in/claire-m/", "project": "A"}).text
    assert "1 prospects ajoutés" in html
    pid = conn.execute("SELECT id FROM prospects WHERE public_identifier='claire-m'").fetchone()["id"]
    assert status(conn, pid) == db.TO_REVIEW
    # valider sans message -> refusé
    assert "Impossible de valider" in ui.post(f"/approve/{pid}").text
    assert status(conn, pid) == db.TO_REVIEW
    # écrire les messages puis valider
    ui.post(f"/messages/{pid}", data={"note": "x" * 500, "first": "Bonjour Claire", "followup1": "Relance",
                                      "followup2": "", "score": "8", "role_verified": "1"})
    assert len(db.get_message(conn, pid, "note")) == s.note_max_chars
    assert db.get_message(conn, pid, "followup2") is None
    ui.post(f"/approve/{pid}")
    assert status(conn, pid) == db.APPROVED
    # quota de recherche
    s.max_searches_per_day = 1
    assert "Quota de recherches" in ui.post("/search", data={"keywords": "x"}).text


def test_engine_never_sends_empty_first_message(env):
    s, conn, clock, client, engine = env
    pid = add(conn, 1, messages={"note": "Bonjour"})
    db.update_prospect(conn, pid, status=db.APPROVED)          # validé à la main sans 1er message
    engine.run_once()
    assert client.sent == [] and status(conn, pid) == db.TO_REVIEW


def test_search_real_unipile_v2_format(env, tmp_path):
    """Format réellement renvoyé par Unipile v2 (2 nov. 2026) : {"data": [...]}, display_name, id ACo..."""
    import csv as _csv
    from prospectia_outreach.search import run_search, company_from_headline
    s, conn, clock, client, engine = env
    page = {"data": [
        {"object": "PeopleSearchResult", "id": "ACoAAGvMpNUBtt", "display_name": "Sans Identifiant",
         "network_distance": "THIRD_DEGREE", "profile_url": "https://www.linkedin.com/in/ACoAAGvMpNUBtt",
         "location": "Paris, Île-de-France, France", "headline": "Directeur RSE"},
        {"object": "PeopleSearchResult", "id": "ACoAAAPPkgYB", "display_name": "LAURENCE TEST",
         "public_identifier": "laurence-test-4b24", "profile_url": "https://www.linkedin.com/in/laurence-test-4b24",
         "location": "France", "headline": "Directrice RSE chez Groupe MCGP"},
        {"object": "PeopleSearchResult", "id": "ACoX", "display_name": "LinkedIn Member"},
    ]}

    class F:
        def search_people(self, **kw):
            return page
    r = run_search(F(), conn, url=None, keywords="directeur RSE", limit=25, project="A", out_dir=str(tmp_path))
    names = [p["full_name"] for p in r["found"]]
    assert names == ["Sans Identifiant", "LAURENCE TEST"]
    assert r["found"][0]["linkedin_url"] == "https://www.linkedin.com/in/ACoAAGvMpNUBtt/"   # casse conservée
    assert r["found"][1]["company"] == "Groupe MCGP" and r["found"][1]["provider_id"] == "ACoAAAPPkgYB"
    assert company_from_headline("Directeur RSE") == ""
    # ajout puis envoi : on utilise directement l'id, sans relire le profil
    from prospectia_outreach.__main__ import cmd_import
    cmd_import(conn, s, r["file"])
    row = conn.execute("SELECT * FROM prospects WHERE full_name='Sans Identifiant'").fetchone()
    assert row["public_identifier"] == "ACoAAGvMpNUBtt" and row["provider_id"] == "ACoAAGvMpNUBtt"
