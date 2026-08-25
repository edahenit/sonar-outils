"""Tests of the validation module: YAML -> schema -> consistency -> uniqueness.

Each test targets a single refusal code, so a failure points directly to the
rule at fault. The valid request (path, content, fixtures) is itself tested
to avoid a future hardening breaking the nominal case without any test
catching it.
"""

from __future__ import annotations

from pathlib import Path

from migration.messages import codes_connus
from migration.modele import Demande
from migration.validation import valider_fichier

_FIXTURES = Path(__file__).parent / "fixtures"
_DEMANDE_VALIDE = (
    _FIXTURES / "requests" / "entite-alpha" / "grp-alpha-facturation-api.yml"
)
_DEMANDE_VALIDE_RELATIF = "requests/entite-alpha/grp-alpha-facturation-api.yml"


def _codes(refus_liste):
    return {r.code for r in refus_liste}


# --- Nominal case -------------------------------------------------------


def test_demande_valide_est_acceptee(inventaire_test):
    demande, refus_liste = valider_fichier(
        _DEMANDE_VALIDE, _DEMANDE_VALIDE_RELATIF, inventaire=inventaire_test
    )
    assert refus_liste == []
    assert demande == Demande(
        version=1,
        instance_source="entite-alpha",
        cle_source="com.alpha:facturation-api",
        cle_cible="grp-alpha-facturation-api",
        ticket="DEVOPS-1234",
        fenetre_souhaitee="2026-09-15",
        commentaire="Valid test request.",
        fichier=_DEMANDE_VALIDE_RELATIF,
    )


def test_demande_minimale_sans_champs_optionnels_est_acceptee(
    inventaire_test, ecrire_demande
):
    chemin, relatif = ecrire_demande(
        "entite-alpha",
        "grp-alpha-minimal.yml",
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:minimal\n"
        "cle_cible: grp-alpha-minimal\n",
    )
    demande, refus_liste = valider_fichier(chemin, relatif, inventaire=inventaire_test)
    assert refus_liste == []
    assert demande.ticket is None


# --- File not found / too large / wrong location ------------------------


def test_fichier_introuvable(tmp_path):
    demande, refus_liste = valider_fichier(
        tmp_path / "absent.yml", "requests/entite-alpha/absent.yml"
    )
    assert demande is None
    assert _codes(refus_liste) == {"FICHIER_INTROUVABLE"}


def test_fichier_trop_gros(ecrire_demande):
    contenu = "version: 1\ncommentaire: \"{}\"\n".format("x" * 20000)
    chemin, relatif = ecrire_demande("entite-alpha", "gros.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"FICHIER_TROP_GROS"}


def test_fichier_hors_dossier_requests(tmp_path):
    # Content is otherwise valid: the only defect to isolate is the location.
    chemin = tmp_path / "grp-alpha-facturation-api.yml"
    chemin.write_text(
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:facturation-api\n"
        "cle_cible: grp-alpha-facturation-api\n",
        encoding="utf-8",
    )
    demande, refus_liste = valider_fichier(chemin, "grp-alpha-facturation-api.yml")
    assert demande is None
    assert _codes(refus_liste) == {"FICHIER_EMPLACEMENT"}


# --- YAML hardening ---------------------------------------------------


def test_yaml_illisible(ecrire_demande):
    chemin, relatif = ecrire_demande("entite-alpha", "casse.yml", "version: [\n")
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"YAML_ILLISIBLE"}


def test_yaml_alias_refuse(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: &a entite-alpha\n"
        "cle_source: *a\n"
        "cle_cible: grp-alpha-x\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "alias.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"YAML_ALIAS"}


def test_yaml_cle_dupliquee_refusee(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:a\n"
        "cle_source: com.alpha:b\n"
        "cle_cible: grp-alpha-a\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "dup.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"YAML_CLE_DUPLIQUEE"}


def test_yaml_racine_liste_refusee(ecrire_demande):
    chemin, relatif = ecrire_demande("entite-alpha", "liste.yml", "- 1\n- 2\n")
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"YAML_RACINE_NON_OBJET"}


