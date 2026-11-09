
"""
Serveur ProspectIA
Recherche de profils LinkedIn publics via Tavily.
Filtrage géographique strict par département.
Suivi des profils nouveaux, visités et contactés.

Variable obligatoire :
TAVILY_API_KEY

Variable recommandée sur Render :
PROSPECTIA_DB_PATH=/var/data/prospectia.db

Sur Render :
- Ajouter TAVILY_API_KEY dans Environment.
- Ajouter un disque persistant monté sur /var/data.
- Définir PROSPECTIA_DB_PATH=/var/data/prospectia.db.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit, parse_qs
from urllib.request import Request, urlopen


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

PORT = int(os.environ.get("PORT", "8765"))

TAVILY_ENDPOINT = "https://api.tavily.com/search"

TAVILY_TIMEOUT = 25

CACHE_TTL_SECONDS = 1800

MAX_RESULTS = 20

DB_PATH = Path(
    os.environ.get(
        "PROSPECTIA_DB_PATH",
        str(ROOT / "prospectia.db")
    )
)


# ============================================================
# RÔLES PAR TYPE DE PROJET
# ============================================================

ROLES = {

    "fund": [
        "directeur de la levée de fonds",
        "responsable levée de fonds",
        "responsable fundraising",
        "fundraiser",
        "directeur du développement",
        "responsable développement",
        "directeur des ressources",
        "responsable ressources financières",
        "responsable collecte de fonds",
        "responsable grands donateurs",
        "responsable philanthropie",
        "directeur de fondation",
        "directeur fondation",
        "responsable fondation",
        "chargé de mécénat",
        "responsable mécénat",
        "directeur mécénat",
        "responsable partenariats",
        "directeur partenariats",
        "responsable partenariats financiers",
        "responsable relations entreprises",
        "directeur relations entreprises",
        "responsable sponsoring",
        "responsable RSE",
        "directeur RSE",
        "responsable engagement sociétal",
        "responsable relations donateurs",
        "responsable développement philanthropique",
        "responsable financement",
        "directeur financement",
        "chargé de fundraising",
        "chargé de développement",
        "responsable communication et collecte",
        "directeur administratif et financier"
    ],

    "corner": [
        "directeur des ressources humaines",
        "DRH",
        "responsable ressources humaines",
        "responsable RH",
        "directeur RH",
        "responsable qualité de vie au travail",
        "responsable QVT",
        "responsable bien-être au travail",
        "responsable expérience collaborateur",
        "directeur expérience collaborateur",
        "responsable communication interne",
        "directeur communication interne",
        "responsable services généraux",
        "directeur services généraux",
        "facility manager",
        "office manager",
        "responsable environnement de travail",
        "responsable événementiel",
        "directeur événementiel",
        "responsable animation interne",
        "responsable marque employeur",
        "directeur marque employeur",
        "responsable engagement collaborateurs",
        "chief happiness officer",
        "responsable achats indirects",
        "responsable développement RH",
        "responsable formation",
        "responsable avantages sociaux",
        "responsable politique sociale",
        "responsable prévention des risques psychosociaux",
        "responsable conditions de travail",
        "responsable relations sociales",
        "responsable communication RH",
        "responsable culture d'entreprise"
    ],

    "custom": [
        "directeur général",
        "directeur",
        "directeur de développement",
        "directeur commercial",
        "responsable commercial",
        "directeur des partenariats",
        "responsable partenariats",
        "responsable communication",
        "directeur communication",
        "responsable marketing",
        "directeur marketing",
        "responsable innovation",
        "directeur innovation",
        "responsable projets",
        "directeur de projets",
        "chef de projet",
        "responsable développement commercial",
        "directeur développement commercial",
        "responsable relations institutionnelles",
        "directeur relations institutionnelles",
        "responsable stratégie",
        "directeur stratégie",
        "responsable développement",
        "directeur développement",
        "responsable affaires publiques",
        "directeur affaires publiques",
        "responsable mécénat",
        "responsable RSE",
        "responsable événementiel",
        "responsable expérience client",
        "responsable relations entreprises",
        "directeur administratif et financier",
        "secrétaire général",
        "président",
        "fondateur",
        "cofondateur"
    ]
}


# ============================================================
# DÉPARTEMENTS ET ALIAS GÉOGRAPHIQUES
# ============================================================

DEPARTMENTS = {

    "75": {
        "name": "Paris",
        "aliases": [
            "Paris", "Paris 1er", "Paris 2e",
            "Paris 3e", "Paris 4e", "Paris 5e",
            "Paris 6e", "Paris 7e", "Paris 8e",
            "Paris 9e", "Paris 10e", "Paris 11e",
            "Paris 12e", "Paris 13e", "Paris 14e",
            "Paris 15e", "Paris 16e", "Paris 17e",
            "Paris 18e", "Paris 19e", "Paris 20e"
        ],
        "postal": ["75"]
    },

    "77": {
        "name": "Seine-et-Marne",
        "aliases": [
            "Melun", "Meaux", "Chelles",
            "Fontainebleau", "Provins", "Torcy",
            "Coulommiers", "Savigny-le-Temple",
            "Bussy-Saint-Georges", "Lagny-sur-Marne",
            "Nemours", "Pontault-Combault",
            "Roissy-en-Brie", "Montereau-Fault-Yonne",
            "Dammarie-les-Lys"
        ],
        "postal": ["77"]
    },

    "78": {
        "name": "Yvelines",
        "aliases": [
            "Versailles", "Saint-Germain-en-Laye",
            "Mantes-la-Jolie", "Poissy", "Sartrouville",
            "Rambouillet", "Plaisir", "Trappes",
            "Guyancourt", "Montigny-le-Bretonneux",
            "Les Mureaux", "Conflans-Sainte-Honorine",
            "Houilles", "Chatou", "Le Chesnay",
            "Vélizy-Villacoublay", "Élancourt",
            "Maurepas", "Carrières-sous-Poissy",
            "Maisons-Laffitte", "Viroflay",
            "Marly-le-Roi", "Bois-d'Arcy",
            "Saint-Cyr-l'École", "Aubergenville"
        ],
        "postal": ["78"]
    },

    "91": {
        "name": "Essonne",
        "aliases": [
            "Évry-Courcouronnes", "Évry",
            "Corbeil-Essonnes", "Massy", "Palaiseau",
            "Étampes", "Les Ulis", "Savigny-sur-Orge",
            "Athis-Mons", "Sainte-Geneviève-des-Bois",
            "Yerres", "Brunoy", "Draveil",
            "Ris-Orangis", "Viry-Châtillon",
            "Grigny", "Longjumeau", "Arpajon",
            "Gif-sur-Yvette", "Orsay", "Montgeron",
            "Dourdan"
        ],
        "postal": ["91"]
    },

    "92": {
        "name": "Hauts-de-Seine",
        "aliases": [
            "Nanterre", "Boulogne-Billancourt",
            "Courbevoie", "Levallois-Perret",
            "Neuilly-sur-Seine", "Issy-les-Moulineaux",
            "Antony", "Colombes", "Asnières-sur-Seine",
            "Rueil-Malmaison", "Puteaux", "Suresnes",
            "Clamart", "Montrouge", "Clichy",
            "La Défense", "Gennevilliers", "Châtillon",
            "Malakoff", "Vanves", "Meudon",
            "Saint-Cloud", "Châtenay-Malabry",
            "Bagneux", "Bois-Colombes", "Garches"
        ],
        "postal": ["92"]
    },

    "93": {
        "name": "Seine-Saint-Denis",
        "aliases": [
            "Bobigny", "Saint-Denis", "Montreuil",
            "Aubervilliers", "Aulnay-sous-Bois",
            "Drancy", "Pantin", "Noisy-le-Grand",
            "Bondy", "Rosny-sous-Bois", "La Courneuve",
            "Épinay-sur-Seine", "Sevran",
            "Le Blanc-Mesnil", "Saint-Ouen-sur-Seine",
            "Bagnolet", "Romainville", "Villepinte",
            "Livry-Gargan", "Neuilly-sur-Marne",
            "Gagny", "Stains"
        ],
        "postal": ["93"]
    },

    "94": {
        "name": "Val-de-Marne",
        "aliases": [
            "Créteil", "Vitry-sur-Seine",
            "Ivry-sur-Seine", "Villejuif", "Vincennes",
            "Saint-Maur-des-Fossés", "Maisons-Alfort",
            "Champigny-sur-Marne", "Choisy-le-Roi",
            "Alfortville", "Thiais", "Orly",
            "Charenton-le-Pont", "Cachan",
            "Le Kremlin-Bicêtre", "Nogent-sur-Marne",
            "Fontenay-sous-Bois", "Joinville-le-Pont",
            "Villeneuve-Saint-Georges", "Fresnes",
            "L'Haÿ-les-Roses", "Gentilly"
        ],
        "postal": ["94"]
    },

    "95": {
        "name": "Val-d'Oise",
        "aliases": [
            "Cergy", "Pontoise", "Argenteuil",
            "Sarcelles", "Garges-lès-Gonesse",
            "Franconville", "Ermont", "Taverny",
            "Herblay-sur-Seine", "Gonesse", "Bezons",
            "Enghien-les-Bains", "Deuil-la-Barre",
            "Montmorency", "Eaubonne", "Fosses",
            "Persan", "Saint-Ouen-l'Aumône",
            "Villiers-le-Bel", "Osny", "Domont",
            "L'Isle-Adam", "Soisy-sous-Montmorency"
        ],
        "postal": ["95"]
    }
}


# ============================================================
# BASE DE DONNÉES SQLITE
# ============================================================

@contextmanager
def get_db():

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    connection = sqlite3.connect(
        str(DB_PATH),
        timeout=20
    )

    connection.row_factory = sqlite3.Row

    try:

        connection.execute("PRAGMA busy_timeout = 20000")

        yield connection

        connection.commit()

    except Exception:

        connection.rollback()

        raise

    finally:

        connection.close()


def initialize_database():

    with get_db() as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_profiles (

                url TEXT PRIMARY KEY,

                title TEXT,

                status TEXT NOT NULL DEFAULT 'new',

                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,

                visited_at TEXT,

                contacted_at TEXT
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_linkedin_profiles_status
            ON linkedin_profiles(status)
        """)

        db.commit()

    logging.info(
        "Base de suivi initialisée : %s",
        DB_PATH
    )


