#!/usr/bin/env python3
"""
sonar-export-publisher
======================

Publie vers Artifactory les exports de projets SonarQube destinés à la migration
vers l'instance centrale.

Principe
--------
Le script ne scanne PAS le répertoire d'export : il part de la liste des tâches
Compute Engine de type PROJECT_EXPORT. Une tâche porte l'identité de la personne
qui a lancé l'export, ce qu'un fichier ne fait pas.

Pour qu'un export soit considéré comme une demande de migration, le projet source
doit porter un lien nommé (par défaut « MIGRATION ») pointant vers le dashboard du
projet cible sur l'instance centrale. Poser ce lien exige le droit Administer sur
le projet : c'est le premier contrôle d'habilitation, et il est gratuit.

L'archive est publiée d'abord, le manifeste ensuite. Le script central ne réagit
qu'au manifeste : il ne peut donc jamais traiter une archive incomplète.

Usage
-----
    sonar_export_publisher.py --config /etc/sonar-export-publisher/config.yml
    sonar_export_publisher.py --config ... --dry-run
    sonar_export_publisher.py --config ... --task AY8xxxx     (rejouer une tâche)

Codes de sortie
---------------
    0  cycle terminé (avec ou sans publication)
    1  erreur de configuration
    2  instance SonarQube injoignable
    3  Artifactory injoignable
    4  verrou déjà pris — un cycle tourne encore
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

LOG = logging.getLogger("publisher")

# Type de tâche Compute Engine pour l'export de projet.
# À VÉRIFIER sur votre version : GET /api/ce/activity et regarder le champ "type".
CE_TASK_TYPE = "PROJECT_EXPORT"

SCHEMA_VERSION = "1.0"


# --------------------------------------------------------------------------- #
#  Configuration                                                              #
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        sq = raw["sonarqube"]
        self.sq_url: str = sq["url"].rstrip("/")
        self.sq_token: str = os.environ.get("SONAR_TOKEN") or sq["token"]
        self.sq_edition: str = sq.get("edition", "enterprise")
        self.export_dir = Path(sq["export_dir"])

        tgt = raw["target"]
        self.target_host: str = tgt["host"].lower()
        self.key_pattern = re.compile(tgt["key_pattern"])

        self.link_name: str = raw.get("link", {}).get("name", "MIGRATION")

        art = raw["artifactory"]
        self.art_url: str = art["base_url"].rstrip("/")
        self.art_repo: str = art["repository"]
        self.art_token: str = os.environ.get("ARTIFACTORY_TOKEN") or art["token"]

        rt = raw.get("runtime", {})
        self.state_file = Path(rt.get("state_file",
                                       "/var/lib/sonar-export-publisher/state.json"))
        self.quarantine_dir = Path(rt.get("quarantine_dir",
                                          "/var/lib/sonar-export-publisher/quarantine"))
        self.lock_file = Path(rt.get("lock_file",
                                     "/var/lock/sonar-export-publisher.lock"))
        self.lookback_hours: int = int(rt.get("lookback_hours", 24))
        self.max_attempts: int = int(rt.get("max_attempts", 6))
        self.stability_seconds: int = int(rt.get("stability_check_seconds", 5))
        self.http_timeout: int = int(rt.get("http_timeout_seconds", 30))
        self.log_level: str = rt.get("log_level", "INFO")

        if not self.sq_token:
            raise ValueError("token SonarQube absent (config ou SONAR_TOKEN)")
        if not self.art_token:
            raise ValueError("token Artifactory absent (config ou ARTIFACTORY_TOKEN)")


# --------------------------------------------------------------------------- #
#  État local — idempotence                                                    #
# --------------------------------------------------------------------------- #

class State:
    """
    Mémorise les tâches déjà traitées, pour ne jamais publier deux fois.
    Format : { "<task_id>": {"status": "...", "at": "...", "attempts": N} }
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                LOG.warning("état illisible, on repart de zéro : %s", self.path)

    def is_done(self, task_id: str) -> bool:
        return self.data.get(task_id, {}).get("status") in ("published", "ignored",
                                                            "quarantined")

    def attempts(self, task_id: str) -> int:
        return self.data.get(task_id, {}).get("attempts", 0)

    def mark(self, task_id: str, status: str, detail: str = "") -> None:
        entry = self.data.setdefault(task_id, {"attempts": 0})
        entry["status"] = status
        entry["detail"] = detail
        entry["at"] = datetime.now(timezone.utc).isoformat()
        if status == "retry":
            entry["attempts"] = entry.get("attempts", 0) + 1
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)          # écriture atomique


# --------------------------------------------------------------------------- #
#  Client SonarQube                                                            #
# --------------------------------------------------------------------------- #

