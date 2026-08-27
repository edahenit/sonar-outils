#!/usr/bin/env python3
"""Faux SonarQube + faux Artifactory, pour tester le script de publication."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

UP = "/tmp/lab/artifactory"          # ce que "Artifactory" reçoit
os.makedirs(UP, exist_ok=True)

SCEN = json.load(open("/tmp/lab/scenario.json"))

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _text(self, s, code=200):
        b = s.encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)

        if p == "/api/server/version":
            return self._text("2026.2.1")

        if p == "/api/plugins/installed":
            return self._json({"plugins": [
                {"key": "java", "version": "8.0.1"},
                {"key": "python", "version": "4.2.0"}]})

        if p == "/api/ce/activity":
            return self._json({"tasks": SCEN["tasks"],
                               "paging": {"pageIndex": 1, "pageSize": 100,
                                          "total": len(SCEN["tasks"])}})

        if p == "/api/project_links/search":
            key = q.get("projectKey", [""])[0]
            return self._json({"links": SCEN["links"].get(key, [])})

        if p == "/api/v2/users-management/users":
            if not SCEN.get("v2_enabled", True):
                return self._json({"message": "not found"}, 404)
            term = q.get("q", [""])[0]
            return self._json({"users": [u for u in SCEN["users"]
                                         if term in u.get("login", "")],
                               "page": {"pageIndex": 1, "pageSize": 50,
                                        "total": len(SCEN["users"])}})

        if p == "/api/users/search":
            term = q.get("q", [""])[0]
            return self._json({"users": [
                {"login": u["login"], "name": u.get("name"), "email": u.get("email"),
                 "externalIdentity": u.get("externalLogin"),
                 "externalProvider": u.get("externalProvider"),
                 "local": u.get("local", False), "active": u.get("active", True)}
                for u in SCEN["users"] if term in u.get("login", "")]})

        if p == "/artifactory/api/system/ping":
            return self._text("OK")

        return self._json({"errors": [{"msg": "not found: " + p}]}, 404)

    def do_PUT(self):
        p = urlparse(self.path).path
        if not p.startswith("/artifactory/"):
            return self._json({"msg": "nope"}, 400)
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        dest = os.path.join(UP, p[len("/artifactory/"):])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        # journal d'ordre de dépôt : c'est ce qui prouve que le manifeste part en dernier
        with open("/tmp/lab/upload_order.log", "a") as f:
            f.write(p + "\n")
        return self._json({"uri": self.path}, 201)

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