def register_profile(url, title=""):

    url = normalize_linkedin_profile(url)

    if not url:
        return

    with get_db() as db:

        db.execute(
            """
            INSERT OR IGNORE INTO linkedin_profiles
            (url, title, status)
            VALUES (?, ?, 'new')
            """,
            (url, str(title or "")[:200])
        )


def get_profile_status(url):

    url = normalize_linkedin_profile(url)

    if not url:

        return {
            "visited": False,
            "status": "invalid",
            "visited_at": None,
            "contacted_at": None,
            "first_seen": None
        }

    with get_db() as db:

        profile = db.execute(
            """
            SELECT
                status,
                first_seen,
                visited_at,
                contacted_at
            FROM linkedin_profiles
            WHERE url = ?
            """,
            (url,)
        ).fetchone()

    if not profile:

        return {
            "visited": False,
            "status": "new",
            "first_seen": None,
            "visited_at": None,
            "contacted_at": None
        }

    return {

        "visited": profile["visited_at"] is not None,

        "status": profile["status"],

        "first_seen": profile["first_seen"],

        "visited_at": profile["visited_at"],

        "contacted_at": profile["contacted_at"]
    }


def mark_profile_visited(url):

    url = normalize_linkedin_profile(url)

    if not url:
        raise ValueError("URL LinkedIn invalide.")

    with get_db() as db:

        db.execute(
            """
            INSERT INTO linkedin_profiles
            (
                url,
                status,
                visited_at
            )
            VALUES (
                ?,
                'visited',
                CURRENT_TIMESTAMP
            )

            ON CONFLICT(url) DO UPDATE SET

                status = CASE
                    WHEN linkedin_profiles.status = 'contacted'
                    THEN 'contacted'
                    ELSE 'visited'
                END,

                visited_at = COALESCE(
                    linkedin_profiles.visited_at,
                    CURRENT_TIMESTAMP
                )
            """,
            (url,)
        )

    return get_profile_status(url)


