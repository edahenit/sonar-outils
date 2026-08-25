"""Tests of loading the instance inventory."""

from __future__ import annotations

from pathlib import Path

import pytest

from migration.inventaire import ErreurInventaire, catalogue_public, charger_inventaire

_FIXTURES = Path(__file__).parent / "fixtures"


def test_inventaire_reel_du_depot_se_charge(tmp_path):
    """The inventory actually shipped in the repository must conform to
    its own schema: a defect here would break the whole pipeline in
    production."""
    racine_engine = Path(__file__).resolve().parents[1]
    chemin = racine_engine / "inventaire" / "instances.yml"
    inventaire = charger_inventaire(chemin)
    assert inventaire.centrale.id == "centrale"
    assert inventaire.centrale.variable_token == "SONAR_CENTRALE_TOKEN"


def test_nom_de_variable_derive_de_lidentifiant(tmp_path):
    inventaire = charger_inventaire(_FIXTURES / "instances_test.yml")
    source = inventaire.source("entite-alpha")
    assert source is not None
    assert source.variable_token == "SONAR_SRC_ENTITE_ALPHA_TOKEN"


def test_instance_inactive_presente_mais_marquee(tmp_path):
    inventaire = charger_inventaire(_FIXTURES / "instances_test.yml")
    source = inventaire.source("entite-inactive")
    assert source is not None
    assert source.actif is False
    assert source not in inventaire.sources_actives()


def test_variable_token_hors_convention_refusee(tmp_path):
    chemin = tmp_path / "instances.yml"
    chemin.write_text(
        "version: 1\n"
        "groupes_interdits: [Anyone]\n"
        "centrale:\n"
        "  id: centrale\n"
        "  libelle: Centrale\n"
        "  url: https://sonar.example\n"
        "  api_identite: v2\n"
        "  ssh_hote: h\n"
        "  sonarqube_home: /opt/sonarqube\n"
        "instances_sources:\n"
        "  - id: entite-x\n"
        "    libelle: Entity X\n"
        "    url: https://sonar.x.example\n"
        "    api_identite: v1\n"
        "    ssh_hote: h\n"
        "    sonarqube_home: /opt/sonarqube\n"
        "    variable_token: UN_SECRET_QUELCONQUE\n",
        encoding="utf-8",
    )
    with pytest.raises(ErreurInventaire):
        charger_inventaire(chemin)


def test_identifiant_instance_source_duplique_refuse(tmp_path):
    chemin = tmp_path / "instances.yml"
    chemin.write_text(
        "version: 1\n"
        "groupes_interdits: [Anyone]\n"
        "centrale:\n"
        "  id: centrale\n"
        "  libelle: Centrale\n"
        "  url: https://sonar.example\n"
        "  api_identite: v2\n"
        "  ssh_hote: h\n"
        "  sonarqube_home: /opt/sonarqube\n"
        "instances_sources:\n"
        "  - {id: entite-x, libelle: X, url: 'https://a.example', api_identite: v1, "
        "ssh_hote: h, sonarqube_home: /opt/sonarqube}\n"
        "  - {id: entite-x, libelle: X2, url: 'https://b.example', api_identite: v1, "
        "ssh_hote: h, sonarqube_home: /opt/sonarqube}\n",
        encoding="utf-8",
    )
    with pytest.raises(ErreurInventaire):
        charger_inventaire(chemin)


def test_inventaire_non_conforme_au_schema_refuse(tmp_path):
    chemin = tmp_path / "instances.yml"
    chemin.write_text("version: 1\n", encoding="utf-8")  # required fields missing
    with pytest.raises(ErreurInventaire):
        charger_inventaire(chemin)


def test_catalogue_public_ne_publie_que_id_et_libelle(tmp_path):
    inventaire = charger_inventaire(_FIXTURES / "instances_test.yml")
    catalogue = catalogue_public(inventaire)
    # The inactive instance does not appear in the catalogue published
    # to requesters.
    assert catalogue == [{"id": "entite-alpha", "libelle": "Entity Alpha"}]
    for entree in catalogue:
        assert set(entree.keys()) == {"id", "libelle"}