class Sonar:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        # SonarQube accepte le token en basic-auth, mot de passe vide
        self.s.auth = (cfg.sq_token, "")
        self.timeout = cfg.http_timeout

    def get(self, path: str, **params):
        r = self.s.get(f"{self.cfg.sq_url}{path}", params=params or None,
                       timeout=self.timeout)
        r.raise_for_status()
        return r

    def json(self, path: str, **params) -> dict:
        return self.get(path, **params).json()

    # -- faits techniques de l'instance ------------------------------------ #

    def version(self) -> str:
        return self.get("/api/server/version").text.strip()

    def plugins_fingerprint(self) -> str:
        """
        Empreinte stable de la liste des plugins installés.
        Le script central compare cette empreinte avant d'importer.
        """
        data = self.json("/api/plugins/installed")
        items = sorted(f"{p['key']}:{p.get('version', '')}"
                       for p in data.get("plugins", []))
        return "sha256:" + hashlib.sha256("|".join(items).encode()).hexdigest()

    # -- tâches d'export ---------------------------------------------------- #

    def export_tasks(self, since: datetime) -> list[dict]:
        """
        Tâches d'export réussies depuis `since`.
        Le paramètre minSubmittedAt attend une date ISO — à VÉRIFIER sur votre
        version, certaines n'acceptent que le format yyyy-MM-dd.
        """
        out, page = [], 1
        while True:
            data = self.json("/api/ce/activity",
                             type=CE_TASK_TYPE,
                             status="SUCCESS",
                             minSubmittedAt=since.strftime("%Y-%m-%d"),
                             ps=100, p=page)
            tasks = data.get("tasks", [])
            out += tasks
            paging = data.get("paging", {})
            if page * paging.get("pageSize", 100) >= paging.get("total", len(out)):
                break
            page += 1
        return out

    # -- liens du projet ---------------------------------------------------- #

    def project_links(self, project_key: str) -> list[dict]:
        return self.json("/api/project_links/search",
                         projectKey=project_key).get("links", [])

    # -- identité de l'utilisateur ------------------------------------------ #

    def user(self, login: str) -> dict | None:
        """
        Résout un utilisateur. Essaie l'API v2 puis retombe sur la v1.
        Les champs email / externalIdentity exigent le droit Administer System.
        """
        # --- v2 (SonarQube récent) ---
        try:
            r = self.s.get(f"{self.cfg.sq_url}/api/v2/users-management/users",
                           params={"q": login, "pageSize": 50},
                           timeout=self.timeout)
            if r.status_code == 200:
                for u in r.json().get("users", []):
                    if u.get("login") == login:
                        return {
                            "login": u["login"],
                            "name": u.get("name"),
                            "email": u.get("email"),
                            # selon la version : externalLogin ou externalIdentity
                            "external_identity": (u.get("externalLogin")
                                                  or u.get("externalIdentity")),
                            "external_provider": u.get("externalProvider"),
                            "local": u.get("local", False),
                            "managed": u.get("managed", False),
                            "active": u.get("active", True),
                            "api": "v2",
                        }
        except requests.RequestException:
            pass

        # --- v1 (repli) ---
        for u in self.json("/api/users/search", q=login, ps=50).get("users", []):
            if u.get("login") == login:
                return {
                    "login": u["login"],
                    "name": u.get("name"),
                    "email": u.get("email"),
                    "external_identity": u.get("externalIdentity"),
                    "external_provider": u.get("externalProvider"),
                    "local": u.get("local", False),
                    "managed": False,
                    "active": u.get("active", True),
                    "api": "v1",
                }
        return None


# --------------------------------------------------------------------------- #
#  Client Artifactory                                                          #
# --------------------------------------------------------------------------- #

class Artifactory:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {cfg.art_token}"

    def deploy(self, local: Path, espace_id: str, sha256: str | None = None) -> str:
        url = f"{self.cfg.art_url}/{self.cfg.art_repo}/{espace_id}/{local.name}"
        headers = {}
        if sha256:
            # Artifactory valide le checksum côté serveur : un transfert tronqué
            # est rejeté au dépôt, pas découvert trois heures plus tard.
            headers["X-Checksum-Sha256"] = sha256
        with local.open("rb") as fh:
            r = self.s.put(url, data=fh, headers=headers,
                           timeout=self.cfg.http_timeout * 20)
        r.raise_for_status()
        return url

    def ping(self) -> None:
        r = self.s.get(f"{self.cfg.art_url}/api/system/ping",
                       timeout=self.cfg.http_timeout)
        r.raise_for_status()


# --------------------------------------------------------------------------- #
#  Utilitaires                                                                 #
# --------------------------------------------------------------------------- #

