from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import text
from typer.testing import CliRunner

from effect_browser import cli
from effect_browser.domain import (
    ActionKind,
    MissionPlan,
    MissionPlanStep,
    MissionStepKind,
    ProposedAction,
)
from effect_browser.store import DatabaseStore

TENANT = UUID("72000000-0000-0000-0000-000000000001")


def test_mission_timeline_merges_global_sequence_and_redacts_content(
    tmp_path: Path,
) -> None:
    store, mission_id = _mission_with_interleaved_child(tmp_path)

    first = store.mission_timeline(TENANT, mission_id)
    second = store.mission_timeline(TENANT, mission_id)

    assert first == second
    assert first["audit"]["valid"] is True
    assert first["audit"]["event_count"] == 6
    assert [event["sequence"] for event in first["events"]] == [1, 2, 3, 5, 6]
    assert [event["scope"] for event in first["events"]] == [
        "mission",
        "mission",
        "mission",
        "browser_child",
        "mission",
    ]
    serialized = json.dumps(first, sort_keys=True)
    for sensitive in (
        "secret query text",
        "secret plan summary",
        "secret browser instruction",
        "secret authority reason",
        "secret task instruction",
        "secret durable output",
        "secret-provider",
    ):
        assert sensitive not in serialized
    assert "redacted_fields" in serialized
    store.close()


def test_replay_mission_is_byte_stable_and_fails_for_a_broken_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, mission_id = _mission_with_interleaved_child(tmp_path)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(default_tenant_id=TENANT),
    )
    monkeypatch.setattr(cli, "_service", lambda: SimpleNamespace(store=store))
    runner = CliRunner()

    first = runner.invoke(cli.app, ["replay-mission", str(mission_id)])
    second = runner.invoke(cli.app, ["replay-mission", str(mission_id)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["audit"]["valid"] is True

    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE audit_events SET event_hash=:event_hash "
                "WHERE tenant_id=:tenant_id AND sequence=1"
            ),
            {"event_hash": "0" * 64, "tenant_id": str(TENANT)},
        )
    invalid = runner.invoke(cli.app, ["replay-mission", str(mission_id)])

    assert invalid.exit_code == 2
    assert json.loads(invalid.stdout)["audit"]["valid"] is False
    assert json.loads(invalid.stdout)["audit"]["first_invalid_sequence"] == 1
    store.close()


def test_timeline_accepts_a_reserved_child_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "reserved.db")
    mission_id = uuid4()
    store.create_mission(
        mission_id=mission_id,
        tenant_id=TENANT,
        query="Inspect a page",
        provider="test",
        plan=_browser_plan(),
        external_commit_authorized=False,
    )

    timeline = store.mission_timeline(TENANT, mission_id)

    assert timeline["audit"]["valid"] is True
    assert [event["kind"] for event in timeline["events"]] == ["mission.created"]
    assert timeline["steps"][0]["child_task_id"] is not None
    store.close()


def _mission_with_interleaved_child(tmp_path: Path) -> tuple[DatabaseStore, UUID]:
    store = _store(tmp_path / "timeline.db")
    mission_id = uuid4()
    store.create_mission(
        mission_id=mission_id,
        tenant_id=TENANT,
        query="secret query text",
        provider="secret-provider",
        plan=_browser_plan(),
        external_commit_authorized=False,
        external_commit_granted=False,
        commit_intent_detected=False,
        authority_reason="secret authority reason",
    )
    step = store.list_mission_steps(TENANT, mission_id)[0]
    store.claim_mission(
        tenant_id=TENANT,
        mission_id=mission_id,
        owner="test-worker",
    )
    store.start_mission_steps(
        tenant_id=TENANT,
        mission_id=mission_id,
        step_ids=(step.id,),
    )
    store.create_task(
        task_id=uuid4(),
        tenant_id=TENANT,
        instruction="unrelated secret work",
        start_url="https://unrelated.example/secret",
        provider="secret-provider",
        actions=(_finish_action(),),
    )
    assert step.child_task_id is not None
    store.create_task(
        task_id=step.child_task_id,
        tenant_id=TENANT,
        instruction="secret task instruction",
        start_url="https://target.example/secret",
        provider="secret-provider",
        actions=(_finish_action(),),
        authority_context={"private": "secret authority context"},
    )
    store.complete_mission_step(
        tenant_id=TENANT,
        mission_id=mission_id,
        step_id=step.id,
        output={"answer": "secret durable output"},
    )
    return store, mission_id


def _store(path: Path) -> DatabaseStore:
    store = DatabaseStore(f"sqlite:///{path}", metrics=None)
    store.initialize()
    return store


def _browser_plan() -> MissionPlan:
    return MissionPlan(
        summary="secret plan summary",
        steps=(
            MissionPlanStep(
                key="browser",
                kind=MissionStepKind.BROWSER,
                instruction="secret browser instruction",
            ),
        ),
    )


def _finish_action() -> ProposedAction:
    return ProposedAction(
        kind=ActionKind.FINISH,
        description="secret finish action",
    )