def mark_profile_contacted(url):

    url = normalize_linkedin_profile(url)

    if not url:
        raise ValueError("URL LinkedIn invalide.")

    with get_db() as db:

        db.execute(
            """
            INSERT INTO linkedin_profiles
            (
                url,
                status,
                visited_at,
                contacted_at
            )
            VALUES (
                ?,
                'contacted',
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )

            ON CONFLICT(url) DO UPDATE SET

                status = 'contacted',

                visited_at = COALESCE(
                    linkedin_profiles.visited_at,
                    CURRENT_TIMESTAMP
                ),

                contacted_at = COALESCE(
                    linkedin_profiles.contacted_at,
                    CURRENT_TIMESTAMP
                )
            """,
            (url,)
        )

    return get_profile_status(url)


def list_profiles(status=None):

    allowed_statuses = (
        "new",
        "visited",
        "contacted"
    )

    with get_db() as db:

        if status and status not in allowed_statuses:

            raise ValueError("Statut de profil invalide.")

        if status:

            rows = db.execute(
                """
                SELECT *
                FROM linkedin_profiles
                WHERE status = ?
                ORDER BY first_seen DESC
                """,
                (status,)
            ).fetchall()

        else:

            rows = db.execute(
                """
                SELECT *
                FROM linkedin_profiles
                ORDER BY first_seen DESC
                """
            ).fetchall()

    return [

        {
            "url": row["url"],
            "title": row["title"],
            "status": row["status"],
            "first_seen": row["first_seen"],
            "visited_at": row["visited_at"],
            "contacted_at": row["contacted_at"],
            "visited": row["visited_at"] is not None
        }

        for row in rows
    ]


