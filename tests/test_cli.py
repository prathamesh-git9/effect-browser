from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from effect_browser import cli
from effect_browser.domain import (
    AutonomyMode,
    AutonomyScope,
    MissionVerdict,
)
from effect_browser.mcp_server import _absolute_document_path as mcp_document_path


def test_ascii_safe_json_round_trips_unicode_on_cp1252() -> None:
    payload = {"message": "R\u00e9sum\u00e9 ready \U0001f680", "status": "complete"}

    rendered = cli._ascii_safe_json(payload)

    assert rendered.isascii()
    assert rendered.encode("cp1252").decode("cp1252") == rendered
    assert json.loads(rendered) == payload


def test_cli_and_mcp_reject_relative_document_paths(tmp_path: Path) -> None:
    with pytest.raises(typer.BadParameter, match="must be absolute"):
        cli._absolute_document_path(Path("resume.pdf"))
    with pytest.raises(ValueError, match="must be absolute"):
        mcp_document_path("resume.pdf")

    document = (tmp_path / "resume.pdf").resolve()
    assert cli._absolute_document_path(document) == document
    assert mcp_document_path(str(document)) == document


def test_worker_reports_one_task_failure_and_exits_cleanly(monkeypatch) -> None:
    task = SimpleNamespace(
        id="synthetic-task",
        status=SimpleNamespace(value="queued"),
        start_url="https://example.com",
        autonomy=AutonomyScope(mode=AutonomyMode.SUPERVISED),
    )
    service = SimpleNamespace(
        store=SimpleNamespace(
            list_tasks=lambda _tenant_id: [task],
            list_missions=lambda _tenant_id: [],
            mission_for_child_task=lambda _tenant_id, _task_id: None,
        )
    )
    monkeypatch.setattr(cli, "_service", lambda: service)
    monkeypatch.setattr(
        cli,
        "_driver",
        lambda _origins, **_kwargs: (_ for _ in ()).throw(RuntimeError("launch failed")),
    )

    result = CliRunner().invoke(cli.app, ["worker", "--once"])

    assert result.exit_code == 0
    assert "RuntimeError: launch failed" in result.output


def test_do_requires_explicit_commit_flag_and_forwards_it(monkeypatch) -> None:
    calls: list[dict] = []

    class Coordinator:
        def __init__(self, **_kwargs):
            pass

        def execute(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                verdict=MissionVerdict.COMPLETED,
                model_dump_json=lambda: json.dumps(
                    {
                        "verdict": "completed",
                        "message": "R\u00e9sum\u00e9 ready \U0001f680",
                    },
                    ensure_ascii=False,
                ),
            )

    monkeypatch.setattr(
        cli,
        "_service",
        lambda: SimpleNamespace(store=SimpleNamespace()),
    )
    monkeypatch.setattr(cli, "MissionCoordinator", Coordinator)

    without_grant = CliRunner().invoke(cli.app, ["do", "Research in order to compare."])
    with_grant = CliRunner().invoke(
        cli.app,
        ["do", "Submit the form.", "--commit"],
    )

    assert without_grant.exit_code == 0
    assert with_grant.exit_code == 0
    assert without_grant.stdout.isascii()
    assert (
        json.loads(without_grant.stdout)["message"] == "R\u00e9sum\u00e9 ready \U0001f680"
    )
    assert calls[0]["allow_external_commit"] is False
    assert calls[1]["allow_external_commit"] is True


def test_do_reports_progress_on_stderr_before_work_and_keeps_stdout_json(
    monkeypatch,
) -> None:
    class Coordinator:
        def __init__(self, **_kwargs):
            pass

        def execute(self, **_kwargs):
            typer.echo("synthetic coordinator entered", err=True)
            return SimpleNamespace(
                verdict=MissionVerdict.COMPLETED,
                model_dump_json=lambda: json.dumps(
                    {
                        "verdict": "completed",
                        "message": "synthetic mission completed",
                    }
                ),
            )

    monkeypatch.setattr(
        cli,
        "_service",
        lambda: SimpleNamespace(store=SimpleNamespace()),
    )
    monkeypatch.setattr(cli, "MissionCoordinator", Coordinator)

    result = CliRunner().invoke(cli.app, ["do", "Inspect the synthetic fixture."])

    assert result.exit_code == 0
    assert result.stderr.splitlines() == [
        "Planning and running the mission; browser work is headless by default...",
        "synthetic coordinator entered",
    ]
    assert json.loads(result.stdout) == {
        "message": "synthetic mission completed",
        "verdict": "completed",
    }
