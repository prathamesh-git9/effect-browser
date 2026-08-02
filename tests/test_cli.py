from __future__ import annotations

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
        lambda _origins: (_ for _ in ()).throw(RuntimeError("launch failed")),
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
                model_dump_json=lambda: '{"verdict":"completed"}',
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
    assert calls[0]["allow_external_commit"] is False
    assert calls[1]["allow_external_commit"] is True
