from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from effect_browser import cli
from effect_browser.domain import AutonomyMode, AutonomyScope
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
