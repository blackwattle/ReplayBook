"""Milestone 27: verified writes. Each rejection, the rendering, the three comparison results,
the example skills' checks against the mocks, replay refusing an invalid skill, and the
planned checks in results and the evidence pack. Offline."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook import packs
from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.checks import (
    Check,
    SkillInvalid,
    checks_for,
    compare,
    parse_checks,
    placeholders,
    render,
    skill_problems,
    validate_skill,
)
from replaybook.cli import app
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
from replaybook.eval.runner import run_replay
from replaybook.eval.testset import build_test_set
from replaybook.mock_env.registry import MockEnvironment
from replaybook.mock_env.world import TicketContext, WorldState
from replaybook.schema.ground_truth import ActionSpec
from replaybook.schema.testcase import Expectation
from replaybook.schema.testcase import TestCase as Case
from replaybook.schema.testcase import TestSet as Suite
from replaybook.skills import parse_skill
from replaybook.tools_import import example_skill

runner = CliRunner()
WIDGET_TOOLS = [
    {
        "name": "widget_get",
        "description": "Get a widget order.",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "widget_order",
        "description": "Order a widget.",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
            "required": ["sku"],
        },
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "widget_cancel",
        "description": "Cancel a widget order.",
        "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}},
        "annotations": {"readOnlyHint": False},
    },
]
GOOD_VERIFY = """verify:
  widget_order:
    tool: widget_get
    args: {sku: "{sku}"}
    expect: {sku: "{sku}", qty: "{qty}"}