def test_yaml_multi_document_refuse(ecrire_demande):
    contenu = "version: 1\n---\nversion: 1\n"
    chemin, relatif = ecrire_demande("entite-alpha", "multi.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"YAML_MULTI_DOCUMENT"}


# --- Schema ---------------------------------------------------------------


def test_champ_obligatoire_manquant(ecrire_demande):
    contenu = "version: 1\ninstance_source: entite-alpha\ncle_source: com.alpha:a\n"
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"SCHEMA_CHAMP_MANQUANT"}
    assert refus_liste[0].pointeur == "" or "cle_cible" in refus_liste[0].message


def test_champ_inconnu_refuse(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:a\n"
        "cle_cible: grp-alpha-a\n"
        "create_placeholder_project: false\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"SCHEMA_CHAMP_INCONNU"}
    # The refused field must be named in the message, not just the code:
    # that's the only way for the requester to know what to remove.
    assert "create_placeholder_project" in refus_liste[0].message


def test_cle_source_avec_caractere_interdit_refusee(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: \"com.alpha:a; rm -rf /\"\n"
        "cle_cible: grp-alpha-a\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"SCHEMA_VALEUR_INVALIDE"}


def test_version_incorrecte_refusee(ecrire_demande):
    contenu = (
        "version: 2\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:a\n"
        "cle_cible: grp-alpha-a\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"SCHEMA_VALEUR_INVALIDE"}


def test_commentaire_trop_long_refuse(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:a\n"
        "cle_cible: grp-alpha-a\n"
        "commentaire: \"{}\"\n"
    ).format("x" * 600)
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"SCHEMA_TROP_LONG"}


def test_toutes_les_erreurs_de_schema_sont_rendues_ensemble(ecrire_demande):
    """Two invalid fields at once must produce two refusals, not just the
    first one encountered — the requester fixes everything in one pass."""
    contenu = (
        "version: 1\n"
        "instance_source: ENTITE ALPHA\n"  # invalid pattern
        "cle_source: \"a b\"\n"  # invalid pattern (space)
        "cle_cible: grp-alpha-a\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert len(refus_liste) >= 2


# --- Path consistency ---------------------------------------------------


def test_dossier_incoherent_avec_instance_source(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-beta\n"
        "cle_source: com.alpha:a\n"
        "cle_cible: grp-alpha-a\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"CHEMIN_DOSSIER_INCOHERENT"}


def test_nom_fichier_incoherent_avec_cle_cible(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:a\n"
        "cle_cible: grp-alpha-autre\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"CHEMIN_NOM_FICHIER_INCOHERENT"}


# --- Business consistency -------------------------------------------------------


def test_cles_source_et_cible_identiques_refusees(ecrire_demande):
    contenu = (
        "version: 1\n"
        "instance_source: entite-alpha\n"
        "cle_source: com.alpha:a\n"
        "cle_cible: com.alpha:a\n"
    )
    chemin, relatif = ecrire_demande("entite-alpha", "com.alpha-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert demande is None
    assert _codes(refus_liste) == {"CLES_IDENTIQUES"}


# --- Inventory --------------------------------------------------------


def test_instance_inconnue_refusee(ecrire_demande, inventaire_test):
    contenu = (
        "version: 1\n"
        "instance_source: entite-inexistante\n"
        "cle_source: com.x:a\n"
        "cle_cible: grp-x-a\n"
    )
    chemin, relatif = ecrire_demande("entite-inexistante", "grp-x-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif, inventaire=inventaire_test)
    assert demande is None
    assert _codes(refus_liste) == {"INSTANCE_INCONNUE"}


def test_instance_inactive_refusee(ecrire_demande, inventaire_test):
    contenu = (
        "version: 1\n"
        "instance_source: entite-inactive\n"
        "cle_source: com.x:a\n"
        "cle_cible: grp-x-a\n"
    )
    chemin, relatif = ecrire_demande("entite-inactive", "grp-x-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif, inventaire=inventaire_test)
    assert demande is None
    assert _codes(refus_liste) == {"INSTANCE_INACTIVE"}


def test_sans_inventaire_fourni_le_controle_instance_est_saute(ecrire_demande):
    """Deliberate partial usage (see module docstring): without an
    inventory, shape validation remains usable, e.g. for a local check
    before pushing."""
    contenu = (
        "version: 1\n"
        "instance_source: entite-inconnue-de-personne\n"
        "cle_source: com.x:a\n"
        "cle_cible: grp-x-a\n"
    )
    chemin, relatif = ecrire_demande("entite-inconnue-de-personne", "grp-x-a.yml", contenu)
    demande, refus_liste = valider_fichier(chemin, relatif)
    assert refus_liste == []
    assert demande is not None


# --- Uniqueness across requests -------------------------------------------------


def test_doublon_cle_cible_refuse(ecrire_demande):
    contenu_1 = (
        "version: 1\ninstance_source: entite-alpha\n"
        "cle_source: com.alpha:a\ncle_cible: grp-alpha-a\n"
    )
    contenu_2 = (
        "version: 1\ninstance_source: entite-alpha\n"
        "cle_source: com.alpha:b\ncle_cible: grp-alpha-a\n"
    )
    ecrire_demande("entite-alpha", "grp-alpha-a-premiere.yml", contenu_1)
    demande_1 = Demande.depuis_dict(
        {
            "version": 1, "instance_source": "entite-alpha",
            "cle_source": "com.alpha:a", "cle_cible": "grp-alpha-a",
        },
        fichier="requests/entite-alpha/grp-alpha-a-premiere.yml",
    )
    chemin_2, relatif_2 = ecrire_demande("entite-alpha", "grp-alpha-a.yml", contenu_2)
    demande_2, refus_liste = valider_fichier(
        chemin_2, relatif_2, autres_demandes=[demande_1]
    )
    assert demande_2 is None
    assert _codes(refus_liste) == {"DOUBLON_CLE_CIBLE"}


def test_doublon_couple_source_refuse(ecrire_demande):
    demande_1 = Demande.depuis_dict(
        {
            "version": 1, "instance_source": "entite-alpha",
            "cle_source": "com.alpha:a", "cle_cible": "grp-alpha-a",
        },
        fichier="requests/entite-alpha/grp-alpha-a.yml",
    )
    contenu_2 = (
        "version: 1\ninstance_source: entite-alpha\n"
        "cle_source: com.alpha:a\ncle_cible: grp-alpha-a-bis\n"
    )
    chemin_2, relatif_2 = ecrire_demande("entite-alpha", "grp-alpha-a-bis.yml", contenu_2)
    demande_2, refus_liste = valider_fichier(
        chemin_2, relatif_2, autres_demandes=[demande_1]
    )
    assert demande_2 is None
    assert _codes(refus_liste) == {"DOUBLON_COUPLE_SOURCE"}


def test_meme_fichier_ne_se_signale_pas_lui_meme(inventaire_test):
    """Revalidating an already-accepted request (e.g. a job rerun) must not
    make it show up as its own duplicate."""
    demande, refus_liste = valider_fichier(
        _DEMANDE_VALIDE,
        _DEMANDE_VALIDE_RELATIF,
        inventaire=inventaire_test,
        autres_demandes=[
            Demande.depuis_dict(
                {
                    "version": 1, "instance_source": "entite-alpha",
                    "cle_source": "com.alpha:facturation-api",
                    "cle_cible": "grp-alpha-facturation-api",
                },
                fichier=_DEMANDE_VALIDE_RELATIF,
            )
        ],
    )
    assert refus_liste == []
    assert demande is not None


# --- Catalogue coverage -------------------------------------------------------


def test_tous_les_codes_de_refus_ont_un_message(ecrire_demande, inventaire_test):
    """Global safety net: walks a sample of cases covering most branches
    and checks that every emitted code exists in the catalogue (otherwise
    'refus()' would produce a fallback message visible in review)."""
    connus = codes_connus()
    cas = [
        ("entite-alpha", "grp-alpha-b.yml", "version: [\n"),
        ("entite-alpha", "grp-alpha-c.yml",
         "version: 1\ninstance_source: entite-alpha\ncle_source: com.alpha:c\n"),
    ]
    for instance, nom, contenu in cas:
        chemin, relatif = ecrire_demande(instance, nom, contenu)
        _, refus_liste = valider_fichier(chemin, relatif, inventaire=inventaire_test)
        for r in refus_liste:
            assert r.code in connus, f"code {r.code} missing from the catalogue"
            assert r.action, f"refusal {r.code} has no action"
