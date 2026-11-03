"""Clients pour l'API Unipile : v1 (UnipileClient) et v2 (UnipileV2Client, celle du tableau de bord dashboardv2).

Documentation : https://developer.unipile.com/docs
- Profil       : GET  /api/v1/users/{identifier}?account_id=...
- Invitation   : POST /api/v1/users/invite            (JSON : provider_id, account_id, message)
- Nouveau chat : POST /api/v1/chats                   (multipart : account_id, attendees_ids, text, linkedin[inmail])
- Message      : POST /api/v1/chats/{chat_id}/messages (multipart : text)
- Webhook      : POST /api/v1/webhooks                (JSON : source, request_url, name, headers, events)

Tous les appels réseau passent par cette classe : si Unipile change son API (une v2 existe),
c'est le seul fichier à modifier.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import requests

log = logging.getLogger("prospectia.unipile")


class UnipileError(Exception):
    def __init__(self, status: int, error_type: str = "", detail: str = ""):
        self.status, self.error_type, self.detail = status, error_type, detail
        super().__init__(f"Unipile {status} {error_type} {detail}".strip())


class RateLimitError(UnipileError):
    """LinkedIn/Unipile signale une limite atteinte -> le moteur se met en pause."""


class AccountProblemError(UnipileError):
    """Compte déconnecté, vérification de sécurité LinkedIn, etc. -> pause immédiate."""


class UnipileClient:
    def __init__(self, dsn: str, api_key: str, account_id: str, timeout: int = 30):
        if not (dsn and api_key and account_id):
            raise ValueError("UNIPILE_DSN, UNIPILE_API_KEY et UNIPILE_ACCOUNT_ID doivent être renseignés dans .env")
        dsn = dsn.removeprefix("https://").removeprefix("http://").rstrip("/")
        self.base = f"https://{dsn}/api/v1"
        self.account_id = account_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})

    # ----- bas niveau -----
    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = self.session.request(method, f"{self.base}{path}", timeout=self.timeout, **kwargs)
        if resp.status_code < 400:
            return resp.json() if resp.content else {}
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        etype, detail = str(body.get("type", "")), str(body.get("detail", body.get("title", "")))
        text = f"{etype} {detail}".lower()
        if resp.status_code == 429 or "limit" in text or "too_many" in text:
            raise RateLimitError(resp.status_code, etype, detail)
        if resp.status_code in (401, 403) or "disconnected" in text or "checkpoint" in text:
            raise AccountProblemError(resp.status_code, etype, detail)
        raise UnipileError(resp.status_code, etype, detail)

    # ----- actions -----
    def get_profile(self, identifier: str) -> dict[str, Any]:
        """identifier = slug public (jean-dupont-123) ou provider_id. Renvoie notamment 'provider_id'."""
        return self._request("GET", f"/users/{identifier}", params={"account_id": self.account_id})

    def send_invitation(self, provider_id: str, message: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"provider_id": provider_id, "account_id": self.account_id}
        if message:
            payload["message"] = message
        return self._request("POST", "/users/invite", json=payload)

    def start_chat(self, provider_id: str, text: str, inmail: bool = False) -> dict[str, Any]:
        """Premier message. Sans InMail, LinkedIn n'accepte que les relations (1er degré)."""
        form = {"account_id": (None, self.account_id), "text": (None, text), "attendees_ids": (None, provider_id)}
        if inmail:
            form["linkedin[inmail]"] = (None, "true")
        return self._request("POST", "/chats", files=form)  # files=... force le multipart/form-data

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        return self._request("POST", f"/chats/{chat_id}/messages", files={"text": (None, text)})

    def create_webhook(self, source: str, request_url: str, name: str, secret: str,
                       events: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": source,  # 'users' (new_relation) ou 'messaging' (message_received)
            "request_url": request_url,
            "name": name,
            "account_ids": [self.account_id],
            "headers": [{"key": "Content-Type", "value": "application/json"},
                        {"key": "X-Webhook-Token", "value": secret}],
        }
        if events:
            payload["events"] = events
        return self._request("POST", "/webhooks", json=payload)


