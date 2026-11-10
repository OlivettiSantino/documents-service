"""Invariantes de la CI que son de seguridad, no de estilo (issue #57).

Un tag de GitHub Action es **mutable**: quien controla el repo de la action
puede moverlo a otro commit, y eso corre en nuestra CI con acceso a
``secrets.GITHUB_TOKEN``. Por eso todas van fijadas por SHA de commit. Es una
regla fácil de romper sin querere al agregar un paso nuevo, así que la verifica
un test en vez de la memoria de quien revisa el PR.

Se lee el YAML como texto a propósito: lo que se quiere afirmar es cómo está
*escrito* el archivo (el SHA y su comentario de versión), no la estructura que
quedaría después de parsearlo.
"""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
DEPENDABOT = Path(__file__).resolve().parent.parent / ".github" / "dependabot.yml"

#: `uses:` de una action de GitHub, con lo que venga después del `@`.
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<action>[^@\s]+)@(?P<ref>\S+)", re.MULTILINE)

#: Un SHA de commit completo. Los 7 caracteres de un SHA corto no alcanzan:
#: son falsificables por colisión de prefijo.
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def actions(workflow: str) -> list[tuple[str, str]]:
    return [(m.group("action"), m.group("ref")) for m in USES.finditer(workflow)]


def test_hay_actions_que_revisar(workflow):
    """Si el regex deja de matchear, el resto de los tests pasarían vacíos."""
    assert len(actions(workflow)) >= 8


def test_todas_las_actions_estan_fijadas_por_sha_de_commit(workflow):
    sin_fijar = [
        f"{action}@{ref}" for action, ref in actions(workflow) if not FULL_SHA.match(ref)
    ]

    assert not sin_fijar, f"actions sin SHA de commit: {sin_fijar}"


def test_cada_sha_lleva_su_version_en_un_comentario(workflow):
    """Un SHA solo no dice qué versión es: el comentario `# vX.Y.Z` es lo que
    hace revisable el diff de una actualización."""
    sin_comentario = [
        linea.strip()
        for linea in workflow.splitlines()
        if USES.match(linea) and "#" not in linea
    ]

    assert not sin_comentario, f"sin comentario de version: {sin_comentario}"


def test_las_herramientas_de_escaneo_tienen_version_exacta(workflow):
    """Grype y pip-audit fijados: el resultado de un escaneo no puede cambiar
    porque salio una version nueva del escaner."""
    assert re.search(r"GRYPE_VERSION:\s*v\d+\.\d+\.\d+", workflow)
    assert re.search(r"pip-audit==\d+\.\d+\.\d+", workflow)


def test_el_sbom_se_genera_antes_de_escanear_y_de_publicar(workflow):
    """Asi describe la imagen que efectivamente se escanea y se publica."""
    sbom = workflow.index("anchore/sbom-action")
    grype = workflow.index("anchore/scan-action")
    push = workflow.index("name: Push a GHCR")

    assert sbom < grype < push


def test_el_sbom_no_se_escribe_dentro_del_repo(workflow):
    """Como el reporte de Grype: no tiene que ser parte del contexto de build."""
    assert "output-file: ${{ runner.temp }}/sbom.spdx.json" in workflow


def test_solo_el_job_de_la_imagen_puede_escribir_paquetes(workflow):
    """`packages: write` es el permiso que permite publicar en GHCR: el resto
    del workflow se queda en `contents: read`.

    Se cuentan lineas de YAML, no menciones: el permiso tambien aparece en los
    comentarios que lo explican.
    """
    otorgado = [
        linea for linea in workflow.splitlines() if linea.strip() == "packages: write"
    ]

    assert len(otorgado) == 1


# --- Dependabot (issue #57) ------------------------------------------------


@pytest.fixture(scope="module")
def dependabot() -> str:
    return DEPENDABOT.read_text(encoding="utf-8")


@pytest.mark.parametrize("ecosystem", ["uv", "github-actions", "docker-compose"])
def test_dependabot_cubre_cada_fuente_de_dependencias(dependabot, ecosystem):
    assert f'package-ecosystem: "{ecosystem}"' in dependabot


def test_dependabot_no_declara_el_ecosystem_docker(dependabot):
    """No es un olvido: Dependabot no resuelve variables en las lineas FROM, y
    las dos imagenes del Dockerfile entran por ARG. Una entrada "docker" no
    encontraria nada y daria una falsa sensacion de cobertura."""
    assert 'package-ecosystem: "docker"' not in dependabot


def test_el_dockerfile_sigue_usando_un_solo_arg_por_imagen(dependabot):
    """La contracara del test de arriba: si algun dia las imagenes se escriben
    literales en cada FROM, Dependabot ya las veria y corresponde agregar el
    ecosystem "docker"."""
    dockerfile = (WORKFLOW.parent.parent.parent / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM ${PYTHON_IMAGE}" in dockerfile
    assert "ARG PYTHON_IMAGE=" in dockerfile
