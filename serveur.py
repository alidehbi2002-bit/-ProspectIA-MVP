"""Serveur ProspectIA : recherche de profils LinkedIn publics via l'API Tavily.

La clé TAVILY_API_KEY doit être définie dans les variables d'environnement
(Render > Environment). Aucune clé n'est stockée dans ce fichier.
"""
import json
import logging
import os
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))
TAVILY_ENDPOINT = "https://api.tavily.com/search"
TAVILY_TIMEOUT = 25
CACHE_TTL_SECONDS = 1800  # 30 minutes

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

DEPARTMENTS = {
    "75": ("Paris", "Paris"),
    "77": ("Seine-et-Marne", "Seine et Marne"),
    "78": ("Yvelines", "Versailles"),
    "91": ("Essonne", "Évry-Courcouronnes"),
    "92": ("Hauts-de-Seine", "La Défense"),
    "93": ("Seine-Saint-Denis", "Saint-Denis"),
    "94": ("Val-de-Marne", "Créteil"),
    "95": ("Val-d'Oise", "Cergy"),
}


def normalize_linkedin_profile(url):
    """N'accepte que les URL de profils publics LinkedIn (/in/)."""
    try:
        parsed = urlsplit(str(url))
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            return None
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            return None
        if not parsed.path.startswith("/in/"):
            return None
        if not parsed.path.split("/in/", 1)[1].strip("/"):
            return None
        return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
    except Exception:
        return None


def geographic_terms(params):
    scope = str(params.get("geo_scope", "yvelines")).strip().lower()
    if scope == "idf":
        return ["Île-de-France", "Yvelines", "Paris", "Hauts-de-Seine"]
    if scope == "department":
        code = str(params.get("department", "78")).strip()
        name, city = DEPARTMENTS.get(code, DEPARTMENTS["78"])
        return [name, city, code]
    if scope == "custom":
        location = str(params.get("location", "")).strip()[:100]
        return [location] if location else ["Yvelines"]
    # Compatibilité avec le formulaire actuel qui envoie seulement location.
    location = str(params.get("location", "")).strip()[:100]
    if location:
        return [location, "France"]
    return ["Yvelines", "Versailles", "Saint-Germain-en-Laye"]


def _quoted_or(values):
    cleaned = []
    for value in values:
        value = str(value).strip()[:100]
        if value and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return f'"{cleaned[0]}"'
    return "(" + " OR ".join(f'"{v}"' for v in cleaned[:4]) + ")"


def build_queries(params):
    """Construit une requête principale et une variante plus large."""
    project = str(params.get("project", "fund")).strip()
    project = project if project in ROLES else "fund"

    role = str(params.get("role", "")).strip()[:100]
    roles = [role] if role else ROLES[project][:3]
    places = geographic_terms(params)

    company = str(params.get("company", "")).strip()[:100]
    keywords = str(params.get("keywords", "")).strip()[:120]

    role_clause = _quoted_or(roles)
    place_clause = _quoted_or(places[:4])

    parts = ["site:linkedin.com/in/", role_clause, place_clause]
    if company:
        parts.append(f'"{company}"')
    if keywords:
        parts.append(keywords)
    primary = " ".join(part for part in parts if part)
    primary = " ".join(primary.split())[:390]

    broad_parts = ["site:linkedin.com/in/", role_clause, _quoted_or(places[:2])]
    if company:
        broad_parts.append(f'"{company}"')
    if keywords:
        broad_parts.append(keywords)
    broad = " ".join(part for part in broad_parts if part)
    broad = " ".join(broad.split())[:390]

    return [primary, broad]


_SEARCH_CACHE = {}
_CACHE_LOCK = threading.Lock()
_SEARCH_LOCK = threading.Lock()


def _cache_key(params, limit):
    normalized = {
        str(key): str(value).strip().lower()
        for key, value in params.items()
        if key != "limit"
    }
    return json.dumps([normalized, limit], sort_keys=True, ensure_ascii=False)


