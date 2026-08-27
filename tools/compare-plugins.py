#!/usr/bin/env python3
"""
compare-plugins.py — compatibilité des plugins entre deux instances SonarQube.

Aucune dépendance : bibliothèque standard uniquement, pas de pip, pas de jq.

Règle de Project Move : l'instance CIBLE doit posséder tous les plugins de la
SOURCE, dans les mêmes versions. La cible peut en avoir davantage.

Usage
    # depuis deux fichiers récupérés par curl
    python3 compare-plugins.py entite.json centrale.json

    # directement depuis les deux instances
    python3 compare-plugins.py \\
        --src https://sonar.entite.corp --src-token squ_xxx \\
        --tgt https://sonar.groupe.corp --tgt-token squ_yyy

    # sortie exploitable par un script
    python3 compare-plugins.py entite.json centrale.json --json

Codes de sortie : 0 compatible · 1 écart bloquant · 2 erreur
"""

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request


def charger_url(base, token):
    """Récupère api/plugins/installed. Le token exige Administer System."""
    url = base.rstrip("/") + "/api/plugins/installed"
    auth = base64.b64encode(f"{token}:".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def normaliser(data):
    """→ { cle: {version, bundled} }"""
    out = {}
    for p in data.get("plugins", []):
        out[p["key"]] = {
            "version": p.get("version", ""),
            "bundled": p.get("editionBundled", False),
        }
    return out


def comparer(src, tgt):
    absents = [
        {"key": k, "version": v["version"], "bundled": v["bundled"]}
        for k, v in sorted(src.items())
        if k not in tgt
    ]
    versions = [
        {
            "key": k,
            "bundled": v["bundled"],
            "version_source": v["version"],
            "version_cible": tgt[k]["version"],
        }
        for k, v in sorted(src.items())
        if k in tgt and tgt[k]["version"] != v["version"]
    ]
    extras = [
        {"key": k, "version": v["version"]}
        for k, v in sorted(tgt.items())
        if k not in src
    ]
    return {
        "absents_cible": absents,
        "versions_differentes": versions,
        "cible_seulement": extras,
        "compatible": not absents and not versions,
    }


def afficher(rapport, nom_src, nom_tgt, n_src, n_tgt):
    print("Comparaison des plugins")
    print(f"  source : {nom_src}  ({n_src} plugins)")
    print(f"  cible  : {nom_tgt}  ({n_tgt} plugins)")

    abs_ = rapport["absents_cible"]
    ver = rapport["versions_differentes"]
    ext = rapport["cible_seulement"]

    if abs_:
        print(f"\nBLOQUANT — {len(abs_)} plugin(s) de la source absent(s) de la cible")
        print("  La CIBLE doit les installer, ou la source doit les retirer.\n")
        for p in abs_:
            suffixe = "   [fourni avec l'edition]" if p["bundled"] else ""
            print(f"    {p['key']} {p['version']}{suffixe}")

    if ver:
        print(f"\nBLOQUANT — {len(ver)} plugin(s) en version differente")
        print("  La SOURCE doit s'aligner sur la version de la cible.\n")
        for p in ver:
            suffixe = "   [fourni avec l'edition]" if p["bundled"] else ""
            print(f"    {p['key']}   source {p['version_source']}"
                  f"  ->  cible {p['version_cible']}{suffixe}")

    if ext:
        print(f"\nSANS EFFET — {len(ext)} plugin(s) present(s) uniquement sur la cible")
        print("  Autorise par Project Move. Aucune action cote source.\n")
        for p in ext[:20]:
            print(f"    {p['key']} {p['version']}")
        if len(ext) > 20:
            print(f"    ... et {len(ext) - 20} autre(s)")

    print()
    if rapport["compatible"]:
        print("COMPATIBLE — l'import peut avoir lieu.")
    else:
        print(f"INCOMPATIBLE — {len(abs_) + len(ver)} ecart(s) bloquant(s) a traiter.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fichiers", nargs="*", metavar="SOURCE.json CIBLE.json")
    ap.add_argument("--src"); ap.add_argument("--src-token")
    ap.add_argument("--tgt"); ap.add_argument("--tgt-token")
    ap.add_argument("--json", action="store_true", help="sortie JSON")
    a = ap.parse_args()

    try:
        if len(a.fichiers) == 2:
            nom_src, nom_tgt = a.fichiers
            src_raw = json.load(open(nom_src, encoding="utf-8"))
            tgt_raw = json.load(open(nom_tgt, encoding="utf-8"))
        elif a.src and a.tgt:
            nom_src, nom_tgt = a.src, a.tgt
            src_raw = charger_url(a.src, a.src_token or "")
            tgt_raw = charger_url(a.tgt, a.tgt_token or "")
        else:
            ap.print_help()
            return 2
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as e:
        print(f"erreur de lecture : {e}", file=sys.stderr)
        return 2

    src, tgt = normaliser(src_raw), normaliser(tgt_raw)
    rapport = comparer(src, tgt)

    if a.json:
        print(json.dumps(rapport, indent=2, ensure_ascii=False))
    else:
        afficher(rapport, nom_src, nom_tgt, len(src), len(tgt))

    return 0 if rapport["compatible"] else 1


if __name__ == "__main__":
    sys.exit(main())
