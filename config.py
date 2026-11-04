"""Réglages de ProspectIA Outreach.

Tous les réglages se font dans le fichier `.env` (voir `.env.example`).
Les valeurs par défaut sont volontairement PRUDENTES.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Lit un fichier .env simple (CLE=valeur) sans dépendance externe."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "oui", "on"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _ints(name: str, default: str) -> list[int]:
    return [int(x) for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    # --- Connexion Unipile ---
    unipile_version: str = "v2"      # "v2" = tableau de bord dashboardv2.unipile.com ; "v1" = ancien tableau de bord
    unipile_base_url: str = "https://api.unipile.com"   # v2 uniquement
    unipile_inbox_id: str = "CLASSIC_PRIMARY"            # v2 : boîte LinkedIn classique
    unipile_dsn: str = ""            # ex : api8.unipile.com:13851 (donné dans le tableau de bord Unipile)
    unipile_api_key: str = ""
    unipile_account_id: str = ""     # l'identifiant du compte LinkedIn connecté dans Unipile
    webhook_secret: str = ""         # v2 : « secret » donné par Unipile à la création du webhook ; v1 : mot de passe inventé

    # --- Mode test ---
    dry_run: bool = True             # True = rien n'est envoyé, tout est seulement simulé et journalisé

    # --- Base de données ---
    db_path: str = "prospectia_outreach.db"

    # --- Garde-fous anti-suspension ---
    timezone: str = "Europe/Paris"
    work_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # 0 = lundi
    work_start_hour: int = 9
    work_end_hour: int = 18
    max_invites_per_day: int = 15
    max_invites_per_week: int = 80
    max_messages_per_day: int = 30
    max_searches_per_day: int = 10   # recherches LinkedIn (lecture) par jour
    warmup_start_invites: int = 5    # 1er jour actif : 5 invitations, puis +2 par jour actif
    warmup_step: int = 2
    min_delay_seconds: int = 90      # pause aléatoire entre deux actions
    max_delay_seconds: int = 300
    max_actions_per_run: int = 3     # le moteur tourne toutes les ~15 min et fait peu d'actions à chaque fois

    # --- Séquence ---
    min_score: int = 7               # validation automatique : score >= 7/10 ...
    require_verified_role: bool = True  # ... ET poste vérifié
    followup_days: list[int] = field(default_factory=lambda: [5, 10])  # relances J+5 et J+10
    invite_expiry_days: int = 21     # invitation non acceptée après 21 jours -> abandon
    note_max_chars: int = 200        # longueur max de la note d'invitation (prudent ; à ajuster)
    use_inmail: bool = False         # True seulement avec Sales Navigator / Premium

    # --- Conformité ---
    footer_fr: str = (
        "\n\n—\nVos coordonnées professionnelles proviennent de sources publiques ({source}). "
        "Pour ne plus être contacté(e), répondez simplement STOP."
    )
    stop_keywords: list[str] = field(default_factory=lambda: [
        "stop", "désinscri", "desinscri", "ne plus me contacter", "pas intéressé", "pas interesse",
        "unsubscribe", "remove me",
    ])


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        _load_dotenv(Path(env_file))
    s = Settings()
    s.unipile_version = os.getenv("UNIPILE_VERSION", s.unipile_version).strip().lower()
    s.unipile_base_url = os.getenv("UNIPILE_BASE_URL", s.unipile_base_url)
    s.unipile_inbox_id = os.getenv("UNIPILE_INBOX_ID", s.unipile_inbox_id)
    s.unipile_dsn = os.getenv("UNIPILE_DSN", s.unipile_dsn)
    s.unipile_api_key = os.getenv("UNIPILE_API_KEY", s.unipile_api_key)
    s.unipile_account_id = os.getenv("UNIPILE_ACCOUNT_ID", s.unipile_account_id)
    s.webhook_secret = os.getenv("WEBHOOK_SECRET", s.webhook_secret)
    s.dry_run = _bool("DRY_RUN", s.dry_run)
    s.db_path = os.getenv("DB_PATH", s.db_path)
    s.timezone = os.getenv("TIMEZONE", s.timezone)
    s.work_days = _ints("WORK_DAYS", ",".join(map(str, s.work_days)))
    s.work_start_hour = _int("WORK_START_HOUR", s.work_start_hour)
    s.work_end_hour = _int("WORK_END_HOUR", s.work_end_hour)
    s.max_invites_per_day = _int("MAX_INVITES_PER_DAY", s.max_invites_per_day)
    s.max_invites_per_week = _int("MAX_INVITES_PER_WEEK", s.max_invites_per_week)
    s.max_messages_per_day = _int("MAX_MESSAGES_PER_DAY", s.max_messages_per_day)
    s.max_searches_per_day = _int("MAX_SEARCHES_PER_DAY", s.max_searches_per_day)
    s.warmup_start_invites = _int("WARMUP_START_INVITES", s.warmup_start_invites)
    s.warmup_step = _int("WARMUP_STEP", s.warmup_step)
    s.min_delay_seconds = _int("MIN_DELAY_SECONDS", s.min_delay_seconds)
    s.max_delay_seconds = _int("MAX_DELAY_SECONDS", s.max_delay_seconds)
    s.max_actions_per_run = _int("MAX_ACTIONS_PER_RUN", s.max_actions_per_run)
    s.min_score = _int("MIN_SCORE", s.min_score)
    s.require_verified_role = _bool("REQUIRE_VERIFIED_ROLE", s.require_verified_role)
    s.followup_days = _ints("FOLLOWUP_DAYS", ",".join(map(str, s.followup_days)))
    s.invite_expiry_days = _int("INVITE_EXPIRY_DAYS", s.invite_expiry_days)
    s.note_max_chars = _int("NOTE_MAX_CHARS", s.note_max_chars)
    s.use_inmail = _bool("USE_INMAIL", s.use_inmail)
    return s
