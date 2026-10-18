"""Launchers: run an off-the-shelf agent harness as the agent under test.

A launcher is a YAML file, `launchers/<name>.yaml`, that lives outside `src/` (next to the
packs) and tells the external adapter how to run one harness:

- command        the argv template. Placeholders: {prompt}, {prompt_file}, {mcp_config},
                 {workdir}, {ticket_id}, {servers}, {servers_csv}, {disabled_tools}; the item
                 "{server_args}" expands to `per_server_args` once per mock server
- mcp_config     how the harness learns about the mock servers: `pass: file` writes the
                 config document to `filename` under the working directory and `{mcp_config}`
                 is its path; `pass: env` does the same and also puts the path in `env_var`;
                 `pass: flags` passes nothing but the `per_server_args`. The document and the
                 per-server entry are templates in the harness's own format (json or toml):
                 "{servers}" marks where the server map goes, "{command}" and "{args}" (or
                 "{argv}" for both in one list) where a server's start command goes, and
                 "{disabled_tools_list}" where the list of switched-off tools goes
- prompt         how the ticket prompt is passed: argument ({prompt} in the command), stdin,
                 file ({prompt_file}) or env (env_var); `template` overrides the default text
- disabled_tools the harness's own tools that the launcher switches off, so the agent can
                 only act through the recorded mock tools
- timeout_s, env, cwd, limitations

Launchers are found through REPLAYBOOK_LAUNCHERS (directories separated by the OS path
separator), otherwise `./launchers`, otherwise the `launchers/` directory of this checkout.
The core never names a harness; a test fails if it does. Every mock server a launcher
starts is the same recording mock an in-process agent gets, so scores are comparable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from replaybook.agents.base import AgentRunResult
from replaybook.agents.external import (
    CaseFiles,
    case_files,
    collect_calls,
    run_command,
    transcript_of,
)
from replaybook.mock_env.registry import (
    ASK_TOOL,
    COMMENT_TOOL,
    ESCALATE_TOOL,
    GET_TOOL,
    MockEnvironment,
)
from replaybook.packs import registry

ENV_VAR = "REPLAYBOOK_LAUNCHERS"
REPO_LAUNCHERS = Path(__file__).resolve().parents[3] / "launchers"
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
SERVER_ARGS = "{server_args}"

DEFAULT_PROMPT = f"""You are an operations agent working one support ticket in a mock environment.
Act only through the MCP tools you have been given (servers: {{servers_csv}}). Do not run
commands, edit files or use any other tool.

Rules:
- Start by calling {GET_TOOL} to read the ticket.
- Use read tools to check the current state before you change anything.
- If the ticket lacks information you need, call {ASK_TOOL} once with a clear
  question and stop. Never guess.
