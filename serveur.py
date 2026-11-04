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

    response = {
        "results": found,
        "count": len(found),
        "verified": False,
        "queries_used": len(queries),
        "search_backends": list(SEARCH_BACKENDS),
    }
    # Si chaque tentative moteur a échoué par exception, renvoyer une vraie erreur
    # plutôt qu'un succès HTTP 200 qui ressemble à une recherche sans résultat.
    if uses_ddgs and backend_calls and len(backend_errors) >= backend_calls:
        response["error"] = "Tous les moteurs de recherche ont échoué ou bloqué les requêtes. Réessayez plus tard."
        response["backend_errors"] = backend_errors[-10:]
        response["status"] = "search_unavailable"
    else:
        response["status"] = "ok" if found else "no_results"
    return response


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
                    "server": "ok", "ddgs": "installed",
                    "search_backends": list(SEARCH_BACKENDS),
                })
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
            result = search_linkedin_index(params)
            if result.get("status") == "search_unavailable":
                self.send_json(503, result)
            else:
                self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            logging.exception("Erreur de recherche")
            self.send_json(503, {"error": str(exc)})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
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