def get_profile_statistics():

    with get_db() as db:

        rows = db.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM linkedin_profiles
            GROUP BY status
            """
        ).fetchall()

    statistics = {
        "new": 0,
        "visited": 0,
        "contacted": 0,
        "total": 0
    }

    for row in rows:

        status = row["status"]

        if status in statistics:

            statistics[status] = row["total"]

            statistics["total"] += row["total"]

    return statistics


# ============================================================
# NORMALISATION DES URL LINKEDIN
# ============================================================

def normalize_linkedin_profile(url):

    try:

        parsed = urlsplit(str(url))

        host = (parsed.hostname or "").lower()

        if parsed.scheme.lower() not in ("https", "http"):
            return None

        if not (
            host == "linkedin.com"
            or host.endswith(".linkedin.com")
        ):
            return None

        if not parsed.path.startswith("/in/"):
            return None

        profile_id = parsed.path.split("/in/", 1)[1].strip("/")

        if not profile_id:
            return None

        # Suppression des paramètres de suivi et fragments.
        return urlunsplit((
            "https",
            "www.linkedin.com",
            "/in/" + profile_id,
            "",
            ""
        ))

    except Exception:

        return None


# ============================================================
# NORMALISATION ET VÉRIFICATION GÉOGRAPHIQUE
# ============================================================

def normalize_text(value):

    value = str(value or "").lower()

    value = unicodedata.normalize("NFD", value)

    value = "".join(
        char for char in value
        if unicodedata.category(char) != "Mn"
    )

    value = value.replace("œ", "oe")
    value = value.replace("æ", "ae")

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value
    )

    return " ".join(value.split())


def department_configuration(params):

    scope = str(
        params.get("geo_scope", "department")
    ).strip().lower()

    code = str(
        params.get("department", "78")
    ).strip()

    if scope in ("department", "yvelines"):

        if code not in DEPARTMENTS:
            code = "78"

        return code, DEPARTMENTS[code]

    return None, None



def geographic_terms(params):
    """Retourne les termes géographiques réellement demandés."""
    code, department = department_configuration(params)
    if department:
        return [department["name"], *department["aliases"]]

    scope = str(params.get("geo_scope", "")).strip().lower()
    if scope == "idf":
        return ["Île-de-France", "Paris", "Yvelines", "Hauts-de-Seine",
                "Seine-Saint-Denis", "Val-de-Marne", "Essonne",
                "Seine-et-Marne", "Val-d'Oise"]

    location = str(params.get("location", "")).strip()[:100]
    return [location] if location else []


def profile_text(item):
    """Texte exploitable retourné par le moteur de recherche."""
    return " ".join(
        str(item.get(key) or "")
        for key in ("title", "content", "raw_content")
    )


def contains_location(text, location):
    normalized_text = normalize_text(text)
    normalized_location = normalize_text(location)
    if not normalized_text or not normalized_location:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(normalized_location) + r"(?![a-z0-9])"
    return bool(re.search(pattern, normalized_text))


def profile_matches_department(item, department_code):
    """N'accepte que les résultats contenant une preuve textuelle du département."""
    department = DEPARTMENTS.get(str(department_code))
    if not department:
        return False

    text = profile_text(item)
    if not normalize_text(text):
        return False

    # Code postal complet du département (ex. 78000), pas le nombre 78 isolé.
    postal_pattern = r"(?<!\d)(?:" + "|".join(
        re.escape(code) for code in department["postal"]
    ) + r")\d{3}(?!\d)"
    if re.search(postal_pattern, text):
        return True

    normalized = normalize_text(text)
    code_pattern = (
        r"\b(?:departement|dept|dpt)\s*" +
        re.escape(str(department_code)) + r"\b"
    )
    if re.search(code_pattern, normalized):
        return True

    return any(contains_location(text, term)
               for term in [department["name"], *department["aliases"]])


def profile_matches_location(item, params):
    """Contrôle une localisation libre ou l'Île-de-France."""
    scope = str(params.get("geo_scope", "")).strip().lower()
    text = profile_text(item)

    if scope == "idf":
        return any(contains_location(text, term) for term in geographic_terms(params))

    location = str(params.get("location", "")).strip()
    return contains_location(text, location) if location else True


def role_matches_text(text, role):
    """Tolère les variantes d'intitulé tout en exigeant une vraie proximité."""
    wanted = normalize_text(role)
    haystack = normalize_text(text)
    if not wanted or not haystack:
        return False
    if wanted in haystack:
        return True

    stop_words = {"de", "du", "des", "la", "le", "les", "et", "en", "au", "aux"}
    tokens = [t for t in wanted.split() if len(t) > 2 and t not in stop_words]
    if not tokens:
        return False
    matched = sum(1 for token in tokens if token in haystack.split())
    return matched >= max(1, round(len(tokens) * 0.7))


def quoted_or(values):
    cleaned = []
    for value in values:
        value = str(value).strip()[:100]
        if value and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f'"{cleaned[0]}"'
    return "(" + " OR ".join(f'"{value}"' for value in cleaned[:4]) + ")"


def build_queries(params):
    """
    Construit les requêtes dans l'ordre de priorité :
    critères explicitement saisis d'abord, élargissement ensuite.
    """
    project = str(params.get("project", "fund")).strip()
    if project not in ROLES:
        project = "fund"

    role = str(params.get("role", "")).strip()[:100]
    company = str(params.get("company", "")).strip()[:100]
    keywords = str(params.get("keywords", "")).strip()[:120]
    scope = str(params.get("geo_scope", "yvelines")).strip().lower()

    code, department = department_configuration(params)
    if department:
        geo_values = [department["name"], *department["aliases"][:12]]
        primary_geo = department["name"]
    elif scope == "idf":
        geo_values = geographic_terms(params)
        primary_geo = "Île-de-France"
    else:
        geo_values = geographic_terms(params)
        primary_geo = geo_values[0] if geo_values else ""

    # La localisation principale et tous les critères saisis sont placés
    # dans la première requête, sans être noyés dans un catalogue de rôles.
    explicit_roles = [role] if role else ROLES[project][:8]
    role_groups = [explicit_roles[i:i + 3] for i in range(0, len(explicit_roles), 3)]

    queries = []

    def add_query(role_values, geo_values_for_query, include_all=True):
        parts = ["site:linkedin.com/in/"]
        role_clause = quoted_or(role_values)
        geo_clause = quoted_or(geo_values_for_query[:4])
        if role_clause:
            parts.append(role_clause)
        if geo_clause:
            parts.append(geo_clause)
        parts.append('"France"')
        if company:
            parts.append(f'"{company}"')
        if keywords:
            # Les mots-clés restent explicites dans chaque requête prioritaire.
            parts.append(keywords)
        query = " ".join(part for part in parts if part)
        query = " ".join(query.split())[:390]
        if query and query not in queries:
            queries.append(query)

    if primary_geo:
        add_query(explicit_roles[:3], [primary_geo])
    elif geo_values:
        add_query(explicit_roles[:3], geo_values[:4])
    else:
        add_query(explicit_roles[:3], [])

    # Si l'utilisateur a fourni des critères, élargir progressivement sans
    # abandonner les critères obligatoires.
    if department:
        for city in geo_values[1:]:
            add_query(explicit_roles[:3], [city])
    elif scope == "idf":
        for geo in geo_values[1:]:
            add_query(explicit_roles[:3], [geo])
    elif geo_values:
        for geo in geo_values[1:4]:
            add_query(explicit_roles[:3], [geo])

    # Quand aucun rôle précis n'est donné, les rôles du projet servent
    # uniquement de deuxième niveau d'élargissement.
    if not role:
        for group in role_groups[1:]:
            if primary_geo:
                add_query(group, [primary_geo])

    return queries[:18]