- If the ticket needs a decision a person must make (an approval that is still needed,
  a possible clash with another team's work, or more than the skill covers), call
  {ESCALATE_TOOL} once with the reason and stop without changing anything.
- Never delete or recreate existing resources unless the skill explicitly says to.
- When finished, call {COMMENT_TOOL} with a one-line summary and stop.
{{skill_section}}
Ticket {{ticket_id}}: {{summary}}
"""


class LauncherError(Exception):
    """A launcher file is malformed or names something that does not exist."""


class McpConfigSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pass_as: Literal["file", "env", "flags"] = Field(default="file", alias="pass")
    format: Literal["json", "toml"] = "json"
    filename: str = "mcp.json"
    env_var: str | None = None
    document: dict[str, Any] = Field(default_factory=lambda: {"mcpServers": "{servers}"})
    server_entry: dict[str, Any] = Field(
        default_factory=lambda: {"command": "{command}", "args": "{args}"}
    )
    per_server_args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> McpConfigSpec:
        if self.pass_as == "env" and not self.env_var:
            raise ValueError("mcp_config.pass is env, so mcp_config.env_var is required")
        if self.pass_as == "flags" and not self.per_server_args:
            raise ValueError("mcp_config.pass is flags, so mcp_config.per_server_args is required")
        if self.pass_as != "flags" and not _mentions(self.document, "{servers}"):
            raise ValueError('mcp_config.document must contain "{servers}" somewhere')
        return self


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pass_as: Literal["argument", "stdin", "file", "env"] = Field(default="argument", alias="pass")
    env_var: str | None = None
    filename: str = "prompt.md"
    template: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PromptSpec:
        if self.pass_as == "env" and not self.env_var:
            raise ValueError("prompt.pass is env, so prompt.env_var is required")
        return self


class Launcher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    docs: str = ""
    checked: str = ""
    command: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    mcp_config: McpConfigSpec = Field(default_factory=McpConfigSpec)
    prompt: PromptSpec = Field(default_factory=PromptSpec)
    disabled_tools: list[str] = Field(default_factory=list)
    limitations: str = ""
    timeout_s: float = 600.0
    servers_pattern: str = "{server}"
    servers_join: str = ","
    path: Path | None = Field(default=None, exclude=True)
    sha256: str = Field(default="", exclude=True)

    @model_validator(mode="after")
    def _check(self) -> Launcher:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.name):
            raise ValueError(
                f"launcher name '{self.name}' must be lowercase letters, digits, _ or -"
            )
        joined = "\n".join(self.command)
        if self.prompt.pass_as == "argument" and "{prompt}" not in joined:
            raise ValueError("prompt.pass is argument but {prompt} is not in the command")
        if self.prompt.pass_as == "file" and "{prompt_file}" not in joined + "\n".join(
            self.env.values()
        ):
            raise ValueError("prompt.pass is file but {prompt_file} is used nowhere")
        if self.mcp_config.per_server_args and SERVER_ARGS not in self.command:
            raise ValueError(f'per_server_args is set but "{SERVER_ARGS}" is not in the command')
        return self

    @classmethod
    def load(cls, path: Path) -> Launcher:
        path = Path(path)
        raw = path.read_bytes()
        try:
            data = yaml.safe_load(raw.decode("utf-8")) or {}
            launcher = cls.model_validate(data)
        except (yaml.YAMLError, ValueError) as exc:
            raise LauncherError(f"{path}: {exc}") from exc
        launcher.path = path
        launcher.sha256 = hashlib.sha256(raw).hexdigest()
        return launcher

    @property
    def executable(self) -> str:
        return self.command[0]


def _mentions(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return value == marker
    if isinstance(value, dict):
        return any(_mentions(v, marker) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, marker) for v in value)
    return False


# --- rendering -------------------------------------------------------------------------


def render(text: str, values: dict[str, Any]) -> str:
    """Replace {name} for the names in `values`; anything else is left as it is."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return PLACEHOLDER.sub(replace, text)


def render_structure(value: Any, values: dict[str, Any], whole: dict[str, Any]) -> Any:
    """Render a config document: strings that are exactly a key of `whole` become that
    value (lists and maps included); other strings get placeholder substitution."""
    if isinstance(value, str):
        if value in whole:
            return whole[value]
        return render(value, values)
    if isinstance(value, dict):
        return {k: render_structure(v, values, whole) for k, v in value.items()}
    if isinstance(value, list):
        return [render_structure(v, values, whole) for v in value]
    return value


def render_document(
    spec: McpConfigSpec,
    servers: dict[str, dict[str, Any]],
    values: dict[str, Any],
    disabled_tools: list[str],
) -> dict[str, Any]:
    entries: dict[str, Any] = {}
    for name, start in servers.items():
        argv = [start["command"], *start["args"]]
        whole = {
            "{command}": start["command"],
            "{args}": list(start["args"]),
            "{argv}": argv,
            "{server}": name,
        }
        entries[name] = render_structure(spec.server_entry, {**values, "server": name}, whole)
    whole_doc = {"{servers}": entries, "{disabled_tools_list}": list(disabled_tools)}
    return render_structure(spec.document, values, whole_doc)


def to_toml(document: dict[str, Any]) -> str:
    """A small TOML writer: scalars, lists of scalars and nested tables. Enough for a
    harness config file; anything else is a launcher error."""
    lines: list[str] = []

    def key(name: str) -> str:
        return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)

    def scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return repr(value)
        if isinstance(value, str):
            return json.dumps(value)
        if isinstance(value, list):
            if any(isinstance(v, dict | list) for v in value):
                raise LauncherError("toml config: lists of tables are not supported")
            return "[" + ", ".join(scalar(v) for v in value) + "]"
        raise LauncherError(f"toml config: cannot write a value of type {type(value).__name__}")

    def table(prefix: list[str], data: dict[str, Any]) -> None:
        plain = {k: v for k, v in data.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in data.items() if isinstance(v, dict)}
        if prefix and (plain or not nested):
            lines.append("[" + ".".join(key(p) for p in prefix) + "]")
        for k, v in plain.items():
            lines.append(f"{key(k)} = {scalar(v)}")
        if plain and prefix:
            lines.append("")
        for k, v in nested.items():
            table([*prefix, k], v)

    table([], document)
    return "\n".join(lines).rstrip() + "\n"


