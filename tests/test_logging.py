"""Logs go to stdout, in the configured format (12-Factor XI)."""

import json
import logging

import pytest

from app.logging_config import configure_logging, get_logger, request_id_var


@pytest.fixture(autouse=True)
def restore_root_logger():
    previous_handlers = logging.getLogger().handlers[:]
    previous_level = logging.getLogger().level
    yield
    root = logging.getLogger()
    root.handlers[:] = previous_handlers
    root.setLevel(previous_level)


def test_logs_go_to_stdout_not_to_a_file(capsys):
    configure_logging("INFO", "text")
    get_logger("prueba").info("servicio iniciado")

    captured = capsys.readouterr()
    assert "servicio iniciado" in captured.out
    assert captured.err == ""


def test_text_format_appends_extras_as_key_value(capsys):
    configure_logging("INFO", "text")
    get_logger("prueba").info("documento almacenado", extra={"page_count": 7})

    out = capsys.readouterr().out
    assert "documento almacenado" in out
    assert "page_count=7" in out


def test_json_format_is_one_parseable_object_per_line(capsys):
    configure_logging("INFO", "json")
    get_logger("prueba").info("documento almacenado", extra={"checksum": "abc123"})

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["message"] == "documento almacenado"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "prueba"
    assert payload["checksum"] == "abc123"


def test_the_request_id_rides_along_on_every_line(capsys):
    configure_logging("INFO", "json")
    token = request_id_var.set("trace-123")
    try:
        get_logger("prueba").info("algo pasó")
    finally:
        request_id_var.reset(token)

    assert json.loads(capsys.readouterr().out.strip())["request_id"] == "trace-123"


def test_the_level_is_respected(capsys):
    configure_logging("WARNING", "text")
    logger = get_logger("prueba")
    logger.info("no debería verse")
    logger.warning("sí debería verse")

    out = capsys.readouterr().out
    assert "no debería verse" not in out
    assert "sí debería verse" in out


def test_exceptions_are_rendered_in_json(capsys):
    configure_logging("ERROR", "json")
    try:
        raise ValueError("se rompió")
    except ValueError:
        get_logger("prueba").exception("fallo")

    payload = json.loads(capsys.readouterr().out.strip())
    assert "ValueError" in payload["exception"]


def test_reconfiguring_does_not_duplicate_handlers(capsys):
    configure_logging("INFO", "text")
    configure_logging("INFO", "text")
    get_logger("prueba").info("una sola vez")

    assert capsys.readouterr().out.count("una sola vez") == 1