def score_profile(item, params):
    """
    Score sur 100, calculé uniquement à partir des critères renseignés.
    Les correspondances exactes dans le titre pèsent davantage que celles
    repérées dans le contenu descriptif.
    """
    title = normalize_text(item.get("title", ""))
    text = normalize_text(profile_text(item))
    role = str(params.get("role", "")).strip()
    company = str(params.get("company", "")).strip()
    keywords = str(params.get("keywords", "")).strip()

    weighted_score = 0.0
    total_weight = 0.0
    evidence = []

    def add_criterion(weight, value, label):
        nonlocal weighted_score, total_weight
        total_weight += weight
        weighted_score += weight * max(0.0, min(1.0, value))
        if value >= 0.72:
            evidence.append(label)

    if role:
        wanted = normalize_text(role)
        role_value = 0.0
        if wanted and wanted in title:
            role_value = 1.0
        elif wanted and wanted in text:
            role_value = 0.90
        else:
            tokens = [
                token for token in wanted.split()
                if len(token) > 2 and token not in {
                    "de", "du", "des", "la", "le", "les",
                    "et", "en", "au", "aux", "pour"
                }
            ]
            if tokens:
                hits = sum(1 for token in tokens if token in text.split())
                role_value = hits / len(tokens)
        add_criterion(35, role_value, "fonction")

    if company:
        wanted_company = normalize_text(company)
        company_value = 1.0 if wanted_company and wanted_company in title else (
            0.95 if wanted_company and wanted_company in text else 0.0
        )
        # Les noms composés peuvent apparaître sous une forme légèrement abrégée.
        if company_value == 0 and wanted_company:
            tokens = [t for t in wanted_company.split() if len(t) > 2]
            if tokens:
                hits = sum(1 for token in tokens if token in text.split())
                company_value = min(0.75, hits / len(tokens))
        add_criterion(25, company_value, "entreprise")

    if keywords:
        keyword_parts = [
            part.strip() for part in re.split(r"[,;|]+", keywords)
            if part.strip()
        ]
        keyword_values = []
        for part in keyword_parts:
            normalized_part = normalize_text(part)
            if not normalized_part:
                continue
            if normalized_part in title:
                keyword_values.append(1.0)
            elif normalized_part in text:
                keyword_values.append(0.9)
            else:
                tokens = [t for t in normalized_part.split() if len(t) > 2]
                if tokens:
                    hits = sum(1 for token in tokens if token in text.split())
                    keyword_values.append(hits / len(tokens))
                else:
                    keyword_values.append(0.0)
        keyword_value = (
            sum(keyword_values) / len(keyword_values)
            if keyword_values else 0.0
        )
        add_criterion(20, keyword_value, "mots-clés")

    scope = str(params.get("geo_scope", "")).strip().lower()
    code, department = department_configuration(params)
    geo_ok = True
    if department:
        geo_ok = profile_matches_department(item, code)
    elif scope in ("custom", "idf"):
        geo_ok = profile_matches_location(item, params)

    if scope in ("department", "yvelines", "idf", "custom"):
        add_criterion(20, 1.0 if geo_ok else 0.0, "localisation")

    if total_weight <= 0:
        return 0, evidence

    score = round((weighted_score / total_weight) * 100)
    return score, evidence


# ============================================================
# CACHE ET VERROUS
# ============================================================

_SEARCH_CACHE = {}

_CACHE_LOCK = threading.Lock()

_SEARCH_LOCK = threading.Lock()


def cache_key(params, limit):

    normalized = {

        str(key): str(value).strip().lower()

        for key, value in params.items()

        if key != "limit"

    }

    return json.dumps(
        [normalized, limit],
        sort_keys=True,
        ensure_ascii=False
    )


def refresh_result_statuses(result):

    """
    Actualise les statuts des profils même si
    les résultats proviennent du cache Tavily.
    """

    profiles = result.get("results", [])

    for profile in profiles:

        url = profile.get("url")

        if not url:
            continue

        register_profile(
            url,
            profile.get("title", "")
        )

        status = get_profile_status(url)

        profile.update(status)

    return result


# ============================================================
# RECHERCHE TAVILY
# ============================================================

