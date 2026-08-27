#!/usr/bin/env python3
"""Faux SonarQube central + faux Artifactory, pour le banc d'essai du worker.

Usage : mock_central.py <port> <repertoire_de_travail>
Aucune instance reelle n'est sollicitee.
"""
import json, os, sys, hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

BASE = sys.argv[2] if len(sys.argv) > 2 else "/tmp/ilab"
INBOX = f"{BASE}/art/sonar-projects-to-migrate"
DONE  = f"{BASE}/art/sonar-projects-migrated"
S = json.load(open(f"{BASE}/state.json"))          # état mutable de l'instance

def save():
    json.dump(S, open(f"{BASE}/state.json", "w"), indent=2)

def log(ev):
    with open(f"{BASE}/actions.log", "a") as f:
        f.write(ev + "\n")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _j(self, o, c=200):
        b = json.dumps(o).encode(); self.send_response(c)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def _t(self, s, c=200):
        b = s.encode(); self.send_response(c)
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)

        if p == "/api/server/version": return self._t(S["version"])
        if p == "/api/plugins/installed":
            return self._j({"plugins": S["plugins"]})

        if p == "/api/components/show":
            k = q["component"][0]
            return self._j({"component":{"key":k}}) if k in S["projects"] \
                   else self._j({"errors":[{"msg":"not found"}]}, 404)

        if p == "/api/project_analyses/search":
            k = q["project"][0]
            n = S["projects"].get(k, {}).get("analyses", 0)
            return self._j({"paging":{"total":n},"analyses":[]})

        if p == "/api/ce/task":
            tid = q["id"][0]
            return self._j({"task":{"id":tid,"status":S["ce"].get(tid,"SUCCESS")}})

        if p == "/api/permissions/users":
            key = q.get("projectKey",[None])[0]
            src = S["perm_global"] if key is None else S["perm_project"].get(key,[])
            return self._j({"users":[{"login":l} for l in src]})

        if p == "/api/permissions/groups":
            key = q.get("projectKey",[None])[0]
            return self._j({"groups":[{"name":g} for g in S["perm_groups"].get(key,[])]})

        if p == "/api/users/groups":
            l = q["login"][0]
            return self._j({"groups":[{"name":g} for g in S["user_groups"].get(l,[])]})

        if p == "/api/v2/users-management/users":
            ident = q.get("externalIdentity",[None])[0]
            us = [u for u in S["users"] if ident is None or u.get("externalLogin")==ident]
            return self._j({"users":us,"page":{"total":len(us)}})

        if p == "/api/users/search":
            t = q.get("q",[""])[0]
            us = [u for u in S["users"] if t in (u.get("email") or "") or t in u["login"]]
            return self._j({"users":[{**u,"externalIdentity":u.get("externalLogin")} for u in us]})

        if p == "/api/project_links/search":
            k = q["projectKey"][0]
            return self._j({"links": S["links"].get(k, [])})

        if p == "/artifactory/api/system/ping": return self._t("OK")

        if p.startswith("/artifactory/"):
            f = os.path.join(BASE, "art", p[len("/artifactory/"):])
            if os.path.isfile(f):
                b = open(f,"rb").read(); self.send_response(200)
                self.send_header("Content-Length",str(len(b))); self.end_headers()
                return self.wfile.write(b)
            return self._j({"errors":"no such file"}, 404)

        return self._j({"errors":[{"msg":"GET "+p}]}, 404)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        n = int(self.headers.get("Content-Length",0)); body = self.rfile.read(n)

        if p == "/api/project_dump/import":
            k = q["key"][0]
            log(f"IMPORT {k}")
            S["projects"][k] = {"analyses": 5}      # le projet importé a un historique
            save()
            return self._j({"taskId": "CE-IMPORT-1"})

        if p == "/api/projects/delete":
            k = q["project"][0]
            log(f"DELETE {k}")
            S["projects"].pop(k, None); save()
            return self._j({})

        if p == "/api/projects/update_key":
            src = q.get("from",[q.get("project",[None])[0]])[0]
            dst = q.get("to",[q.get("newKey",[None])[0]])[0]
            if src not in S["projects"]:
                return self._j({"errors":[{"msg":"unknown"}]}, 404)
            if dst in S["projects"]:
                return self._j({"errors":[{"msg":"key taken"}]}, 400)
            log(f"RENAME {src} -> {dst}")
            S["projects"][dst] = S["projects"].pop(src); save()
            return self._j({})

        for path, ev in (("/api/permissions/apply_template","TEMPLATE"),
                         ("/api/qualitygates/select","GATE"),
                         ("/api/alm_settings/set_gitlab_binding","BINDING"),
                         ("/api/project_links/delete","UNLINK")):
            if p == path:
                log(f"{ev} {u.query}")
                return self._j({})

        if p == "/artifactory/api/search/aql":
            res = []
            for root, _, files in os.walk(INBOX):
                for f in files:
                    if f.endswith(".manifest.json"):
                        rel = os.path.relpath(root, INBOX)
                        res.append({"path": rel, "name": f})
            return self._j({"results": res})

        if p.startswith("/artifactory/api/move/"):
            rest = p[len("/artifactory/api/move/"):]
            to = q["to"][0].lstrip("/")
            src = os.path.join(BASE,"art",rest); dst = os.path.join(BASE,"art",to)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isfile(src):
                os.replace(src,dst); log(f"MOVE {rest} -> {to}")
            return self._j({})

        return self._j({"errors":[{"msg":"POST "+p}]}, 404)

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
