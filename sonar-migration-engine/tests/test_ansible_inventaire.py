"""Tests of the dynamic Ansible inventory.

It does only one thing: translate ``inventaire/instances.yml`` (which is
authoritative, already validated by ``migration.inventaire``) into the JSON
format Ansible expects from a dynamic inventory script — groups,
``_meta.hostvars``. No new source of truth: an instance added in
instances.yml shows up here automatically, nothing duplicated.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "ansible" / "inventaire"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from depuis_instances import construire_inventaire

_FIXTURES = Path(__file__).parent / "fixtures"


def test_deux_groupes_source_et_centrale():
    inv = construire_inventaire(_FIXTURES / "instances_test.yml")
    assert inv["sonar_centrale"]["hosts"] == ["centrale"]
    assert set(inv["sonar_source"]["hosts"]) == {"entite-alpha", "entite-inactive"}


def test_hostvars_portent_les_champs_utiles_sans_jamais_de_token():
    inv = construire_inventaire(_FIXTURES / "instances_test.yml")
    hv = inv["_meta"]["hostvars"]["entite-alpha"]
    assert hv["ansible_host"] == "sonar-alpha.test"
    assert hv["sonar_url"] == "https://sonar.alpha.test"
    assert hv["sonar_home"] == "/opt/sonarqube"
    assert hv["sonar_api_identite"] == "v1"
    assert hv["sonar_fournisseur_identite_sso"] == "saml-entreprise"
    assert hv["sonar_role"] == "source"
    assert hv["sonar_variable_token"] == "SONAR_SRC_ENTITE_ALPHA_TOKEN"
    # The NAME of the protected variable is legitimate to publish (it's
    # used to know which CI secret to use); its VALUE must never transit
    # here.
    assert not any("token" == k.lower() for k in hv)
    for valeur in hv.values():
        assert "squ_" not in str(valeur) and "glpat-" not in str(valeur)


def test_instance_inactive_est_quand_meme_listee():
    """The Ansible inventory does not filter out inactive instances: it is
    the (Python) authorization check's job to refuse a request on an
    inactive instance, not the server inventory's job to make it
    disappear."""
    inv = construire_inventaire(_FIXTURES / "instances_test.yml")
    assert "entite-inactive" in inv["_meta"]["hostvars"]


def test_format_conforme_au_contrat_dinventaire_dynamique_ansible():
    """--list must produce groups with 'hosts' and a '_meta.hostvars' —
    this is the convention Ansible expects from an external inventory
    script."""
    inv = construire_inventaire(_FIXTURES / "instances_test.yml")
    for groupe in ("sonar_source", "sonar_centrale"):
        assert "hosts" in inv[groupe]
        assert isinstance(inv[groupe]["hosts"], list)
    assert "_meta" in inv and "hostvars" in inv["_meta"]
