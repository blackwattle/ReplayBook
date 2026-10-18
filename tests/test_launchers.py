"""Launchers: an off-the-shelf agent harness as the agent under test, described by a YAML
file outside the core. Every shipped launcher is exercised against a fake command that
reads the launcher's output the way the real harness would, and the core names no harness."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents import launchers as mod
from replaybook.agents.launchers import (
    DEFAULT_PROMPT,
    Launcher,
    LauncherAgent,
    LauncherError,
    load_launchers,
    render,
    to_toml,
)
from replaybook.classify import classify
from replaybook.cli import app
from replaybook.eval.runner import load_run
from replaybook.eval.shadow import load_proposals, open_tickets
from replaybook.eval.testset import build_test_set
from replaybook.mock_env.registry import ASK_TOOL, GET_TOOL, MockEnvironment
from replaybook.mock_env.world import WorldState
from replaybook.schema.ticket import Ticket

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "replaybook"
LAUNCHERS = ROOT / "launchers"
FAKE = Path(__file__).resolve().parent / "fake_harness.py"
FAKE_COMMAND = [sys.executable, str(FAKE)]
SHIPPED = {"claude-code", "codex", "gemini-cli", "opencode"}
# Names of agent harnesses. They belong in launcher files, never in the core.
HARNESS_WORDS = (
    r"\bclaude\b",
    r"\bcodex\b",
    r"gemini[-_ ]cli",
    r"\bcopilot\b",
    r"\bopencode\b",
    r"\bgoose\b",
    r"\baider\b",
    r"\bcline\b",
    r"\bwindsurf\b",
)
# How the fake harness finds the MCP servers for each shipped launcher: the same way the
# real harness would (a flag, a file in the working directory, an environment variable,
# or repeated -c flags).
FAKE_READS = {
    "claude-code": {"FAKE_HARNESS_CONFIG": "arg:--mcp-config"},
    "codex": {"FAKE_HARNESS_CONFIG": "flags"},
    "gemini-cli": {"FAKE_HARNESS_CONFIG": "file:.gemini/settings.json"},
    "opencode": {"FAKE_HARNESS_CONFIG": "env:OPENCODE_CONFIG", "FAKE_HARNESS_SERVERS_KEY": "mcp"},
}
runner = CliRunner()


def _kafka_ticket(data_dir: Path) -> Ticket:
    return next(
        t
        for t in load_tickets(data_dir)
        if classify(t)[0] == "kafka_topic"
        and t.actions
        and not any(c.to_status == "Waiting on Requester" for c in t.status_changes)
    )


def _env_for(ticket: Ticket, data_dir: Path) -> MockEnvironment:
    return MockEnvironment(WorldState.from_ticket(ticket, load_platform(data_dir)))


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_shipped_launchers_load_and_say_what_they_switch_off() -> None:
    reg = load_launchers([LAUNCHERS])
    assert set(reg.by_name) == SHIPPED
    for launcher in reg.launchers:
        assert launcher.path is not None and launcher.path.stem == launcher.name
        assert launcher.description and launcher.docs.startswith("https://") and launcher.checked
        assert launcher.disabled_tools, f"{launcher.name} must say which built-in tools are off"
        assert launcher.limitations, f"{launcher.name} must say what it cannot switch off"
        assert launcher.timeout_s > 0 and launcher.sha256
        assert launcher.executable not in ("python", sys.executable)


def test_core_names_no_harness() -> None:
    pattern = re.compile("|".join(HARNESS_WORDS), re.IGNORECASE)
    offenders = []
    for path in SRC.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("name", sorted(SHIPPED))
def test_each_shipped_launcher_drives_the_fake_harness(
    name: str, small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = load_launchers([LAUNCHERS]).launcher(name)
    ticket = _kafka_ticket(small_dataset)
    env = _env_for(ticket, small_dataset)
    dump_path = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_HARNESS_DUMP", str(dump_path))
    for key, value in FAKE_READS[name].items():
        monkeypatch.setenv(key, value)
    agent = LauncherAgent(launcher, "kafka_topic", executable=FAKE_COMMAND, timeout_s=120)
    result = agent.run(env)
    assert result.error is None, (result.error, result.final_message)
    assert result.stopped_reason == "done" and "fake harness done" in result.final_message
    writes = [w.tool for w in env.recorder.writes]
    assert writes == ["kafka_create_topic", "ticket_comment"], writes
    assert env.recorder.reads[0].tool == GET_TOOL
    assert [t["tool"] for t in result.transcript] == [GET_TOOL, *writes]

    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    argv: list[str] = dump["argv"]
    assert dump["outcome"]["ticket_id"] == ticket.id, "the fake read the ticket through the mock"
    assert set(dump["servers"]) == set(env.servers), "every mock server reached the harness"
    assert all(cmd[0] == sys.executable for cmd in dump["servers"].values())
    everything = "\n".join(argv) + dump["stdin"] + "\n".join(dump["env"].values())
    assert ticket.id in everything and ticket.summary in everything, "the prompt names the ticket"
    assert "KRaft" in everything, "the pack's sample skill is part of the prompt"
    assert dump["cwd"].endswith("work"), "the harness ran in an empty working directory"
    assert "REPLAYBOOK_MCP_CONFIG" in dump["env"], "the plain external contract still holds"

    if name == "claude-code":
        assert "--strict-mcp-config" in argv and "--restricted" in argv
        assert _after(argv, "--permission-prompts") == "none"
        assert _after(argv, "--tools") == "", "every built-in tool is off"
        allowed = _after(argv, "--allowedTools").split(",")
        assert allowed == [f"mcp__{s}" for s in sorted(env.servers)]
        assert "Bash" in _after(argv, "--disallowedTools").split(",")
        assert _after(argv, "--mcp-config").endswith("mcp.json")
        assert json.loads(dump["config_text"])["mcpServers"]["ticket"]["args"][:2] == [
            "-m",
            "replaybook.cli",
        ]
    elif name == "codex":
        assert argv[:2] == ["exec", "--skip-git-repo-check"]
        assert "--ignore-user-config" in argv and "--ephemeral" in argv
        assert _after(argv, "--sandbox") == "read-only"
        assert 'approval_policy="never"' in argv
        assert sum(a.startswith("mcp_servers.") for a in argv) == 2 * len(env.servers)
        assert argv[-1].startswith("You are an operations agent"), "the prompt is the last argument"
    elif name == "gemini-cli":
        settings = json.loads(dump["config_text"])
        assert "run_shell_command" in settings["tools"]["exclude"]
        assert all(entry["trust"] is True for entry in settings["mcpServers"].values())
        assert argv.count("--allowed-mcp-server-names") == len(env.servers)
        assert "--yolo" in argv and _after(argv, "--prompt").startswith("You are an operations")
    elif name == "opencode":
        assert dump["env"]["OPENCODE_CONFIG"].endswith("opencode.json")
        config = json.loads(dump["config_text"])
        assert config["tools"]["bash"] is False and config["permission"]["bash"] == "deny"
        assert config["mcp"]["ticket"]["type"] == "local"
        assert config["mcp"]["ticket"]["command"][0] == sys.executable

    config = agent.config
    assert config["agent"] == "launcher" and config["launcher"] == name
    assert config["launcher_sha256"] == launcher.sha256 and config["skill_sha256"]
    assert config["disabled_tools"] == launcher.disabled_tools
    assert config["command"][: len(FAKE_COMMAND)] == FAKE_COMMAND


def _write_launcher(directory: Path, name: str, **overrides: object) -> Path:
    spec: dict[str, object] = {
        "name": name,
        "description": "test launcher",
        "command": ["fake-harness", "--mcp-config", "{mcp_config}", "{prompt}"],
        "disabled_tools": ["shell"],
        "timeout_s": 60,
    }
    spec.update(overrides)
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def test_prompt_can_go_through_stdin_a_file_or_the_environment(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _kafka_ticket(small_dataset)
    dump_path = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_HARNESS_DUMP", str(dump_path))
    monkeypatch.setenv("FAKE_HARNESS_CONFIG", "arg:--mcp-config")

    monkeypatch.setenv("FAKE_HARNESS_READ_STDIN", "1")
    stdin_launcher = Launcher.load(
        _write_launcher(
            tmp_path,
            "stdin-harness",
            command=["fake-harness", "--mcp-config", "{mcp_config}"],
            prompt={"pass": "stdin", "template": "Work ticket {ticket_id}: {summary}"},
        )
    )
    env = _env_for(ticket, small_dataset)
    result = LauncherAgent(stdin_launcher, executable=FAKE_COMMAND).run(env)
    assert result.error is None, result.error
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    assert dump["stdin"] == f"Work ticket {ticket.id}: {ticket.summary}"
    assert "{ticket_id}" not in dump["stdin"]
    monkeypatch.delenv("FAKE_HARNESS_READ_STDIN")

    monkeypatch.setenv("FAKE_HARNESS_PROMPT_FILE_FLAG", "--prompt-file")
    file_launcher = Launcher.load(
        _write_launcher(
            tmp_path,
            "file-harness",
            command=[
                "fake-harness",
                "--mcp-config",
                "{mcp_config}",
                "--prompt-file",
                "{prompt_file}",
            ],
            prompt={"pass": "file", "filename": "task/prompt.md"},
        )
    )
    env = _env_for(ticket, small_dataset)
    assert LauncherAgent(file_launcher, executable=FAKE_COMMAND).run(env).error is None
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    assert ticket.id in dump["prompt_file_text"]
    assert _after(dump["argv"], "--prompt-file").replace("\\", "/").endswith("work/task/prompt.md")
    monkeypatch.delenv("FAKE_HARNESS_PROMPT_FILE_FLAG")

    env_launcher = Launcher.load(
        _write_launcher(
            tmp_path,
            "env-harness",
            command=["fake-harness", "--mcp-config", "{mcp_config}"],
            prompt={"pass": "env", "env_var": "FAKE_TASK"},
            env={"FAKE_EXTRA": "servers={servers_csv}"},
        )
    )
    env = _env_for(ticket, small_dataset)
    assert LauncherAgent(env_launcher, executable=FAKE_COMMAND).run(env).error is None
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    assert dump["env"]["FAKE_TASK"].startswith("You are an operations agent")
    assert dump["env"]["FAKE_EXTRA"] == "servers=" + ",".join(sorted(env.servers))


def test_toml_config_and_the_flags_mode_render_the_servers(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {
        "approval_policy": "never",
        "mcp_servers": {"ticket": {"command": "py", "args": ["-m", "x"], "env": {"A": "1"}}},
        "tools": {"web_search": False, "limit": 3},
    }
    text = to_toml(document)
    assert tomllib.loads(text) == document
    assert '[mcp_servers.ticket]\ncommand = "py"\nargs = ["-m", "x"]' in text
    with pytest.raises(LauncherError):
        to_toml({"bad": [{"a": 1}]})

    ticket = _kafka_ticket(small_dataset)
    dump_path = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_HARNESS_DUMP", str(dump_path))
    monkeypatch.setenv("FAKE_HARNESS_CONFIG", "arg:--config")
    toml_launcher = Launcher.load(
        _write_launcher(
            tmp_path,
            "toml-harness",
            command=["fake-harness", "--config", "{mcp_config}", "{prompt}"],
            mcp_config={
                "pass": "file",
                "format": "toml",
                "filename": "harness.toml",
                "document": {"servers": "{servers}", "trusted": "{disabled_tools_list}"},
                "server_entry": {"command": "{command}", "args": "{args}", "name": "{server}"},
            },
        )
    )
    monkeypatch.setenv("FAKE_HARNESS_SERVERS_KEY", "servers")
    env = _env_for(ticket, small_dataset)
    result = LauncherAgent(toml_launcher, executable=FAKE_COMMAND).run(env)
    assert result.error is None, result.error
    dump = json.loads(dump_path.read_text(encoding="utf-8"))
    parsed = tomllib.loads(dump["config_text"])
    assert parsed["servers"]["ticket"]["name"] == "ticket" and parsed["trusted"] == ["shell"]
    assert [w.tool for w in env.recorder.writes] == ["kafka_create_topic", "ticket_comment"]
    monkeypatch.delenv("FAKE_HARNESS_SERVERS_KEY")


def test_failures_are_reported_not_hidden(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _kafka_ticket(small_dataset)
    monkeypatch.setenv("FAKE_HARNESS_CONFIG", "arg:--mcp-config")
    launcher = Launcher.load(_write_launcher(tmp_path, "plain-harness"))

    monkeypatch.setenv("FAKE_HARNESS_EXIT", "3")
    env = _env_for(ticket, small_dataset)
    result = LauncherAgent(launcher, executable=FAKE_COMMAND).run(env)
    assert result.stopped_reason == "error" and "exit code 3" in (result.error or "")
    assert [w.tool for w in env.recorder.writes][0] == "kafka_create_topic", (
        "calls made before the crash are still recorded"
    )
    monkeypatch.delenv("FAKE_HARNESS_EXIT")

    slow = Launcher.load(
        _write_launcher(
            tmp_path,
            "slow-harness",
            command=["python", "-c", "import time; time.sleep(30)", "{prompt}"],
            timeout_s=1,
        )
    )
    env = _env_for(ticket, small_dataset)
    result = LauncherAgent(slow, executable=[sys.executable]).run(env)
    assert "timed out" in (result.error or "")

    env = _env_for(ticket, small_dataset)
    result = LauncherAgent(launcher, executable=["no-such-harness-xyz"]).run(env)
    assert "could not start" in (result.error or "")
    assert not env.recorder.writes


def test_discovery_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_launcher(tmp_path, "good")
    monkeypatch.setenv(mod.ENV_VAR, str(tmp_path))
    mod.reset()
    try:
        reg = mod.launchers()
        assert [launcher.name for launcher in reg.launchers] == ["good"]
        assert reg.search_path == [tmp_path] and "good" in reg
        with pytest.raises(LauncherError, match="no launcher 'nope'"):
            reg.launcher("nope")
    finally:
        monkeypatch.delenv(mod.ENV_VAR)
        mod.reset()
    assert set(mod.launchers().by_name) == SHIPPED

    (tmp_path / "renamed.yaml").write_text(
        yaml.safe_dump({"name": "other", "command": ["x", "{prompt}"]}), encoding="utf-8"
    )
    with pytest.raises(LauncherError, match="must match the file name"):
        load_launchers([tmp_path])
    (tmp_path / "renamed.yaml").unlink()

    bad = tmp_path / "bad"
    bad.mkdir()
    cases = {
        "no-prompt": {"command": ["x"]},
        "env-needs-var": {"command": ["x", "{prompt}"], "mcp_config": {"pass": "env"}},
        "flags-need-args": {"command": ["x", "{prompt}"], "mcp_config": {"pass": "flags"}},
        "no-servers": {"command": ["x", "{prompt}"], "mcp_config": {"document": {"a": 1}}},
        "typo": {"command": ["x", "{prompt}"], "timeout": 3},
        "Bad Name": {"command": ["x", "{prompt}"]},
    }
    for name, spec in cases.items():
        path = bad / "case.yaml"
        path.write_text(yaml.safe_dump({"name": name, **spec}), encoding="utf-8")
        with pytest.raises(LauncherError):
            Launcher.load(path)
    (bad / "case.yaml").write_text("command: [x, '{prompt}'\n", encoding="utf-8")
    with pytest.raises(LauncherError):
        Launcher.load(bad / "case.yaml")


def test_render_leaves_unknown_braces_alone() -> None:
    assert render("a {x} {y} {z}", {"x": 1, "y": "two"}) == "a 1 two {z}"
    assert render('{"json": true}', {}) == '{"json": true}'
    assert "{ticket_id}" in DEFAULT_PROMPT and "{skill_section}" in DEFAULT_PROMPT


def test_cli_replay_shadow_and_launchers_commands(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launchers_dir = tmp_path / "launchers"
    launchers_dir.mkdir()
    shutil.copy(LAUNCHERS / "claude-code.yaml", launchers_dir / "claude-code.yaml")
    monkeypatch.setenv(mod.ENV_VAR, str(launchers_dir))
    monkeypatch.setenv("FAKE_HARNESS_CONFIG", "arg:--mcp-config")
    mod.reset()
    try:
        listed = runner.invoke(app, ["launchers"])
        assert listed.exit_code == 0, listed.output
        assert "claude-code:" in listed.output and "17 built-in tools switched off" in listed.output

        tickets = load_tickets(small_dataset)
        platform = load_platform(small_dataset)
        ts = build_test_set(tickets, platform, "kafka_topic", n=3, data_dir=str(small_dataset))
        ts_path = tmp_path / "ts.json"
        ts_path.write_text(ts.model_dump_json(), encoding="utf-8")

        shown = runner.invoke(app, ["launchers", "show", "claude-code", "--testset", str(ts_path)])
        assert shown.exit_code == 0, shown.output
        assert "'--strict-mcp-config'" in shown.output and ts.cases[0].ticket_id in shown.output
        assert "config file" in shown.output and '"mcpServers"' in shown.output
        assert "nothing was started" in shown.output
        missing = runner.invoke(app, ["launchers", "show", "nope"])
        assert missing.exit_code == 2 and "no launcher 'nope'" in missing.output

        fake = " ".join(f'"{part}"' for part in FAKE_COMMAND)
        replayed = runner.invoke(
            app,
            [
                "replay",
                "--testset",
                str(ts_path),
                "--out",
                str(tmp_path / "run"),
                "--agent",
                "claude-code",
                "--command",
                fake,
                "--workers",
                "2",
                "--accepted-file",
                str(tmp_path / "acc.yaml"),
            ],
        )
        assert replayed.exit_code in (0, 1), replayed.output
        run = load_run(tmp_path / "run")
        assert run.agent_config["launcher"] == "claude-code"
        assert run.summary.total == 3 and run.summary.errors == 0, replayed.output
        assert all(r.transcript and r.transcript[0]["tool"] == GET_TOOL for r in run.results)
        assert "claude-code" in replayed.output or "launcher" in replayed.output

        unknown = runner.invoke(
            app,
            ["replay", "--testset", str(ts_path), "--out", str(tmp_path / "r2"), "--agent", "nope"],
        )
        assert unknown.exit_code == 2 and "no launcher 'nope'" in unknown.output

        if open_tickets(tickets, "kafka_topic"):
            recorded = runner.invoke(
                app,
                [
                    "shadow",
                    "record",
                    "--type",
                    "kafka_topic",
                    "--data",
                    str(small_dataset),
                    "--out",
                    str(tmp_path / "shadow"),
                    "--agent",
                    "claude-code",
                    "--command",
                    fake,
                ],
            )
            assert recorded.exit_code == 0, recorded.output
            proposals = load_proposals(tmp_path / "shadow")
            assert proposals and proposals[0].agent_config["launcher"] == "claude-code"
            assert all(p.stopped_reason == "done" for p in proposals)
            assert ASK_TOOL not in {w.tool for p in proposals for w in p.writes}
    finally:
        monkeypatch.delenv(mod.ENV_VAR)
        mod.reset()
