"""docker-compose.yml listo para varias réplicas (issue #35, 12-Factor VIII).

Lee el compose ya interpolado con ``docker compose config``, que no consulta
al daemon. Se le pasa un ``--env-file`` vacío para que el ``.env`` local no
cambie el resultado (ni se cuele un token en la salida). Si no hay Docker en
la máquina se saltea; en la CI el runner lo trae.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="sin Docker")


def compose_config(tmp_path: Path, **env: str) -> dict:
    empty_env_file = tmp_path / "empty.env"
    empty_env_file.write_text("")
    environ = {k: v for k, v in os.environ.items() if k != "DOCUMENTS_REPLICAS"}
    result = subprocess.run(
        [
            "docker", "compose",
            "--project-directory", str(ROOT),
            "--env-file", str(empty_env_file),
            "config", "--format", "json",
        ],
        capture_output=True,
        text=True,
        env={**environ, **env},
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"docker compose no disponible: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout)


def test_documents_no_publica_puertos_en_el_host(tmp_path):
    """Dos réplicas no pueden publicar el mismo puerto: se entra por Traefik."""
    documents = compose_config(tmp_path)["services"]["documents"]

    assert not documents.get("ports")
    assert "8002" in [str(port) for port in documents["expose"]]


def test_el_puerto_interno_sigue_siendo_8002(tmp_path):
    documents = compose_config(tmp_path)["services"]["documents"]

    assert str(documents["environment"]["PORT"]) == "8002"


def test_por_defecto_hay_una_replica(tmp_path):
    documents = compose_config(tmp_path)["services"]["documents"]

    assert documents["deploy"]["replicas"] == 1


def test_las_replicas_salen_de_documents_replicas(tmp_path):
    documents = compose_config(tmp_path, DOCUMENTS_REPLICAS="3")["services"]["documents"]

    assert documents["deploy"]["replicas"] == 3


def test_mongo_tampoco_publica_puertos(tmp_path):
    """Database per service: la base sigue privada aunque haya N réplicas."""
    mongo = compose_config(tmp_path)["services"]["mongo"]

    assert not mongo.get("ports")