class UnipileV2Client:
    """Client pour Unipile v2 (tableau de bord dashboardv2.unipile.com).

    Base : https://api.unipile.com/v2, en-tête X-API-KEY, corps JSON.
    - Compte       : GET  /v2/{account_id}  (via /v2/accounts/{account_id})
    - Profil       : GET  /v2/{account_id}/users/{identifiant}          -> champ 'id'
    - Invitation   : POST /v2/{account_id}/users/me/relation-requests    {user_id, message}
    - Nouveau chat : POST /v2/{account_id}/inboxes/{inbox_id}/chats/send {users_ids, text, specifics}
    - Message      : POST /v2/{account_id}/chats/{chat_id}/messages/send {text}
    - Webhooks     : POST /v2/webhooks/endpoints                         {url, trigger_events, account_ids}
    La v2 est en bêta chez Unipile : si un appel change, c'est ici qu'il faut corriger.
    """

    def __init__(self, api_key: str, account_id: str, base_url: str = "https://api.unipile.com",
                 inbox_id: str = "CLASSIC_PRIMARY", timeout: int = 30):
        if not (api_key and account_id):
            raise ValueError("UNIPILE_API_KEY et UNIPILE_ACCOUNT_ID doivent être renseignés dans .env")
        if not account_id.startswith("acc_"):
            raise ValueError(f"UNIPILE_ACCOUNT_ID doit commencer par 'acc_' (reçu : {account_id[:12]}…)")
        self.root = base_url.rstrip("/") + "/v2"
        self.account_id = account_id
        self.inbox_id = inbox_id
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-KEY": api_key, "Accept": "application/json"})

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        return UnipileClient._request(self, method, path, **kwargs)  # même gestion d'erreurs que v1

    @property
    def base(self) -> str:  # utilisé par _request
        return self.root

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", f"/accounts/{self.account_id}")

    def get_profile(self, identifier: str) -> dict[str, Any]:
        res = self._request("GET", f"/{self.account_id}/users/{identifier}")
        res.setdefault("provider_id", res.get("id"))  # le moteur attend 'provider_id'
        return res

    def send_invitation(self, provider_id: str, message: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"user_id": provider_id}
        if message:
            body["message"] = message
        return self._request("POST", f"/{self.account_id}/users/me/relation-requests", json=body)

    def start_chat(self, provider_id: str, text: str, inmail: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"users_ids": provider_id, "text": text}
        if inmail:
            body["specifics"] = {"linkedin": {"classic": {"inmail": True}}}
        res = self._request("POST", f"/{self.account_id}/inboxes/{self.inbox_id}/chats/send", json=body)
        chat = res.get("chat") if isinstance(res.get("chat"), dict) else {}
        res.setdefault("chat_id", res.get("chat_id") or chat.get("id") or res.get("id"))
        return res

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        return self._request("POST", f"/{self.account_id}/chats/{chat_id}/messages/send", json={"text": text})

    def search_people(self, *, url: str | None = None, keywords: str | None = None,
                      cursor: str | None = None) -> dict[str, Any]:
        """Recherche de personnes sur LinkedIn (lecture seule).

        - url      : adresse d'une page de résultats LinkedIn (linkedin.com/search/results/people/?...)
                     -> POST /v2/{account_id}/linkedin/search  {url}
        - keywords : mots-clés simples -> POST /v2/{account_id}/linkedin/search/people {keywords}
        - cursor   : page suivante (renvoyé par l'appel précédent)
        """
        if cursor:
            path = "/linkedin/search" if url else "/linkedin/search/people"
            return self._request("POST", f"/{self.account_id}{path}", json={"cursor": cursor})
        if url:
            return self._request("POST", f"/{self.account_id}/linkedin/search", json={"url": url})
        return self._request("POST", f"/{self.account_id}/linkedin/search/people", json={"keywords": keywords or ""})

    def create_webhook_endpoint(self, url: str, events: list[str]) -> dict[str, Any]:
        """Crée UN point de réception pour tous les événements. La réponse contient le 'secret'."""
        return self._request("POST", "/webhooks/endpoints", json={
            "url": url, "trigger_events": events, "account_ids": [self.account_id],
            "description": "ProspectIA Outreach"})


class DryRunClient:
    """Faux client : n'envoie rien, journalise ce qui serait envoyé. Utilisé quand DRY_RUN=true."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def _record(self, action: str, **data) -> None:
        self.sent.append((action, data))
        log.info("[SIMULATION] %s %s", action, {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
                                                 for k, v in data.items()})

    def get_profile(self, identifier: str) -> dict[str, Any]:
        return {"provider_id": f"SIM_{identifier}", "public_identifier": identifier}

    def send_invitation(self, provider_id: str, message: str | None) -> dict[str, Any]:
        self._record("invitation", provider_id=provider_id, message=message or "")
        return {"object": "UserInvitationSent", "invitation_id": f"sim_{uuid.uuid4().hex[:8]}"}

    def start_chat(self, provider_id: str, text: str, inmail: bool = False) -> dict[str, Any]:
        self._record("inmail" if inmail else "premier_message", provider_id=provider_id, text=text)
        return {"object": "ChatStarted", "chat_id": f"simchat_{uuid.uuid4().hex[:8]}"}

    def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        self._record("relance", chat_id=chat_id, text=text)
        return {"object": "MessageSent", "message_id": f"simmsg_{uuid.uuid4().hex[:8]}"}

    def create_webhook(self, *args, **kwargs) -> dict[str, Any]:
        self._record("webhook", **kwargs)
        return {"object": "WebhookCreated", "webhook_id": "sim"}

    def create_webhook_endpoint(self, url: str, events: list[str]) -> dict[str, Any]:
        self._record("webhook", url=url)
        return {"object": "WebhookEndpoint", "id": "we_sim", "secret": "sim-secret"}

    def get_account(self) -> dict[str, Any]:
        return {"id": "acc_sim", "type": "LINKEDIN", "simulation": True}