"""


def _skill(front: str, tools: str = "widget_get, widget_order, ticket_comment") -> str:
    return (
        f"---\nname: widget-order\nrequest_type: widget_order\ntools: {tools}\n{front}---\n\n"
        "# Widget order\n"
    )


@pytest.fixture()
def widget_pack(tmp_path: Path) -> Iterator[Path]:
    """A pack the core has never seen, served by the recording mock, with one skill."""
    pack = tmp_path / "packs" / "widgets"
    (pack / "skills" / "widget_order").mkdir(parents=True)
    (pack / "__init__.py").write_text("", encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "name: widgets\nrequest_types:\n  widget_order:\n    label: Widget order\n"
        "    keywords: [widget]\n    tools: [widget_order]\n"
        "    skill: skills/widget_order/SKILL.md\n",
        encoding="utf-8",
    )
    (pack / "tools.json").write_text(json.dumps({"tools": WIDGET_TOOLS}), encoding="utf-8")
    (pack / "skills" / "widget_order" / "SKILL.md").write_text(
        _skill(GOOD_VERIFY), encoding="utf-8"
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(packs.ENV_VAR, str(tmp_path / "packs"))
        packs.reset()
        yield pack / "skills" / "widget_order" / "SKILL.md"
    packs.reset()


def _problems(front: str, tools: str = "widget_get, widget_order, ticket_comment") -> list[str]:
    skill = parse_skill(_skill(front, tools), Path("SKILL.md"), "widget_order")
    return validate_skill(skill)


# --- each rejection -----------------------------------------------------------------


def test_a_valid_skill_passes(widget_pack: Path) -> None:
    assert _problems(GOOD_VERIFY) == []
    assert skill_problems("widget_order") == []


def test_each_rejection(widget_pack: Path) -> None:
    cases = {
        "the write widget_order has no check": "",
        "which no loaded pack serves": GOOD_VERIFY.replace("tool: widget_get", "tool: widget_x"),
        "which is not read-only": GOOD_VERIFY.replace("tool: widget_get", "tool: widget_cancel"),
        "names {colour}, which widget_order does not take": GOOD_VERIFY.replace(
            'qty: "{qty}"', 'qty: "{colour}"'
        ),
        "passes site to widget_get, which does not take it": GOOD_VERIFY.replace(
            'args: {sku: "{sku}"}', 'args: {sku: "{sku}", site: "x"}'
        ),
        "leaves out sku, which widget_get needs": GOOD_VERIFY.replace(
            'args: {sku: "{sku}"}', "args: {}"
        ),
        "has no `expect`": GOOD_VERIFY.replace(
            'expect: {sku: "{sku}", qty: "{qty}"}', "expect: {}"
        ),
        "timeout_s must be a positive number": GOOD_VERIFY + "    timeout_s: 0\n",
        "must map each write tool to its check": "verify: [widget_order]\n",
        "must be a mapping with tool, args and expect": "verify:\n  widget_order: widget_get\n",
    }
    for needle, front in cases.items():
        problems = _problems(front)
        assert any(needle in p for p in problems), (needle, problems)
    unknown = _problems(GOOD_VERIFY, "widget_get, widget_order, widget_fly")
    assert any("`tools:` names widget_fly" in p for p in unknown)
    extra = _problems(
        GOOD_VERIFY + "  widget_cancel:\n    tool: widget_get\n    args: {sku: x}\n"
        "    expect: {sku: x}\n"
    )
    assert any("not a write in `tools:`" in p for p in extra)
    ticket = _problems(
        GOOD_VERIFY + "  ticket_comment:\n    tool: widget_get\n    args: {sku: x}\n"
        "    expect: {sku: x}\n"
    )
    assert any("ticket tool and takes no check" in p for p in ticket)


def test_replay_shadow_and_cli_refuse_an_invalid_skill(widget_pack: Path, tmp_path: Path) -> None:
    widget_pack.write_text(_skill(""), encoding="utf-8")  # the write has no check
    case = Case(
        id="W-1",
        ticket_id="W-1",
        request_type="widget_order",
        created=datetime(2026, 5, 1, tzinfo=UTC),
        summary="Order a widget",
        world=WorldState(
            ticket=TicketContext(
                id="W-1", summary="Order widget", description="sku w-1", reporter="r", created="x"
            )
        ),
        expectation=Expectation(
            kind="actions", actions=[ActionSpec(tool="widget_order", args={"sku": "w-1"})]
        ),
    )
    suite = Suite(
        name="widgets",
        request_type="widget_order",
        source="test",
        data_dir="",
        created_at=case.created,
        cases=[case],
    )
    with pytest.raises(SkillInvalid, match="widget_order has no check"):
        run_replay(suite, ScriptedAgent(), tmp_path / "run")
    path = tmp_path / "ts.json"
    path.write_text(suite.model_dump_json(), encoding="utf-8")
    refused = runner.invoke(app, ["replay", "--testset", str(path), "--out", str(tmp_path / "r")])
    assert refused.exit_code == 2 and "fails validation" in refused.output
    validated = runner.invoke(app, ["skills", "validate"])
    assert validated.exit_code == 1 and "INVALID" in validated.output
    assert "widget_order has no check" in validated.output


# --- rendering ----------------------------------------------------------------------


def test_rendering() -> None:
    values = {"cluster": "kafka-prod", "partitions": 6, "operations": ["READ"], "svc": "pay"}
    assert render("{partitions}", values) == 6, "a whole placeholder keeps the type"
    assert render("{operations}", values) == ["READ"]
    assert render("svc-{svc}-{{literal}}", values) == "svc-pay-{literal}"
    assert render({"a": ["{cluster}", {"b": "{missing}"}]}, values) == {
        "a": ["kafka-prod", {"b": None}]
    }
    assert placeholders({"a": ["{x}", "y-{z}"], "b": "{{not}}"}) == {"x", "z"}
    check = Check(
        write="kafka_add_acl",
        tool="kafka_describe_topic",
        args={"cluster": "{cluster}", "topic": "{topic}"},
        expect={"acls": [{"principal": "{principal}", "operations": "{operations}"}]},
    )
    rendered = check.render(
        {"cluster": "k", "topic": "t", "principal": "svc-a", "operations": ["READ"]}
    )
    assert rendered == {
        "tool": "kafka_describe_topic",
        "args": {"cluster": "k", "topic": "t"},
        "expect": {"acls": [{"principal": "svc-a", "operations": ["READ"]}]},
        "timeout_s": 30.0,
    }


# --- the three comparison results ---------------------------------------------------


def test_comparison_results() -> None:
    result = {
        "name": "payments.events.v1",
        "partitions": 6,
        "acls": [{"principal": "svc-a", "operations": ["DESCRIBE", "READ"]}],
        "extra": "ignored",
    }
    verified = compare(
        {"partitions": "6", "acls": [{"principal": "SVC-A ", "operations": ["read"]}]}, result
    )
    assert verified.status == "verified" and verified.differences == []
    assert compare({"partitions": 6, "retention_ms": None}, result).status == "verified"
    failed = compare({"partitions": 12, "acls": [{"principal": "svc-b"}]}, result)
    assert failed.status == "verify_failed"
    assert failed.differences == [
        "partitions: expected 12, got 6",
        'acls: no item matches {"principal": "svc-b"}',
    ]
    assert compare({"missing": 1}, result).differences == ["missing: missing"]
    errored = compare({"partitions": 6}, {"error": "topic 'x' not found"})
    assert errored.status == "verify_error" and "not found" in errored.differences[0]
    assert compare({"partitions": 6}, None).status == "verify_error"
    assert compare({"error": "gone"}, {"error": "gone"}).status == "verified"
    assert compare({"flag": "true"}, {"flag": True}).status == "verified"


def test_example_skills_are_valid_and_their_checks_confirm_the_writes() -> None:
    world = WorldState.from_inventory(
        {
            "kafka": {"cluster": "kafka-prod", "topics": []},
            "artifactory": {"repositories": [], "groups": ["lending-developers"]},
            "openstack": {"projects": [{"name": "risk-tech-test", "quotas": {"cores": 32}}]},
        },
        {},
    )
    writes = {
        "kafka_topic": [
            (
                "kafka_create_topic",
                {"cluster": "kafka-prod", "topic": "a.events.v1", "partitions": 6},
            ),
            (
                "kafka_add_acl",
                {
                    "cluster": "kafka-prod",
                    "topic": "a.events.v1",
                    "principal": "svc-a",
                    "operations": ["WRITE", "DESCRIBE"],
                },
            ),
        ],
        "artifactory_repo": [
            (
                "artifactory_create_repo",
                {"key": "x-local", "package_type": "npm", "rclass": "local"},
            ),
            (
                "artifactory_create_permission_target",
                {
                    "name": "x-local-read",
                    "repositories": ["x-local"],
                    "groups": {"lending-developers": ["read"]},
                },
            ),
        ],
        "openstack_quota": [("openstack_quota_set", {"project": "risk-tech-test", "cores": 64})],
    }
    for request_type, calls in writes.items():
        assert skill_problems(request_type) == [], request_type
        checks = checks_for(request_type)
        env = MockEnvironment(world.model_copy(deep=True))
        for tool, args in calls:
            assert "error" not in env.call(tool, args), (tool, env.call(tool, args))
            rendered = checks[tool].render(args)
            outcome = compare(rendered["expect"], env.call(rendered["tool"], rendered["args"]))
            assert outcome.status == "verified", (tool, outcome.differences)
        # A different value the write did not make is caught.
        tool, args = calls[0]
        wrong = {k: (v + 1 if isinstance(v, int) else v) for k, v in args.items()}
        rendered = checks[tool].render(wrong)
        if any(isinstance(v, int) for v in args.values()):
            assert compare(
                rendered["expect"], env.call(rendered["tool"], rendered["args"])
            ).status == ("verify_failed")


def test_tools_import_writes_placeholder_checks_that_fail() -> None:
    text = example_skill("widgets", WIDGET_TOOLS)
    skill = parse_skill(text, Path("SKILL.md"), "widget_order")
    assert set(skill.verify) == {"widget_order", "widget_cancel"}
    checks, problems = parse_checks(skill)
    assert all("has no `expect`" in p for p in problems) and len(problems) == 2
    assert checks["widget_order"].args == {"sku": "{sku}"}


def test_tools_import_skill_fails_validation(widget_pack: Path) -> None:
    skill = parse_skill(example_skill("widgets", WIDGET_TOOLS), Path("SKILL.md"), "widget_order")
    problems = validate_skill(skill)
    assert any("TODO_read_tool_that_shows_widget_order" in p for p in problems)
    assert any("has no `expect`" in p for p in problems)


# --- planned checks in results and the evidence pack --------------------------------


def test_replay_lists_planned_checks_and_never_runs_them(
    small_dataset: Path, tmp_path: Path
) -> None:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=12
    )
    run = run_replay(ts, ScriptedAgent(), tmp_path / "run", workers=2)
    planned = [p for r in run.results for p in r.planned_checks]
    assert planned, "the fixed agent writes on most cases"
    assert all(p["check"]["tool"] == "kafka_describe_topic" for p in planned if p["check"])
    for r in run.results:
        writes = [w for w in r.recorded if w["tool"] in ("kafka_create_topic", "kafka_add_acl")]
        assert len(r.planned_checks) == len(writes), r.case_id
        reads = [s["tool"] for s in r.transcript]
        # The checks were rendered, not run: the agent's transcript has no extra reads.
        assert reads.count("kafka_describe_topic") <= 1, r.case_id
    everything = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=1000
    )
    flawed = run_replay(
        everything, ScriptedAgent(flaws=["unsafe_write"]), tmp_path / "flawed", workers=3
    )
    outside = [p for r in flawed.results for p in r.planned_checks if p["check"] is None]
    assert outside and all(p["write"]["tool"] == "kafka_delete_topic" for p in outside)
    assert all("does not list" in p["note"] for p in outside)

    paths = build_evidence(
        EvidenceInputs(title="t", bundles=[RunBundle(run=run, test_set=ts)]), tmp_path / "ev"
    )
    html = paths["html"].read_text(encoding="utf-8")
    assert "Planned checks (dry run)" in html and "kafka_describe_topic" in html
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    pc = data["planned_checks"][0]
    assert pc["planned_writes"] == len(planned) and len(pc["declared"]) == 2


def test_cli_checks_render_and_compare(tmp_path: Path) -> None:
    write = json.dumps(
        {"tool": "openstack_quota_set", "args": {"project": "risk-tech-test", "cores": 64}}
    )
    rendered = runner.invoke(
        app, ["checks", "render", "--skill", "openstack_quota", "--write", write]
    )
    assert rendered.exit_code == 0, rendered.output
    check = json.loads(rendered.output)
    assert check["tool"] == "openstack_quota_show" and check["expect"]["quotas"]["cores"] == 64
    (tmp_path / "check.json").write_text(rendered.output, encoding="utf-8")
    for result, code, status in (
        ({"project": "risk-tech-test", "quotas": {"cores": 64, "ram": 1}}, 0, "verified"),
        ({"project": "risk-tech-test", "quotas": {"cores": 32}}, 1, "verify_failed"),
        ({"error": "project not found"}, 2, "verify_error"),
    ):
        (tmp_path / "read.json").write_text(json.dumps(result), encoding="utf-8")
        compared = runner.invoke(
            app,
            [
                "checks",
                "compare",
                "--check",
                str(tmp_path / "check.json"),
                "--result",
                str(tmp_path / "read.json"),
            ],
        )
        assert compared.exit_code == code and json.loads(compared.output)["status"] == status
    unreadable = runner.invoke(
        app, ["checks", "compare", "--expect", "{}", "--result", str(tmp_path / "none.json")]
    )
    assert unreadable.exit_code == 2 and "verify_error" in unreadable.output
    no_check = runner.invoke(
        app,
        [
            "checks",
            "render",
            "--skill",
            "openstack_quota",
            "--write",
            '{"tool": "openstack_server_create"}',
        ],
    )
    assert no_check.exit_code == 2
