"""Serveur ProspectIA : recherche limitée de profils LinkedIn publics indexés.

Optimisations :
- 2 requêtes de recherche construites au maximum.
- 3 appels DDGS maximum par recherche utilisateur.
- Un seul moteur interrogé par appel (pas de backend='auto').
- Cache mémoire de 30 minutes.
- Délai minimal entre les appels sortants.
- Coupe-circuit temporaire pour les moteurs en erreur / limitation.
- Les résultats ne sont pas présentés comme vérifiés ou contactables.
"""
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))

ROLES = {
    "fund": [
        "responsable mécénat",
        "responsable RSE",
        "responsable partenariats",
        "directeur fondation",
    ],
    "corner": [
        "responsable ressources humaines",
        "responsable QVT",
        "responsable services généraux",
        "responsable événementiel",
    ],
    "custom": [
        "responsable partenariats",
        "directeur",
        "responsable communication",
    ],
}

# Moteurs autorisés. Startpage est volontairement retiré (CAPTCHA constaté).
# Un moteur est appelé à la fois afin de contrôler le nombre d'appels.
SEARCH_BACKENDS = ("yahoo", "duckduckgo")

MAX_SEARCH_CALLS = 3
CACHE_TTL_SECONDS = 30 * 60
CACHE_MAX_ENTRIES = 128
MIN_SECONDS_BETWEEN_CALLS = 5
BACKEND_COOLDOWN_SECONDS = 15 * 60
RATE_LIMIT_COOLDOWN_SECONDS = 30 * 60
MAX_QUERY_RESULTS = 15

_cache = OrderedDict()
_cache_lock = threading.Lock()
_search_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_outbound_call = 0.0
_backend_blocked_until = {}


def normalize_linkedin_profile(url):
    """Ne conserve que les URL HTTPS de profils publics /in/ de LinkedIn."""
    try:
        u = urlsplit(str(url or "").strip())
        host = (u.hostname or "").lower()
        if u.scheme.lower() != "https":
            return None
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            return None
        if not u.path.startswith("/in/"):
            return None
        profile_id = u.path.split("/in/", 1)[1].strip("/")
        if not profile_id:
            return None
        return urlunsplit(("https", host, u.path.rstrip("/"), "", ""))
    except Exception:
        return None


DEPARTMENTS = {
    "75": ("Paris", "Paris"),
    "77": ("Seine-et-Marne", "Melun"),
    "78": ("Yvelines", "Versailles"),
    "91": ("Essonne", "Évry-Courcouronnes"),
    "92": ("Hauts-de-Seine", "La Défense"),
    "93": ("Seine-Saint-Denis", "Saint-Denis"),
    "94": ("Val-de-Marne", "Créteil"),
    "95": ("Val-d'Oise", "Cergy"),
}


def _clean(value, max_length=100):
    return " ".join(str(value or "").split())[:max_length]


def geographic_terms(params):
    scope = _clean(params.get("geo_scope", "yvelines"), 20).lower()
    if scope == "idf":
        return ["Île-de-France", "Yvelines", "Paris"]
    if scope == "department":
        code = _clean(params.get("department", "78"), 2)
        name, city = DEPARTMENTS.get(code, DEPARTMENTS["78"])
        return [name, city, code]
    if scope == "custom":
        location = _clean(params.get("location", ""), 100)
        return [location] if location else ["Yvelines", "Versailles"]
    return ["Yvelines", "Versailles", "Saint-Germain-en-Laye"]


def build_queries(params):
    """Construit deux requêtes maximum au lieu du produit métier × ville."""
    project = _clean(params.get("project", "fund"), 20)
    if project not in ROLES:
        project = "fund"

    role = _clean(params.get("role", ""), 100)
    if role:
        roles = [role]
    else:
        roles = ROLES[project][:3]

    company = _clean(params.get("company", ""), 100)
    keywords = _clean(params.get("keywords", ""), 120)
    places = geographic_terms(params)

    # OR regroupe les métiers et lieux dans une seule recherche.
    role_clause = " OR ".join(f'"{r}"' for r in roles)
    place_clause = " OR ".join(f'"{p}"' for p in places[:3])

    terms = ['site:linkedin.com/in/', f"({role_clause})", f"({place_clause})"]
    if company:
        terms.append(f'"{company}"')
    if keywords:
        terms.append(keywords)
    primary = " ".join(terms)

    # Variante plus large : on garde le métier et la zone générale,
    # mais on retire les lieux secondaires pour augmenter les chances.
    broad_places = places[:1]
    broad_place_clause = " OR ".join(f'"{p}"' for p in broad_places)
    broad_terms = [
        "site:linkedin.com/in/",
        f"({role_clause})",
        f"({broad_place_clause})",
    ]
    if company:
        broad_terms.append(f'"{company}"')
    if keywords:
        broad_terms.append(keywords)
    broad = " ".join(broad_terms)

    queries = [primary]
    if broad != primary:
        queries.append(broad)
    return queries[:2]


