"""Serveur local ProspectIA : recherche de profils LinkedIn publics indexés."""
import json
import logging
import os
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8765"))

ROLES = {
    "fund": ["responsable mécénat", "responsable RSE", "responsable partenariats", "directeur fondation"],
    "corner": ["responsable ressources humaines", "responsable QVT", "responsable services généraux", "responsable événementiel"],
    "custom": ["responsable partenariats", "directeur", "responsable communication"],
}


def normalize_linkedin_profile(url):
    try:
        u = urlsplit(str(url))
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            return None
        if not u.path.startswith("/in/") or not u.path.split("/in/", 1)[1].strip("/"):
            return None
        return urlunsplit(("https", host, u.path.rstrip("/"), "", ""))
    except Exception:
        return None


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


def geographic_terms(params):
    scope = str(params.get("geo_scope", "yvelines")).strip().lower()
    if scope == "idf":
        return ["Île-de-France", "Yvelines", "Paris", "Hauts-de-Seine"]
    if scope == "department":
        code = str(params.get("department", "78"))
        name, city = DEPARTMENTS.get(code, DEPARTMENTS["78"])
        return [name, city, code]
    if scope == "custom":
        location = str(params.get("location", "")).strip()[:100]
        return [location] if location else ["Yvelines"]
    return ["Yvelines", "78", "Versailles", "Saint-Germain-en-Laye"]


def build_queries(params):
    project = str(params.get("project", "fund"))
    project = project if project in ROLES else "fund"
    role = str(params.get("role", "")).strip()[:100]
    roles = [role] if role else ROLES[project][:3]
    company = str(params.get("company", "")).strip()[:100]
    keywords = str(params.get("keywords", "")).strip()[:120]
    places = geographic_terms(params)
    queries = []
    # Interroger les termes géographiques séparément améliore la couverture
    # des profils dont l'entreprise mentionne une ville ou un département.
    for r in roles:
        for place in places[:4]:
            q = f'site:linkedin.com/in/ "{r}" "{place}" {company} {keywords}'
            queries.append(" ".join(q.split()))
    return queries


def search_linkedin_index(params, search_func=None):
    try:
        limit = min(20, max(1, int(params.get("limit", 10))))
    except (ValueError, TypeError):
        limit = 10

    if search_func is None:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError("Le module ddgs n'est pas installé. Lancez INSTALLER_ET_LANCER.bat.") from exc
        engine = DDGS(timeout=20)
        # Ne pas dépendre d'un seul fournisseur : certains bloquent ou
        # deviennent temporairement injoignables selon le réseau.
        # DDGS essaie les moteurs dans l'ordre et passe au suivant en cas d'échec.
        search_func = lambda query, count: engine.text(
            query, region="fr-fr", max_results=count,
            backend="google,duckduckgo,bing,mojeek,yahoo"
        )

    found, seen, errors = [], set(), []
    for query in build_queries(params):
        try:
            records = search_func(query, min(limit + 5, 15)) or []
        except Exception as exc:
            logging.warning("Recherche DDGS échouée : %s", exc)
            errors.append(str(exc))
            continue

        for item in records:
            if not isinstance(item, dict):
                continue
            url = normalize_linkedin_profile(item.get("href") or item.get("url"))
            if not url or url in seen:
                continue
            seen.add(url)
            found.append({
                "url": url,
                "title": str(item.get("title") or "Profil LinkedIn public")[:200],
            })
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break

    if not found and errors:
        raise RuntimeError(
            "Aucun moteur DDGS n'a répondu (Google, DuckDuckGo, Bing, Mojeek, Yahoo). "
            + "Dernière erreur : " + errors[-1][:220]
            + ". Vérifiez l'accès Internet, le pare-feu ou le proxy, puis réessayez."
        )
    return {"results": found, "count": len(found), "verified": False}


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
                self.send_json(200, {"server": "ok", "ddgs": "installed"})
            except ImportError:
                self.send_json(503, {"server": "ok", "ddgs": "not installed"})
            return
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/linkedin-discovery":
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
            self.send_json(200, search_linkedin_index(params))
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            logging.exception("Erreur de recherche")
            self.send_json(503, {"error": str(exc)})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Render exige une écoute sur toutes les interfaces réseau. En local,
    # on conserve une écoute limitée à la machine.
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
        print("\\nArrêt de ProspectIA.")
    finally:
        server.server_close()