def serialise(document: dict[str, Any], fmt: str) -> str:
    if fmt == "toml":
        return to_toml(document)
    return json.dumps(document, indent=2) + "\n"


def per_server_args(spec: McpConfigSpec, servers: dict[str, dict[str, Any]]) -> list[str]:
    args: list[str] = []
    for name, start in servers.items():
        values = {
            "server": name,
            "command": start["command"],
            "command_json": json.dumps(start["command"]),
            "args_json": json.dumps(list(start["args"])),
            "argv_json": json.dumps([start["command"], *start["args"]]),
        }
        args.extend(render(item, values) for item in spec.per_server_args)
    return args


class Invocation(BaseModel):
    """Everything about one run of the harness, for the dry run and the tests."""

    command: list[str]
    cwd: str
    env: dict[str, str]
    stdin: str | None = None
    prompt: str
    config_path: str | None = None
    config_text: str | None = None


def prompt_values(env: MockEnvironment, request_type: str | None, skill: str) -> dict[str, Any]:
    ticket = env.world.ticket
    servers = sorted(env.servers)
    return {
        "ticket_id": ticket.id if ticket else "",
        "summary": ticket.summary if ticket else "",
        "description": ticket.description if ticket else "",
        "ticket_json": json.dumps(ticket.model_dump() if ticket else {}, indent=2),
        "request_type": request_type or "",
        "skill": skill,
        "skill_section": f"\n# Skill\n\n{skill}\n" if skill else "",
        "servers_csv": ",".join(servers),
    }


def build_invocation(
    launcher: Launcher,
    env: MockEnvironment,
    files: CaseFiles,
    request_type: str | None = None,
    skill: str = "",
) -> Invocation:
    """Fill the launcher's templates for one case. Writes the harness config file and the
    prompt file into the working directory when the launcher wants them."""
    servers = files.mcp_servers()
    values = prompt_values(env, request_type, skill)
    prompt = render(launcher.prompt.template or DEFAULT_PROMPT, values)
    values.update(
        {
            "workdir": str(files.workdir),
            "servers": launcher.servers_join.join(
                render(launcher.servers_pattern, {"server": name}) for name in servers
            ),
            "disabled_tools": ",".join(launcher.disabled_tools),
            "prompt": prompt,
        }
    )
    config_path: Path | None = None
    config_text: str | None = None
    if launcher.mcp_config.pass_as in ("file", "env"):
        config_path = files.workdir / render(launcher.mcp_config.filename, values)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        document = render_document(launcher.mcp_config, servers, values, launcher.disabled_tools)
        config_text = serialise(document, launcher.mcp_config.format)
        config_path.write_text(config_text, encoding="utf-8")
        values["mcp_config"] = str(config_path)
    prompt_file: Path | None = None
    if launcher.prompt.pass_as == "file":
        prompt_file = files.workdir / render(launcher.prompt.filename, values)
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")
        values["prompt_file"] = str(prompt_file)

    command: list[str] = []
    for item in launcher.command:
        if item == SERVER_ARGS:
            command.extend(per_server_args(launcher.mcp_config, servers))
        else:
            command.append(render(item, values))

    child_env = files.environment()
    for name, value in launcher.env.items():
        child_env[name] = render(value, values)
    if launcher.mcp_config.pass_as == "env" and launcher.mcp_config.env_var:
        child_env[launcher.mcp_config.env_var] = str(config_path)
    if launcher.prompt.pass_as == "env" and launcher.prompt.env_var:
        child_env[launcher.prompt.env_var] = prompt
    cwd = render(launcher.cwd, values) if launcher.cwd else str(files.workdir)
    return Invocation(
        command=command,
        cwd=cwd,
        env=child_env,
        stdin=prompt if launcher.prompt.pass_as == "stdin" else None,
        prompt=prompt,
        config_path=str(config_path) if config_path else None,
        config_text=config_text,
    )