def _tavily_search(query, max_results):
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "La clé Tavily est absente. Ajoutez TAVILY_API_KEY dans "
            "Render > Environment puis redéployez."
        )

    payload = {
        "query": query,
        "search_depth": "basic",
        "topic": "general",
        "max_results": max(1, min(20, int(max_results))),
        "include_answer": False,
        "include_raw_content": False,
        "include_domains": ["linkedin.com"],
        "include_domains_mode": "restrict",
        "country": "france",
    }
    request = Request(
        TAVILY_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=TAVILY_TIMEOUT) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except HTTPError as exc:
        details = ""
        try:
            details = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if exc.code == 401:
            message = "Clé Tavily invalide ou non reconnue (HTTP 401). Vérifiez TAVILY_API_KEY dans Render."
        elif exc.code == 429:
            message = "Tavily limite temporairement les requêtes (HTTP 429). Attendez avant de réessayer."
        elif exc.code in (432, 433):
            message = "Le quota ou la limite d'utilisation Tavily est atteint. Vérifiez votre tableau de bord Tavily."
        elif exc.code == 403:
            message = "Tavily a refusé la requête (HTTP 403). Vérifiez l'état de votre compte et de votre clé."
        else:
            message = f"Erreur Tavily HTTP {exc.code}."
        logging.warning("Tavily HTTP %s : %s", exc.code, details[:500])
        raise RuntimeError(message) from exc
    except URLError as exc:
        raise RuntimeError(
            "Impossible de joindre l'API Tavily. Vérifiez la connexion du service Render."
        ) from exc
    except TimeoutError as exc:
        raise RuntimeError("Tavily n'a pas répondu dans le délai imparti.") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Tavily a renvoyé une réponse illisible.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Réponse inattendue de l'API Tavily.")
    return data


def search_linkedin_index(params):
    try:
        limit = min(20, max(1, int(params.get("limit", 10))))
    except (ValueError, TypeError):
        limit = 10

    key = _cache_key(params, limit)
    now = time.monotonic()

    with _CACHE_LOCK:
        cached = _SEARCH_CACHE.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            result = dict(cached[1])
            result["cached"] = True
            return result
        if cached:
            _SEARCH_CACHE.pop(key, None)

    # Sérialise les recherches : plusieurs visiteurs ne déclenchent pas
    # simultanément des appels identiques vers le service externe.
    with _SEARCH_LOCK:
        now = time.monotonic()
        with _CACHE_LOCK:
            cached = _SEARCH_CACHE.get(key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                result = dict(cached[1])
                result["cached"] = True
                return result

        queries = build_queries(params)
        started = time.monotonic()
        found, seen = [], set()
        calls_made = 0

        # Deuxième requête uniquement si la première ne donne aucun profil
        # LinkedIn exploitable : au maximum 2 crédits par recherche utilisateur.
        for query_index, query in enumerate(queries):
            if query_index == 1 and found:
                break
            data = _tavily_search(query, min(limit + 5, 20))
            calls_made += 1
            records = data.get("results", [])
            if not isinstance(records, list):
                records = []

            for item in records:
                if not isinstance(item, dict):
                    continue
                url = normalize_linkedin_profile(item.get("url"))
                if not url or url in seen:
                    continue
                seen.add(url)
                found.append({
                    "url": url,
                    "title": str(item.get("title") or "Profil LinkedIn public")[:200],
                })
                if len(found) >= limit:
                    break
            if found:
                break

        result = {
            "results": found,
            "count": len(found),
            "verified": False,
            "status": "ok" if found else "no_results",
            "cached": False,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "queries_used": calls_made,
            "search_backends": ["tavily"],
            "message": (
                "Liens de profils publics trouvés dans l'index web. "
                "Les profils ne sont pas vérifiés par connexion à LinkedIn."
                if found else
                "Aucun profil LinkedIn public correspondant n'a été trouvé. "
                "Essayez une zone plus large ou un intitulé de poste différent."
            ),
        }

        with _CACHE_LOCK:
            if len(_SEARCH_CACHE) >= 100:
                oldest = min(_SEARCH_CACHE, key=lambda item: _SEARCH_CACHE[item][0])
                _SEARCH_CACHE.pop(oldest, None)
            _SEARCH_CACHE[key] = (time.monotonic(), result)

        return result


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
        if self.path.split("?", 1)[0] == "/api/health":
            configured = bool(os.environ.get("TAVILY_API_KEY", "").strip())
            self.send_json(200 if configured else 503, {
                "server": "ok",
                "tavily": "configured" if configured else "not_configured",
                "search_backend": "tavily",
                "cache_ttl_seconds": CACHE_TTL_SECONDS,
                "url": f"http://127.0.0.1:{PORT}/",
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
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            logging.warning("Recherche Tavily indisponible : %s", exc)
            self.send_json(503, {"error": str(exc), "status": "search_unavailable"})
        except Exception:
            logging.exception("Erreur inattendue pendant la recherche Tavily")
            self.send_json(503, {
                "error": "Erreur inattendue pendant la recherche. Consultez les logs Render.",
                "status": "search_unavailable",
            })


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
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