def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_migration_link(url: str, cfg: Config) -> tuple[str, str]:
    """
    Extrait la clé cible et l'espace_id depuis l'URL du lien MIGRATION.

    Trois contrôles, dans cet ordre :
      1. le host doit être celui de l'instance centrale ;
      2. le paramètre id est décodé (une clé collée du navigateur est encodée) ;
      3. la clé doit respecter le format attendu, d'où l'on tire l'espace_id.

    Lève ValueError avec un message exploitable en cas de refus.
    """
    parsed = urllib.parse.urlparse(url.strip())

    if parsed.hostname is None or parsed.hostname.lower() != cfg.target_host:
        raise ValueError(f"host inattendu « {parsed.hostname} », "
                         f"attendu « {cfg.target_host} »")

    qs = urllib.parse.parse_qs(parsed.query)
    if "id" not in qs or not qs["id"]:
        raise ValueError("paramètre « id » absent de l'URL")

    # parse_qs décode déjà le %XX ; on nettoie les espaces résiduels
    target_key = qs["id"][0].strip()

    m = cfg.key_pattern.match(target_key)
    if not m:
        raise ValueError(f"clé cible « {target_key} » hors format attendu")

    espace_id = m.groupdict().get("espace_id")
    if not espace_id:
        raise ValueError("espace_id introuvable dans la clé cible")

    return target_key, espace_id


def find_archive(export_dir: Path, project_key: str) -> Path | None:
    """
    Localise l'archive d'export. SonarQube la nomme d'après la clé du projet ;
    les caractères spéciaux peuvent être substitués selon la version, d'où les
    variantes testées.
    """
    candidates = [
        project_key,
        project_key.replace(":", "_"),
        project_key.replace(":", "-"),
        re.sub(r"[^A-Za-z0-9._-]", "_", project_key),
    ]
    for name in candidates:
        p = export_dir / f"{name}.zip"
        if p.is_file():
            return p
    return None


def is_stable(path: Path, seconds: int) -> bool:
    """
    Un fichier encore en cours d'écriture par SonarQube changera de taille.
    Deux mesures espacées suffisent à l'écarter.
    """
    first = path.stat().st_size
    time.sleep(seconds)
    return path.exists() and path.stat().st_size == first and first > 0


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# --------------------------------------------------------------------------- #
#  Traitement d'une tâche                                                      #
# --------------------------------------------------------------------------- #

class Skip(Exception):
    """Pas une demande de migration — on ignore définitivement."""


class Retry(Exception):
    """Condition temporaire — on réessaiera au prochain cycle."""


def build_manifest(task: dict, project_key: str, target_key: str, espace_id: str,
                   requester: dict, archive: Path, digest: str,
                   sonar: Sonar, cfg: Config) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "id": str(uuid.uuid4()),
            "at": datetime.now(timezone.utc).isoformat(),
            "by": {
                "login": requester["login"],
                "name": requester.get("name"),
                "email": requester.get("email"),
                "external_identity": requester.get("external_identity"),
                "external_provider": requester.get("external_provider"),
                "managed": requester.get("managed", False),
                "resolved_from": "ce_task.submitterLogin",
            },
            # Déclaré par l'équipe via le lien MIGRATION.
            # NON vérifié ici : le contrôle des droits sur la cible se fait
            # côté instance centrale, au moment de l'import.
            "declared_target_project_key": target_key,
            "espace_id": espace_id,
        },
        "source": {
            "instance": cfg.sq_url,
            "project_key": project_key,
            "sonar_version": sonar.version(),
            "edition": cfg.sq_edition,
            "plugins_fingerprint": sonar.plugins_fingerprint(),
            "export_task_id": task["id"],
            "export_executed_at": task.get("executedAt"),
        },
        "archive": {
            "filename": archive.name,
            "sha256": digest,
            "size_bytes": archive.stat().st_size,
        },
    }