def _cache_key(params):
    safe_params = {
        "project": _clean(params.get("project", "fund"), 20).lower(),
        "role": _clean(params.get("role", ""), 100).lower(),
        "company": _clean(params.get("company", ""), 100).lower(),
        "keywords": _clean(params.get("keywords", ""), 120).lower(),
        "geo_scope": _clean(params.get("geo_scope", "yvelines"), 20).lower(),
        "department": _clean(params.get("department", "78"), 2),
        "location": _clean(params.get("location", ""), 100).lower(),
        "limit": min(20, max(1, _to_int(params.get("limit", 10), 10))),
    }
    raw = json.dumps(safe_params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _to_int(value, default):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _cache_get(key):
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        expires_at, payload = entry
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        result = dict(payload)
        result["cached"] = True
        return result


def _cache_put(key, payload):
    with _cache_lock:
        _cache[key] = (time.time() + CACHE_TTL_SECONDS, dict(payload))
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def _wait_for_global_interval():
    """Espace les appels sortants, même si plusieurs utilisateurs recherchent."""
    global _last_outbound_call
    with _rate_lock:
        now = time.monotonic()
        wait = MIN_SECONDS_BETWEEN_CALLS - (now - _last_outbound_call)
        if wait > 0:
            time.sleep(wait)
        _last_outbound_call = time.monotonic()


def _backend_is_blocked(backend):
    return _backend_blocked_until.get(backend, 0) > time.time()


def _mark_backend_failure(backend, exc):
    message = str(exc).lower()
    cooldown = RATE_LIMIT_COOLDOWN_SECONDS if any(
        marker in message
        for marker in ("429", "rate", "ratelimit", "captcha", "blocked", "forbidden")
    ) else BACKEND_COOLDOWN_SECONDS
    _backend_blocked_until[backend] = time.time() + cooldown


def _valid_profiles(records):
    valid = []
    for item in records or []:
        if not isinstance(item, dict):
            continue
        url = normalize_linkedin_profile(item.get("href") or item.get("url"))
        if not url:
            continue
        valid.append({
            "url": url,
            "title": str(item.get("title") or "Profil LinkedIn public")[:200],
        })
    return valid


def search_linkedin_index(params, search_func=None):
    """Recherche avec cache, 2 requêtes maximum et 3 appels moteur maximum."""
    limit = min(20, max(1, _to_int(params.get("limit", 10), 10)))
    key = _cache_key(params)

    cached = _cache_get(key)
    if cached is not None:
        logging.info("Recherche servie depuis le cache")
        return cached

    queries = build_queries(params)
    if not queries:
        return {
            "results": [], "count": 0, "verified": False,
            "status": "no_query", "queries_used": 0, "calls_made": 0,
            "search_backends": list(SEARCH_BACKENDS), "cached": False,
        }

    # Sérialise les recherches pour éviter plusieurs rafales simultanées.
    if not _search_lock.acquire(blocking=False):
        return {
            "results": [], "count": 0, "verified": False,
            "status": "search_busy",
            "error": "Une autre recherche est déjà en cours. Réessayez dans quelques instants.",
            "queries_used": 0, "calls_made": 0,
            "search_backends": list(SEARCH_BACKENDS), "cached": False,
        }

    try:
        cached = _cache_get(key)
        if cached is not None:
            return cached

        if search_func is None:
            try:
                from ddgs import DDGS
            except ImportError as exc:
                raise RuntimeError(
                    "Le module ddgs n'est pas installé. Ajoutez ddgs à requirements.txt."
                ) from exc
            engine = DDGS(timeout=8)

            def run_search(query, count, backend):
                _wait_for_global_interval()
                return engine.text(
                    query,
                    region="fr-fr",
                    safesearch="moderate",
                    max_results=count,
                    page=1,
                    backend=backend,
                ) or []
        else:
            def run_search(query, count, backend):
                # Fonction injectée pour les tests : pas d'appel réseau réel.
                return search_func(query, count) or []

        found = []
        seen = set()
        backend_errors = []
        calls_made = 0
        queries_used = 0
        backends_used = []
        successful_backend = None

        def collect(records):
            for item in _valid_profiles(records):
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                found.append(item)
                if len(found) >= limit:
                    break

        # Première requête : moteur principal disponible.
        first_backend = next(
            (b for b in SEARCH_BACKENDS if not _backend_is_blocked(b)),
            None,
        )

        if first_backend is None:
            result = {
                "results": [], "count": 0, "verified": False,
                "status": "search_unavailable",
                "error": "Les moteurs sont temporairement en pause après des erreurs ou limitations. Réessayez plus tard.",
                "backend_errors": [],
                "queries_used": 0, "calls_made": 0,
                "search_backends": list(SEARCH_BACKENDS), "cached": False,
            }
            return result

        # Tentative 1 : requête précise avec le premier moteur.
        try:
            calls_made += 1
            queries_used = 1
            backends_used.append(first_backend)
            records = run_search(queries[0], min(limit + 5, MAX_QUERY_RESULTS), first_backend)
            collect(records)
            if records is not None:
                successful_backend = first_backend
            logging.info(
                "Recherche backend=%s : %s résultat(s) LinkedIn retenu(s)",
                first_backend, len(found)
            )
        except Exception as exc:
            backend_errors.append({"backend": first_backend, "error": str(exc)[:250]})
            _mark_backend_failure(first_backend, exc)
            logging.warning("DDGS backend=%s échoué : %s", first_backend, exc)

        # Tentative 2 : secours uniquement si la première n'a pas donné
        # suffisamment de profils (ou a échoué).
        if len(found) < limit and calls_made < MAX_SEARCH_CALLS:
            backup_backend = next(
                (b for b in SEARCH_BACKENDS
                 if b != first_backend and not _backend_is_blocked(b)),
                None,
            )
            if backup_backend:
                try:
                    calls_made += 1
                    backends_used.append(backup_backend)
                    records = run_search(queries[0], min(limit + 5, MAX_QUERY_RESULTS), backup_backend)
                    collect(records)
                    if records:
                        successful_backend = backup_backend
                    logging.info(
                        "Recherche secours backend=%s : %s résultat(s) cumulés",
                        backup_backend, len(found)
                    )
                except Exception as exc:
                    backend_errors.append({"backend": backup_backend, "error": str(exc)[:250]})
                    _mark_backend_failure(backup_backend, exc)
                    logging.warning("DDGS backend=%s échoué : %s", backup_backend, exc)

        # Tentative 3 : une seule requête élargie, sur le moteur qui a
        # fourni des résultats. Pas de troisième moteur ni de boucle.
        if (
            len(found) < limit
            and calls_made < MAX_SEARCH_CALLS
            and len(queries) > 1
            and successful_backend
            and not _backend_is_blocked(successful_backend)
        ):
            try:
                calls_made += 1
                queries_used = 2
                backends_used.append(successful_backend)
                records = run_search(
                    queries[1],
                    min(limit + 5, MAX_QUERY_RESULTS),
                    successful_backend,
                )
                collect(records)
            except Exception as exc:
                backend_errors.append({
                    "backend": successful_backend,
                    "error": str(exc)[:250],
                })
                _mark_backend_failure(successful_backend, exc)
                logging.warning(
                    "Recherche élargie backend=%s échouée : %s",
                    successful_backend, exc
                )

        all_attempts_failed = calls_made > 0 and len(backend_errors) >= calls_made
        response = {
            "results": found,
            "count": len(found),
            "verified": False,
            "queries_used": queries_used,
            "calls_made": calls_made,
            "search_backends": list(dict.fromkeys(backends_used)),
            "cached": False,
        }

        if all_attempts_failed:
            response.update({
                "status": "search_unavailable",
                "error": "Les moteurs de recherche ont échoué ou limité les requêtes. Réessayez plus tard.",
                "backend_errors": backend_errors[-5:],
            })
        else:
            response["status"] = "ok" if found else "no_results"
            if backend_errors:
                response["backend_errors"] = backend_errors[-5:]

        # Mettre en cache les résultats utiles et les recherches sans résultat
        # pour éviter de répéter immédiatement une recherche identique.
        if response["status"] in ("ok", "no_results"):
            _cache_put(key, response)
        return response

    finally:
        _search_lock.release()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/health":
            try:
                import ddgs  # noqa: F401
                self.send_json(200, {
                    "server": "ok",
                    "ddgs": "installed",
                    "search_backends": list(SEARCH_BACKENDS),
                    "max_calls_per_search": MAX_SEARCH_CALLS,
                    "cache_ttl_seconds": CACHE_TTL_SECONDS,
                    "minimum_seconds_between_calls": MIN_SECONDS_BETWEEN_CALLS,
                })
            except ImportError:
                self.send_json(503, {
                    "server": "ok",
                    "ddgs": "not installed",
                    "error": "Ajoutez ddgs à requirements.txt puis redéployez.",
                })
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/linkedin-discovery":
            self.send_json(404, {"error": "Route inconnue"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                self.send_json(400, {"error": "Paramètres de recherche invalides"})
                return
            params = json.loads(self.rfile.read(length))
            if not isinstance(params, dict):
                raise ValueError("Les paramètres doivent être un objet JSON.")
            result = search_linkedin_index(params)
            if result.get("status") == "search_unavailable":
                self.send_json(503, result)
            elif result.get("status") == "search_busy":
                self.send_json(429, result)
            else:
                self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            logging.exception("Erreur de recherche")
            self.send_json(503, {"error": str(exc)})


if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    on_render = bool(os.environ.get("RENDER") or os.environ.get("PORT"))
    host = "0.0.0.0" if on_render else "127.0.0.1"
    server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"ProspectIA disponible sur http://{host}:{PORT}/", flush=True)
    if not on_render:
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt de ProspectIA.")
    finally:
        server.server_close()