# --- the agent -------------------------------------------------------------------------


class LauncherAgent:
    """The agent under test is whatever the launcher starts."""

    name = "launcher"

    def __init__(
        self,
        launcher: Launcher,
        request_type: str | None = None,
        executable: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.launcher = launcher
        self.request_type = request_type
        # Replaces the harness executable (a wrapper script, another install, the fake
        # command in the tests); may be several argv items.
        self.executable = list(executable) if executable else None
        self.timeout_s = timeout_s if timeout_s is not None else launcher.timeout_s
        self.skill = ""
        if request_type:
            path = registry().skill_path(request_type)
            if path is not None:
                self.skill = path.read_text(encoding="utf-8")

    @property
    def command_template(self) -> list[str]:
        command = list(self.launcher.command)
        if self.executable:
            command[:1] = self.executable
        return command

    @property
    def config(self) -> dict[str, Any]:
        template = self.launcher.prompt.template or DEFAULT_PROMPT
        return {
            "agent": self.name,
            "version": 1,
            "launcher": self.launcher.name,
            "launcher_sha256": self.launcher.sha256,
            "command": self.command_template,
            "request_type": self.request_type,
            "skill_sha256": hashlib.sha256(self.skill.encode("utf-8")).hexdigest()
            if self.skill
            else None,
            "prompt_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
            "disabled_tools": list(self.launcher.disabled_tools),
            "timeout_s": self.timeout_s,
            "model": "chosen by the harness (not visible to Replaybook)",
        }

    def invocation(self, env: MockEnvironment, files: CaseFiles) -> Invocation:
        launcher = self.launcher
        if self.executable:
            launcher = launcher.model_copy(update={"command": self.command_template})
        return build_invocation(launcher, env, files, self.request_type, self.skill)

    def run(self, env: MockEnvironment) -> AgentRunResult:
        with case_files(env) as files:
            invocation = self.invocation(env, files)
            stdout, error = run_command(
                invocation.command,
                invocation.env,
                self.timeout_s,
                cwd=Path(invocation.cwd),
                stdin=invocation.stdin,
            )
            calls = collect_calls(env, files)
        return AgentRunResult(
            transcript=transcript_of(calls),
            final_message=stdout.strip()[-2000:],
            stopped_reason="error" if error else "done",
            error=error,
        )


# --- discovery -------------------------------------------------------------------------


class LauncherRegistry:
    def __init__(self, launchers: list[Launcher], search_path: list[Path]) -> None:
        self.launchers = sorted(launchers, key=lambda item: item.name)
        self.search_path = search_path
        self.by_name = {item.name: item for item in self.launchers}
        if len(self.by_name) != len(self.launchers):
            names = [item.name for item in self.launchers]
            raise LauncherError(
                f"duplicate launcher names: {sorted(n for n in names if names.count(n) > 1)}"
            )

    def launcher(self, name: str) -> Launcher:
        try:
            return self.by_name[name]
        except KeyError as exc:
            raise LauncherError(
                f"no launcher '{name}'. Available: {', '.join(self.by_name) or 'none'}"
            ) from exc

    def __contains__(self, name: object) -> bool:
        return name in self.by_name


def search_path() -> list[Path]:
    configured = os.environ.get(ENV_VAR, "").strip()
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    local = Path.cwd() / "launchers"
    if local.is_dir() and any(local.glob("*.yaml")):
        return [local]
    return [REPO_LAUNCHERS]


def load_launchers(directories: list[Path] | None = None) -> LauncherRegistry:
    dirs = list(directories) if directories is not None else search_path()
    found: list[Launcher] = []
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            launcher = Launcher.load(path)
            if launcher.name != path.stem:
                raise LauncherError(f"{path}: name '{launcher.name}' must match the file name")
            found.append(launcher)
    return LauncherRegistry(found, dirs)


_registry: LauncherRegistry | None = None
_lock = threading.Lock()


def launchers() -> LauncherRegistry:
    """The launchers on the search path, loaded once per process (see `reset`)."""
    global _registry
    with _lock:
        if _registry is None:
            _registry = load_launchers()
        return _registry


def reset() -> None:
    global _registry
    with _lock:
        _registry = None