def process_task(task: dict, sonar: Sonar, art: Artifactory, cfg: Config,
                 dry_run: bool) -> str:
    project_key = task.get("componentKey")
    task_id = task["id"]

    if not project_key:
        raise Skip("tâche sans componentKey")

    # --- 1. la déclaration : le lien MIGRATION ---------------------------- #
    links = sonar.project_links(project_key)
    link = next((l for l in links
                 if (l.get("name") or "").strip().upper()
                 == cfg.link_name.upper()), None)
    if link is None:
        raise Skip(f"pas de lien « {cfg.link_name} » sur {project_key}")

    target_key, espace_id = parse_migration_link(link["url"], cfg)
    LOG.info("[%s] %s → %s (espace %s)", task_id, project_key, target_key, espace_id)

    # --- 2. l'identité du demandeur --------------------------------------- #
    submitter = task.get("submitterLogin")
    if not submitter:
        raise Skip("tâche sans submitterLogin — export déclenché sans utilisateur ?")

    requester = sonar.user(submitter)
    if requester is None:
        raise Retry(f"utilisateur « {submitter} » non résolu")
    if requester.get("local"):
        raise Skip(f"« {submitter} » est un compte local — "
                   "une migration doit être demandée par une personne")

    # --- 3. l'archive ------------------------------------------------------ #
    archive = find_archive(cfg.export_dir, project_key)
    if archive is None:
        raise Retry("archive absente du répertoire d'export")
    if not is_stable(archive, cfg.stability_seconds):
        raise Retry("archive encore en cours d'écriture")

    digest = sha256_of(archive)

    # --- 4. le manifeste --------------------------------------------------- #
    manifest = build_manifest(task, project_key, target_key, espace_id,
                              requester, archive, digest, sonar, cfg)

    if dry_run:
        LOG.info("[%s] DRY-RUN — manifeste qui aurait été publié :\n%s",
                 task_id, json.dumps(manifest, indent=2, ensure_ascii=False))
        return "dry-run"

    # --- 5. publication : l'archive d'abord, le manifeste ensuite ---------- #
    url = art.deploy(archive, espace_id, digest)
    LOG.info("[%s] archive publiée : %s", task_id, url)

    manifest_path = archive.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    try:
        # Le manifeste EN DERNIER : le script central ne réagit qu'à lui,
        # il ne peut donc jamais traiter une archive incomplète.
        art.deploy(manifest_path, espace_id)
        LOG.info("[%s] manifeste publié", task_id)
    finally:
        manifest_path.unlink(missing_ok=True)

    # --- 6. ménage --------------------------------------------------------- #
    archive.unlink(missing_ok=True)
    LOG.info("[%s] archive locale supprimée", task_id)
    return "published"


# --------------------------------------------------------------------------- #
#  Cycle principal                                                             #
# --------------------------------------------------------------------------- #

def run_cycle(cfg: Config, dry_run: bool, only_task: str | None) -> int:
    state = State(cfg.state_file)
    sonar = Sonar(cfg)
    art = Artifactory(cfg)

    try:
        LOG.debug("SonarQube version %s", sonar.version())
    except requests.RequestException as e:
        LOG.error("instance SonarQube injoignable : %s", e)
        return 2

    if not dry_run:
        try:
            art.ping()
        except requests.RequestException as e:
            LOG.error("Artifactory injoignable : %s", e)
            return 3

    since = datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)
    tasks = sonar.export_tasks(since)
    LOG.info("%d tâche(s) d'export sur les %d dernières heures",
             len(tasks), cfg.lookback_hours)

    for task in tasks:
        tid = task["id"]

        if only_task and tid != only_task:
            continue
        if not only_task and state.is_done(tid):
            continue

        try:
            result = process_task(task, sonar, art, cfg, dry_run)
            if not dry_run:
                state.mark(tid, "published", result)

        except Skip as e:
            LOG.info("[%s] ignoré : %s", tid, e)
            state.mark(tid, "ignored", str(e))

        except Retry as e:
            n = state.attempts(tid) + 1
            if n >= cfg.max_attempts:
                LOG.error("[%s] abandon après %d tentatives : %s", tid, n, e)
                quarantine(task, cfg)
                state.mark(tid, "quarantined", str(e))
            else:
                LOG.warning("[%s] tentative %d/%d : %s",
                            tid, n, cfg.max_attempts, e)
                state.mark(tid, "retry", str(e))

        except Exception as e:                       # noqa: BLE001
            n = state.attempts(tid) + 1
            LOG.exception("[%s] erreur inattendue (tentative %d) : %s", tid, n, e)
            if n >= cfg.max_attempts:
                quarantine(task, cfg)
                state.mark(tid, "quarantined", str(e))
            else:
                state.mark(tid, "retry", str(e))

    return 0


def quarantine(task: dict, cfg: Config) -> None:
    """
    Déplace l'archive hors du répertoire d'export après trop d'échecs.
    Sans cela, le répertoire se remplit et le script boucle indéfiniment
    sur les mêmes fichiers.
    """
    cfg.quarantine_dir.mkdir(parents=True, exist_ok=True)
    archive = find_archive(cfg.export_dir, task.get("componentKey", ""))
    if archive:
        dest = cfg.quarantine_dir / f"{task['id']}_{archive.name}"
        shutil.move(str(archive), str(dest))
        LOG.error("archive mise en quarantaine : %s", dest)


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="n'écrit rien, affiche les manifestes")
    ap.add_argument("--task", help="ne traiter qu'une tâche, même déjà traitée")
    args = ap.parse_args()

    try:
        cfg = Config(args.config)
    except (OSError, KeyError, ValueError, yaml.YAMLError) as e:
        print(f"configuration invalide : {e}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    lock = acquire_lock(cfg.lock_file)
    if lock is None:
        LOG.warning("un cycle est déjà en cours, on sort")
        return 4

    try:
        return run_cycle(cfg, args.dry_run, args.task)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
