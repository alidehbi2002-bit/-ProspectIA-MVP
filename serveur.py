"""Serveur local ProspectIA. Recherche d'URL publiques via moteur web, sans accès LinkedIn."""
import json
import logging
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
PORT = 8765

ROLES = {
    "fund": ["responsable mécénat", "responsable RSE", "responsable partenariats", "directeur fondation"],
    "corner": ["responsable ressources humaines", "responsable QVT", "responsable services généraux", "responsable événementiel"],
}


def normalize_linkedin_profile(url):
    """Accepte uniquement les URL de profils LinkedIn publics issues d'un index externe."""
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


def build_queries(project, location, company, role):
    project = project if project in ROLES else "fund"
    roles = [role.strip()] if role else ROLES[project][:3]
    loc = (location or "France").strip()
    # Recherche d'URL indexées sur le web ; ne charge jamais la page LinkedIn.
    return [f'site:linkedin.com/in/ "{r}" {company} {loc}'.strip() for r in roles]


def search_linkedin_index(params, search_func=None):
    """Recherche de liens dans les moteurs web ; aucune extraction sur linkedin.com."""
    project = str(params.get('project', 'fund'))[:20]
    location = str(params.get('location', 'France'))[:90]
    company = str(params.get('company', ''))[:90]
    role = str(params.get('role', ''))[:90]
    try: limit = min(20, max(1, int(params.get('limit', 10))))
    except (ValueError, TypeError): limit = 10
    if search_func is None:
        try:
            from ddgs import DDGS
        except ImportError as e:
            raise RuntimeError('Module ddgs non installé : lancez INSTALLER_ET_LANCER.bat ou « pip install ddgs ».') from e
        engine = DDGS(timeout=12)
        search_func = lambda q, n: engine.text(q, region='fr-fr', max_results=n, backend='auto')
    found, seen, errors = [], set(), []
    for q in build_queries(project, location, company, role):
        try:
            records = search_func(q, min(limit+4, 12)) or []
        except Exception as e:
            logging.warning('Moteur de recherche indisponible : %s', str(e))
            errors.append(str(e))
            continue
        for x in records:
            if not isinstance(x, dict): continue
            url = normalize_linkedin_profile(x.get('href') or x.get('url'))
            if not url or url in seen: continue
            seen.add(url)
            # Un titre d'index n'est pas une identité vérifiée.
            found.append({'url': url, 'title': str(x.get('title') or 'Profil LinkedIn public')[:180]})
            if len(found) >= limit: break
        if len(found) >= limit: break
    if not found and errors:
        detail = str(errors[0])[:260]
        raise RuntimeError('Échec du moteur de recherche Web (DDGS). Détail technique : ' + detail + '. Vérifiez votre connexion et réessayez une recherche plus simple. Les moteurs publics peuvent limiter les requêtes.')
    return {'results': found, 'count': len(found), 'verified': False, 'origin': 'index_web_public'}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/api/health':
            try:
                from ddgs import DDGS
                return self.send_json(200, {'server': 'ok', 'ddgs': 'installed', 'url': 'http://127.0.0.1:8765/'})
            except ImportError:
                return self.send_json(503, {'server': 'ok', 'ddgs': 'not installed', 'action': 'Lancez INSTALLER_ET_LANCER.bat'})
        return super().do_GET()

    def do_POST(self):
        if self.path != '/api/linkedin-discovery':
            return self.send_json(404, {'error': 'Route inconnue'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                return self.send_json(400, {'error': 'Données de recherche invalides'})
            params = json.loads(self.rfile.read(size))
            if not isinstance(params, dict):
                raise ValueError('Paramètres invalides')
            self.send_json(200, search_linkedin_index(params))
        except (ValueError, json.JSONDecodeError) as e:
            self.send_json(400, {'error': str(e)})
        except Exception as e:
            self.send_json(503, {'error': str(e)})


if __name__ == '__main__':
    import webbrowser
    logging.basicConfig(level=logging.INFO)
    httpd = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'ProspectIA disponible sur http://127.0.0.1:{PORT}/', flush=True)
    webbrowser.open(f'http://127.0.0.1:{PORT}/')
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally: httpd.server_close()
