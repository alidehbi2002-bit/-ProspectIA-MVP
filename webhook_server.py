"""Petit serveur qui reçoit les notifications d'Unipile (invitation acceptée, nouveau message).

Lancement : python -m prospectia_outreach webhook --port 8000
Unipile doit pouvoir joindre cette adresse depuis Internet (voir README : tunnel Cloudflare).

Sécurité :
- Unipile v2 signe chaque envoi (en-tête « unipile-signature : t=...,v0=... ») avec le secret
  du webhook : HMAC-SHA256(secret, "t.corps_brut"). On vérifie cette signature.
- Unipile v1 : en-tête X-Webhook-Token = WEBHOOK_SECRET.
Chaque notification reçue est aussi copiée telle quelle dans webhooks_recus.jsonl (pour diagnostic).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from . import db
from .config import Settings
from .engine import OutreachEngine

log = logging.getLogger("prospectia.webhook")
MAX_AGE_SECONDS = 600


def verify_v2_signature(secret: str, header: str, raw_body: bytes, now: float | None = None) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = parts["t"].strip(), parts["v0"].strip()
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - int(ts)) > MAX_AGE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _first(*values):
    for v in values:
        if v not in (None, "", {}):
            return v
    return None


def _dig(d: Any, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def normalize_v2(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Traduit un événement v2 ({type, payload}) dans le format attendu par le moteur.

    Les noms de champs des événements v2 ne sont pas encore tous documentés par Unipile :
    on cherche donc chaque information à plusieurs endroits possibles.
    """
    etype = _first(event.get("type"), event.get("event"), "")
    p = _first(event.get("payload"), event.get("data"), event.get("object"), {}) or {}
    me = _first(event.get("account_user_id"), _dig(event, "account", "user_id"))
    if etype == "relation.new":
        user = p.get("user") if isinstance(p.get("user"), dict) else p
        return "new_relation", {
            "user_provider_id": _first(user.get("id"), user.get("user_id"), user.get("provider_id")),
            "user_public_identifier": _first(user.get("public_identifier"), user.get("public_id")),
        }
    if etype == "message.new":
        sender = p.get("sender") if isinstance(p.get("sender"), dict) else {}
        return "message_received", {
            "chat_id": _first(p.get("chat_id"), _dig(p, "chat", "id")),
            "message": _first(p.get("text"), p.get("body"), p.get("message"), ""),
            "is_sender": bool(_first(p.get("is_sender"), p.get("from_me"), p.get("is_outgoing"), False)),
            "sender": {"attendee_provider_id": _first(p.get("sender_id"), sender.get("id"), sender.get("user_id"),
                                                      p.get("user_id"))},
            "account_info": {"user_id": me},
        }
    return etype, p


def create_app(settings: Settings, client=None) -> FastAPI:
    app = FastAPI(title="ProspectIA Outreach webhooks")

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/webhooks/unipile")
    async def unipile(request: Request) -> dict:
        raw = await request.body()
        secret = settings.webhook_secret
        sig = request.headers.get("unipile-signature", "")
        token = request.headers.get("x-webhook-token", "")
        ok = bool(secret) and (
            (sig and verify_v2_signature(secret, sig, raw)) or (token and hmac.compare_digest(token, secret)))
        if not ok:
            raise HTTPException(status_code=401, detail="signature ou jeton invalide")
        event = json.loads(raw or b"{}")
        with open("webhooks_recus.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

        if sig:  # format v2
            kind, payload = normalize_v2(event)
        else:    # format v1
            kind, payload = event.get("event", ""), event
        conn = db.connect(settings.db_path)
        try:
            engine = OutreachEngine(conn, settings, client)
            if kind == "new_relation":
                result = engine.handle_new_relation(payload)
            elif kind == "message_received":
                result = engine.handle_message(payload)
            else:
                result = f"événement ignoré : {kind}"
        finally:
            conn.close()
        log.info("webhook %s : %s", kind, result)
        return {"ok": True, "result": result}

    return app