def tavily_search(query, max_results):

    api_key = os.environ.get(
        "TAVILY_API_KEY", ""
    ).strip()

    if not api_key:

        raise RuntimeError(
            "La clé Tavily est absente. "
            "Ajoutez TAVILY_API_KEY dans "
            "Render > Environment."
        )

    payload = {

        "query": query,

        "search_depth": "basic",

        "topic": "general",

        "max_results": max(
            1,
            min(MAX_RESULTS, int(max_results))
        ),

        "include_answer": False,

        "include_raw_content": True,

        "include_domains": ["linkedin.com"],

        "include_domains_mode": "restrict",

        "country": "france"
    }

    request = Request(

        TAVILY_ENDPOINT,

        data=json.dumps(payload).encode("utf-8"),

        headers={

            "Authorization": f"Bearer {api_key}",

            "Content-Type": "application/json"

        },

        method="POST"
    )

    try:

        with urlopen(
            request,
            timeout=TAVILY_TIMEOUT
        ) as response:

            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

            data = json.loads(raw)

    except HTTPError as exc:

        details = ""

        try:

            details = exc.read().decode(
                "utf-8",
                errors="replace"
            )

        except Exception:
            pass

        if exc.code == 401:

            message = (
                "Clé Tavily invalide. "
                "Vérifiez TAVILY_API_KEY."
            )

        elif exc.code == 429:

            message = (
                "Tavily limite temporairement les requêtes. "
                "Attendez avant de réessayer."
            )

        elif exc.code in (432, 433):

            message = (
                "Le quota Tavily est atteint. "
                "Vérifiez votre tableau de bord Tavily."
            )

        elif exc.code == 403:

            message = (
                "Tavily a refusé la requête. "
                "Vérifiez votre compte et votre clé."
            )

        else:

            message = f"Erreur Tavily HTTP {exc.code}."

        logging.warning(
            "Tavily HTTP %s : %s",
            exc.code,
            details[:500]
        )

        raise RuntimeError(message) from exc

    except URLError as exc:

        raise RuntimeError(
            "Impossible de joindre l'API Tavily. "
            "Vérifiez la connexion du serveur."
        ) from exc

    except TimeoutError as exc:

        raise RuntimeError(
            "Tavily n'a pas répondu dans le délai imparti."
        ) from exc

    except json.JSONDecodeError as exc:

        raise RuntimeError(
            "Tavily a renvoyé une réponse illisible."
        ) from exc

    if not isinstance(data, dict):

        raise RuntimeError(
            "Réponse inattendue de l'API Tavily."
        )

    return data


# ============================================================
# RECHERCHE LINKEDIN ET FILTRAGE GÉOGRAPHIQUE
# ============================================================


