"""Moteur de séquence : validation auto -> invitation -> 1er message -> relances -> arrêt.

Le moteur est appelé régulièrement (ex. toutes les 15 min). À chaque passage il fait PEU d'actions,
espacées de pauses aléatoires, uniquement pendant les heures ouvrées et dans la limite des quotas.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from . import db, guards
from .config import Settings
from .unipile_client import AccountProblemError, RateLimitError, UnipileError

log = logging.getLogger("prospectia.engine")


class OutreachEngine:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, client: Any,
                 now: Callable[[], datetime] = db.utcnow, sleep: Callable[[float], None] = time.sleep):
        self.conn, self.s, self.client, self.now, self.sleep = conn, settings, client, now, sleep

    # =====================================================================
    # Passage principal
    # =====================================================================
    def run_once(self) -> dict[str, Any]:
        report: dict[str, Any] = {"actions": [], "skipped_reason": None}
        paused, reason = db.is_paused(self.conn)
        if paused:
            report["skipped_reason"] = f"en pause : {reason}"
            return report

        self.auto_review()
        self.expire_old_invitations()

        now = self.now()
        if not guards.in_working_hours(now, self.s):
            report["skipped_reason"] = "hors heures ouvrées"
            return report

        budget = self.s.max_actions_per_run
        self._acted = False
        try:
            for step in (self._next_first_message, self._next_followup, self._next_invitation):
                while budget > 0:
                    done = step()
                    if not done:
                        break
                    report["actions"].append(done)
                    budget -= 1
        except (RateLimitError, AccountProblemError) as exc:
            db.pause(self.conn, f"{type(exc).__name__}: {exc}")
            report["skipped_reason"] = f"pause automatique : {exc}"
            log.error("Pause automatique : %s", exc)
        return report

    # =====================================================================
    # Étapes sans appel réseau
    # =====================================================================
    def auto_review(self) -> int:
        """Nouveaux prospects -> 'approved' si score et poste vérifié OK, sinon 'to_review' (contrôle humain)."""
        n = 0
        for p in self.conn.execute("SELECT * FROM prospects WHERE status = ?", (db.NEW,)).fetchall():
            if db.is_suppressed(self.conn, linkedin_slug=p["public_identifier"], email=p["email"]):
                self._upd(p["id"], status=db.OPTED_OUT)
                continue
            ok = p["score"] >= self.s.min_score and (p["role_verified"] or not self.s.require_verified_role)
            ok = ok and bool(db.get_message(self.conn, p["id"], "first"))
            self._upd(p["id"], status=db.APPROVED if ok else db.TO_REVIEW)
            n += ok
        return n

    def expire_old_invitations(self) -> None:
        limit = db.iso(self.now() - timedelta(days=self.s.invite_expiry_days))
        for p in self.conn.execute("SELECT id FROM prospects WHERE status = ? AND invited_at < ?",
                                   (db.INVITED, limit)).fetchall():
            self._upd(p["id"], status=db.NO_RESPONSE)
            self._log("invite_expired", p["id"])

    # =====================================================================
    # Actions (une action par appel ; renvoie une description ou None)
    # =====================================================================
    def _next_first_message(self) -> str | None:
        if guards.messages_remaining(self.conn, self.now(), self.s) <= 0:
            return None
        p = self.conn.execute("SELECT * FROM prospects WHERE status = ? ORDER BY connected_at LIMIT 1",
                              (db.CONNECTED,)).fetchone()
        if not p or self._blocked(p):
            return None
        first = db.get_message(self.conn, p["id"], "first")
        if not first:
            self._upd(p["id"], status=db.TO_REVIEW, error="1er message manquant : à écrire avant l'envoi")
            return None
        return self._send_first(p, self._with_footer(p, first), inmail=False)

    def _next_followup(self) -> str | None:
        if guards.messages_remaining(self.conn, self.now(), self.s) <= 0:
            return None
        now = self.now()
        gaps = self._followup_gaps()
        for p in self.conn.execute("SELECT * FROM prospects WHERE status = ? AND step BETWEEN 1 AND ? "
                                   "ORDER BY last_message_at", (db.MESSAGED, len(gaps))).fetchall():
            last = db.parse_iso(p["last_message_at"])
            if not last or now - last < timedelta(days=gaps[p["step"] - 1]):
                continue
            if self._blocked(p):
                continue
            kind = f"followup{p['step']}"
            text = db.get_message(self.conn, p["id"], kind)
            if not text or not p["chat_id"]:
                self._upd(p["id"], status=db.DONE)
                continue
            try:
                self._before_send()
                self.client.send_message(p["chat_id"], text)
            except (RateLimitError, AccountProblemError):
                raise
            except UnipileError as exc:
                return self._fail(p, exc)
            new_step = p["step"] + 1
            final = new_step > len(gaps)
            self._upd(p["id"], step=new_step, last_message_at=db.iso(now),
                               status=db.DONE if final else db.MESSAGED)
            self._log("message_sent", p["id"], kind, dry_run=self.s.dry_run)
            return f"{kind} -> {p['full_name']}"
        return None

    def _next_invitation(self) -> str | None:
        p = self.conn.execute("SELECT * FROM prospects WHERE status = ? ORDER BY score DESC, id LIMIT 1",
                              (db.APPROVED,)).fetchone()
        if not p or self._blocked(p):
            return None
        if not db.get_message(self.conn, p["id"], "first"):
            self._upd(p["id"], status=db.TO_REVIEW, error="1er message manquant : à écrire avant l'envoi")
            return None

        if p["channel"] == "inmail" and self.s.use_inmail:
            if guards.messages_remaining(self.conn, self.now(), self.s) <= 0:
                return None
            text = self._with_footer(p, db.get_message(self.conn, p["id"], "first"))
            return self._send_first(p, text, inmail=True)

        if guards.invites_remaining(self.conn, self.now(), self.s) <= 0:
            return None
        note = db.get_message(self.conn, p["id"], "note") or ""
        if len(note) > self.s.note_max_chars:
            self._upd(p["id"], status=db.TO_REVIEW,
                               error=f"note trop longue ({len(note)} > {self.s.note_max_chars} caractères)")
            return None
        try:
            provider_id = self._provider_id(p)
            self._before_send()
            self.client.send_invitation(provider_id, note or None)
        except (RateLimitError, AccountProblemError):
            raise
        except UnipileError as exc:
            return self._fail(p, exc)
        now = self.now()
        self._upd(p["id"], status=db.INVITED, invited_at=db.iso(now))
        self._log("invite_sent", p["id"], dry_run=self.s.dry_run)
        return f"invitation -> {p['full_name']}"

    # =====================================================================
    # Webhooks Unipile
    # =====================================================================
    def handle_new_relation(self, payload: dict[str, Any]) -> str:
        p = db.find_prospect(self.conn, provider_id=payload.get("user_provider_id"),
                             public_identifier=payload.get("user_public_identifier"))
        if not p:
            return "relation inconnue (pas un prospect)"
        if p["status"] != db.INVITED:
            return f"ignoré (statut {p['status']})"
        self._upd(p["id"], status=db.CONNECTED, connected_at=db.iso(self.now()),
                           provider_id=payload.get("user_provider_id") or p["provider_id"])
        self._log("connected", p["id"])
        return f"connecté : {p['full_name']}"

    def handle_message(self, payload: dict[str, Any]) -> str:
        sender = payload.get("sender") or {}
        sender_id = sender.get("attendee_provider_id")
        me = (payload.get("account_info") or {}).get("user_id")
        if payload.get("is_sender") or (me and sender_id == me):
            return "message envoyé par nous : ignoré"
        p = db.find_prospect(self.conn, provider_id=sender_id, chat_id=payload.get("chat_id"))
        if not p:
            return "expéditeur inconnu (pas un prospect)"
        text = payload.get("message") or ""
        fields: dict[str, Any] = {"chat_id": payload.get("chat_id") or p["chat_id"], "provider_id": sender_id or p["provider_id"]}
        if self._is_opt_out(text):
            db.suppress(self.conn, linkedin_slug=p["public_identifier"], email=p["email"], reason="STOP LinkedIn")
            self._upd(p["id"], status=db.OPTED_OUT, **fields)
            self._log("opt_out", p["id"], text[:200])
            return f"désinscription : {p['full_name']}"
        self._upd(p["id"], status=db.REPLIED, replied_at=db.iso(self.now()), **fields)
        self._log("replied", p["id"], text[:200])
        return f"RÉPONSE de {p['full_name']} -> à traiter par un humain"

    # =====================================================================
    # Outils internes
    # =====================================================================
    def _send_first(self, p: sqlite3.Row, text: str, inmail: bool) -> str | None:
        try:
            provider_id = self._provider_id(p)
            self._before_send()
            res = self.client.start_chat(provider_id, text, inmail=inmail)
        except (RateLimitError, AccountProblemError):
            raise
        except UnipileError as exc:
            return self._fail(p, exc)
        now = self.now()
        self._upd(p["id"], status=db.MESSAGED, step=1, chat_id=res.get("chat_id"),
                           last_message_at=db.iso(now))
        self._log("inmail_sent" if inmail else "message_sent", p["id"], "first", dry_run=self.s.dry_run)
        return f"{'InMail' if inmail else 'premier message'} -> {p['full_name']}"

    def _before_send(self) -> None:
        """Pause « humaine » aléatoire entre deux envois (pas avant le premier du passage)."""
        if getattr(self, "_acted", False):
            guards.human_pause(self.s, self.sleep)
        self._acted = True

    def _log(self, kind: str, prospect_id: int | None = None, detail: str = "", dry_run: bool = False) -> None:
        db.log_event(self.conn, kind, prospect_id, detail, dry_run=dry_run, now=self.now())

    def _upd(self, prospect_id: int, **fields) -> None:
        db.update_prospect(self.conn, prospect_id, now=self.now(), **fields)

    def _provider_id(self, p: sqlite3.Row) -> str:
        if p["provider_id"]:
            return p["provider_id"]
        profile = self.client.get_profile(p["public_identifier"])
        pid = profile.get("provider_id")
        if not pid:
            raise UnipileError(404, "profile_not_found", p["public_identifier"])
        self._upd(p["id"], provider_id=pid)
        return pid

    def _blocked(self, p: sqlite3.Row) -> bool:
        """Dernière vérification juste avant l'envoi : liste STOP."""
        if db.is_suppressed(self.conn, linkedin_slug=p["public_identifier"], email=p["email"]):
            self._upd(p["id"], status=db.OPTED_OUT)
            return True
        return False

    def _fail(self, p: sqlite3.Row, exc: UnipileError) -> str:
        self._upd(p["id"], status=db.ERROR, error=str(exc)[:300])
        self._log("error", p["id"], str(exc)[:300])
        log.warning("Erreur pour %s : %s", p["full_name"], exc)
        return f"erreur -> {p['full_name']} ({exc.status})"

    def _with_footer(self, p: sqlite3.Row, text: str | None) -> str:
        source = p["source_label"] or "site internet de votre organisation"
        return (text or "").rstrip() + self.s.footer_fr.format(source=source)

    def _followup_gaps(self) -> list[int]:
        days = sorted(self.s.followup_days)
        return [d - prev for prev, d in zip([0] + days[:-1], days)]

    def _is_opt_out(self, text: str) -> bool:
        t = text.lower()
        for kw in self.s.stop_keywords:
            if kw == "stop":
                if re.search(r"\bstop\b", t):
                    return True
            elif kw in t:
                return True
        return False