def search_linkedin_index(params):
    try:
        limit = min(MAX_RESULTS, max(1, int(params.get("limit", 10))))
    except (ValueError, TypeError):
        limit = 10

    params = dict(params or {})
    for field in ("role", "company", "keywords", "location"):
        params[field] = str(params.get(field, "") or "").strip()

    if params.get("geo_scope") == "custom" and not params.get("location"):
        raise ValueError("Veuillez renseigner une localisation.")

    if params.get("geo_scope") == "department":
        code = str(params.get("department", "")).strip()
        if code not in DEPARTMENTS:
            raise ValueError("Département invalide.")

    # Les profils ouverts dans le navigateur sont envoyés au serveur :
    # ils sont exclus avant de constituer la liste des résultats.
    visited_urls = set()
    raw_visited = params.get("visited_urls", [])
    if isinstance(raw_visited, list):
        for value in raw_visited[:2000]:
            normalized_url = normalize_linkedin_profile(value)
            if normalized_url:
                visited_urls.add(normalized_url)
    params["visited_urls"] = sorted(visited_urls)

    MIN_MATCH_SCORE = 82
    key = cache_key(params, limit)

    # On ne réutilise pas le cache pour ne pas renvoyer un ancien lot
    # contenant des profils déjà visités ou ne plus chercher de nouveaux profils.
    with _SEARCH_LOCK:
        queries = build_queries(params)
        started = time.monotonic()
        candidates = {}
        seen_urls = set()
        calls_made = 0
        rejected_geo = 0
        rejected_relevance = 0
        rejected_visited = 0
        department_code, department = department_configuration(params)
        scope = str(params.get("geo_scope", "")).strip().lower()

        # On collecte un vivier plus large que le nombre demandé afin de
        # classer les meilleurs scores, sans laisser les profils visités
        # consommer la place des nouveaux profils.
        target_candidates = min(
            MAX_RESULTS * 3,
            max(limit * 3, limit + 10)
        )

        # Tant que le vivier de profils nouveaux et pertinents n'est pas
        # suffisant, on poursuit les variantes géographiques de la recherche.
        for query_index, query in enumerate(queries):
            if calls_made >= 18:
                break

            logging.info(
                "Recherche Tavily %s/%s | zone=%s | fonction=%s | entreprise=%s | score_min=%s",
                query_index + 1, len(queries),
                department_code or params.get("location") or scope,
                params.get("role") or "non précisée",
                params.get("company") or "non précisée",
                MIN_MATCH_SCORE
            )

            data = tavily_search(query, MAX_RESULTS)
            calls_made += 1
            records = data.get("results", [])
            if not isinstance(records, list):
                continue

            for item in records:
                if not isinstance(item, dict):
                    continue

                url = normalize_linkedin_profile(item.get("url"))
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Exclusion immédiate des profils visités côté navigateur.
                if url in visited_urls:
                    rejected_visited += 1
                    continue

                # Exclusion des profils visités/contactés déjà connus en base.
                profile_status = get_profile_status(url)
                if profile_status.get("visited") or profile_status.get("status") == "contacted":
                    rejected_visited += 1
                    continue

                if department_code:
                    geo_ok = profile_matches_department(item, department_code)
                elif scope in ("custom", "idf"):
                    geo_ok = profile_matches_location(item, params)
                else:
                    geo_ok = True

                if not geo_ok:
                    rejected_geo += 1
                    continue

                score, matched_fields = score_profile(item, params)
                if score < MIN_MATCH_SCORE:
                    rejected_relevance += 1
                    continue

                title = str(item.get("title") or "Profil LinkedIn public")[:200]
                register_profile(url, title)
                profile_status = get_profile_status(url)

                candidates[url] = {
                    "url": url,
                    "title": title,
                    "department": department["name"] if department else (
                        params.get("location") if scope == "custom" else
                        ("Île-de-France" if scope == "idf" else None)
                    ),
                    "department_code": department_code,
                    "location_verified": bool(geo_ok and (department_code or scope in ("custom", "idf"))),
                    "location_evidence": "Localisation repérée dans le contenu du résultat" if geo_ok else None,
                    "match_score": score,
                    "matched_fields": matched_fields,
                    "visited": False,
                    "status": profile_status["status"],
                    "first_seen": profile_status["first_seen"],
                    "visited_at": profile_status["visited_at"],
                    "contacted_at": profile_status["contacted_at"]
                }

            # Le seuil porte uniquement sur les profils neufs et pertinents.
            if len(candidates) >= target_candidates:
                break

        ranked = sorted(
            candidates.values(),
            key=lambda profile: (
                profile["match_score"],
                bool(profile.get("location_verified"))
            ),
            reverse=True
        )
        found = ranked[:limit]

        result = {
            "results": found,
            "count": len(found),
            "verified": False,
            "minimum_match_score": MIN_MATCH_SCORE,
            "geographic_filter": "strict" if department_code else "requested",
            "department": department["name"] if department else (
                params.get("location") if scope == "custom" else
                ("Île-de-France" if scope == "idf" else None)
            ),
            "department_code": department_code,
            "rejected_geographic": rejected_geo,
            "rejected_relevance": rejected_relevance,
            "rejected_visited": rejected_visited,
            "status": "ok" if found else "no_results",
            "cached": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "queries_used": calls_made,
            "search_backends": ["tavily"],
            "message": (
                f"{len(found)} nouveau(x) profil(s) retenu(s) avec un score minimum de "
                f"{MIN_MATCH_SCORE}/100. {rejected_visited} profil(s) déjà visité(s) ignoré(s)."
                if found else
                f"Aucun nouveau profil n'atteint le seuil de {MIN_MATCH_SCORE}/100. "
                f"{rejected_visited} profil(s) déjà visité(s), {rejected_relevance} résultat(s) "
                "insuffisamment pertinents et "
                f"{rejected_geo} résultat(s) hors zone ont été écartés."
            )
        }
        return result


# ============================================================
# SERVEUR HTTP
# ============================================================

class Handler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):

        super().__init__(
            *args,
            directory=str(ROOT),
            **kwargs
        )

    # --------------------------------------------------------
    # RÉPONSE JSON
    # --------------------------------------------------------

    def send_json(self, status, payload):

        data = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(data))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )

        self.end_headers()

        self.wfile.write(data)

    # --------------------------------------------------------
    # LECTURE DU CORPS JSON
    # --------------------------------------------------------

    def read_json_body(self):

        length = int(
            self.headers.get("Content-Length", "0")
        )

        if not 0 < length <= 262144:

            raise ValueError(
                "Paramètres de requête invalides."
            )

        try:

            data = json.loads(
                self.rfile.read(length)
            )

        except json.JSONDecodeError as exc:

            raise ValueError(
                "JSON invalide."
            ) from exc

        if not isinstance(data, dict):

            raise ValueError(
                "Les paramètres doivent être un objet JSON."
            )

        return data

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def do_GET(self):

        path = self.path.split("?", 1)[0]

        # Santé du serveur.
        if path == "/api/health":

            configured = bool(
                os.environ.get(
                    "TAVILY_API_KEY", ""
                ).strip()
            )

            try:

                stats = get_profile_statistics()

                database_status = "ok"

            except Exception:

                logging.exception(
                    "Erreur de lecture de la base SQLite"
                )

                stats = {}

                database_status = "error"

            self.send_json(

                200 if configured and database_status == "ok" else 503,

                {

                    "server": "ok",

                    "tavily": (
                        "configured"
                        if configured
                        else "not_configured"
                    ),

                    "search_backend": "tavily",

                    "database": database_status,

                    "database_path": str(DB_PATH),

                    "profile_statistics": stats,

                    "geographic_filter": "strict",

                    "available_departments": list(
                        DEPARTMENTS.keys()
                    ),

                    "cache_ttl_seconds": CACHE_TTL_SECONDS,

                    "available_projects": list(ROLES.keys()),

                    "roles_count": {
                        project: len(roles)
                        for project, roles in ROLES.items()
                    },

                    "url": f"http://127.0.0.1:{PORT}/"
                }
            )

            return

        # Consultation du statut d'un profil.
        if path == "/api/profile-status":

            params = parse_qs(
                urlsplit(self.path).query
            )

            url = params.get("url", [""])[0]

            normalized_url = normalize_linkedin_profile(url)

            if not normalized_url:

                self.send_json(
                    400,
                    {"error": "URL LinkedIn invalide."}
                )

                return

            self.send_json(
                200,
                {
                    "success": True,
                    "profile": get_profile_status(normalized_url)
                }
            )

            return

        # Liste des profils suivis.
        if path == "/api/profiles":

            params = parse_qs(
                urlsplit(self.path).query
            )

            status = params.get("status", [None])[0]

            try:

                profiles = list_profiles(status)

                self.send_json(
                    200,
                    {
                        "success": True,
                        "count": len(profiles),
                        "profiles": profiles,
                        "statistics": get_profile_statistics()
                    }
                )

            except ValueError as exc:

                self.send_json(
                    400,
                    {"error": str(exc)}
                )

            return

        return super().do_GET()

    # --------------------------------------------------------
    # POST
    # --------------------------------------------------------

    def do_POST(self):

        path = self.path.split("?", 1)[0]

        # Enregistrement d'une visite ou d'un contact.
        if path == "/api/profile-visited":

            try:

                data = self.read_json_body()

                action = str(
                    data.get("action", "")
                ).strip().lower()

                url = data.get("url", "")

                normalized_url = normalize_linkedin_profile(
                    url
                )

                if not normalized_url:

                    raise ValueError(
                        "URL LinkedIn invalide."
                    )

                if action == "visit":

                    profile = mark_profile_visited(
                        normalized_url
                    )

                elif action == "contact":

                    profile = mark_profile_contacted(
                        normalized_url
                    )

                else:

                    raise ValueError(
                        "Action inconnue. "
                        "Utilisez visit ou contact."
                    )

                logging.info(
                    "Suivi profil : %s | %s",
                    normalized_url,
                    profile["status"]
                )

                self.send_json(
                    200,
                    {
                        "success": True,
                        "message": (
                            "Profil enregistré avec succès."
                        ),
                        "profile": profile,
                        "statistics": get_profile_statistics()
                    }
                )

            except ValueError as exc:

                self.send_json(
                    400,
                    {"error": str(exc)}
                )

            except Exception:

                logging.exception(
                    "Erreur pendant le suivi du profil"
                )

                self.send_json(
                    500,
                    {
                        "error": (
                            "Impossible d'enregistrer le profil."
                        )
                    }
                )

            return

        # Recherche LinkedIn.
        if path != "/api/linkedin-discovery":

            self.send_json(
                404,
                {"error": "Route inconnue"}
            )

            return

        try:

            params = self.read_json_body()

            result = search_linkedin_index(params)

            self.send_json(
                200,
                result
            )

        except ValueError as exc:

            self.send_json(
                400,
                {"error": str(exc)}
            )

        except RuntimeError as exc:

            logging.warning(
                "Recherche Tavily indisponible : %s",
                exc
            )

            self.send_json(
                503,
                {
                    "error": str(exc),
                    "status": "search_unavailable"
                }
            )

        except Exception:

            logging.exception(
                "Erreur inattendue pendant la recherche Tavily"
            )

            self.send_json(
                503,
                {
                    "error": (
                        "Erreur inattendue pendant la recherche. "
                        "Consultez les logs Render."
                    ),
                    "status": "search_unavailable"
                }
            )


# ============================================================
# DÉMARRAGE DU SERVEUR
# ============================================================

def main():

    logging.basicConfig(

        level=logging.INFO,

        format=(
            "%(asctime)s - %(levelname)s - %(message)s"
        )
    )

    # Initialisation de la base de données.
    initialize_database()

    on_render = bool(
        os.environ.get("RENDER") or
        os.environ.get("PORT")
    )

    host = "0.0.0.0" if on_render else "127.0.0.1"

    server = ThreadingHTTPServer(
        (host, PORT),
        Handler
    )

    print(
        f"ProspectIA disponible sur http://{host}:{PORT}/",
        flush=True
    )

    print(
        "Projets disponibles :",
        ", ".join(ROLES.keys()),
        flush=True
    )

    for project, roles in ROLES.items():

        print(
            f"{project} : {len(roles)} rôles",
            flush=True
        )

    print(
        "Filtrage géographique strict activé.",
        flush=True
    )

    print(
        "Suivi des profils LinkedIn activé.",
        flush=True
    )

    print(
        f"Base de données : {DB_PATH}",
        flush=True
    )

    if not on_render:

        try:

            import webbrowser

            webbrowser.open(
                f"http://127.0.0.1:{PORT}/"
            )

        except Exception:
            pass

    try:

        server.serve_forever()

    except KeyboardInterrupt:

        print(
            "\nArrêt de ProspectIA."
        )

    finally:

        server.server_close()


if __name__ == "__main__":

    main()
