"""Air-gapped mode refuses every connection except the allowlisted endpoint, and Replaybook
has no telemetry: the only network-capable code is the model client, and the guard covers
it. All of this runs on the loopback interface; no packet leaves the machine."""

from __future__ import annotations

import ast
import select
import socket
import sys
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook import netguard
from replaybook.cli import app
from replaybook.llm.client import PROVIDER_KEYS, LLMClient, LLMConfig, ollama_available

SRC = Path(__file__).resolve().parents[1] / "src" / "replaybook"
NETWORK_MODULES = {
    "psycopg",
    "socket",
    "ssl",
    "urllib.request",
    "http.client",
    "requests",
    "httpx",
    "aiohttp",
    "websockets",
    "litellm",
    "smtplib",
    "ftplib",
    "xmlrpc",
}
# The model client probes Ollama and calls LiteLLM; the guard patches the socket module;
# the Jira Cloud adapter is an explicit export to a site the user configures (and the
# guard still governs it).
MAY_USE_THE_NETWORK = {
    SRC / "llm" / "client.py",
    SRC / "netguard.py",
    SRC / "adapters" / "jira_cloud.py",
    SRC / "adapters" / "postgres.py",
    SRC / "adapters" / "servicenow.py",
}


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    yield server
    server.close()


@pytest.fixture
def guard() -> Iterator[None]:
    yield
    netguard.uninstall()


def _got_connection(server: socket.socket, wait: float = 0.25) -> bool:
    ready, _, _ = select.select([server], [], [], wait)
    if not ready:
        return False
    conn, _ = server.accept()
    conn.close()
    return True


def test_refuses_loopback_until_the_port_is_allowlisted(
    listener: socket.socket, guard: None
) -> None:
    port = listener.getsockname()[1]
    netguard.install()
    assert netguard.is_installed()
    with pytest.raises(netguard.AirgapViolation):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    assert not _got_connection(listener), "a refused connection must never reach the listener"

    netguard.install(f"127.0.0.1:{port}")
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        assert _got_connection(listener)
    other_port = port - 1 if port > 1024 else port + 1
    with pytest.raises(netguard.AirgapViolation):
        socket.create_connection(("127.0.0.1", other_port), timeout=1)

    status = netguard.status()
    assert status["air_gapped"] is True
    assert status["allowed_hosts"] == [f"127.0.0.1:{port}"]
    assert status["refused_connections"] >= 2
    assert any("127.0.0.1" in item for item in status["refused"])


def test_refuses_name_lookups_and_connects_by_name(guard: None) -> None:
    netguard.install()
    with pytest.raises(netguard.AirgapViolation):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(netguard.AirgapViolation):
        socket.gethostbyname("example.com")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(netguard.AirgapViolation):
            sock.connect(("example.com", 80))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(netguard.AirgapViolation):
            sock.sendto(b"x", ("192.0.2.1", 53))
    assert netguard.status()["refused_connections"] == 4


def test_allowlisted_name_resolves_and_its_addresses_connect(
    listener: socket.socket, guard: None
) -> None:
    port = listener.getsockname()[1]
    netguard.install(f"localhost:{port}")
    infos = socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
    assert infos
    with socket.create_connection(("localhost", port), timeout=1):
        assert _got_connection(listener)
    # The same name on another port is still refused.
    with pytest.raises(netguard.AirgapViolation):
        socket.getaddrinfo("localhost", port + 1 if port < 65535 else port - 1)


def test_unix_sockets_are_not_outbound(guard: None, tmp_path: Path) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no AF_UNIX on this platform")
    netguard.install()
    path = str(tmp_path / "s.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        with pytest.raises(OSError) as excinfo:
            sock.connect(path)  # nothing listens there; the OS refuses, not the guard
        assert not isinstance(excinfo.value, netguard.AirgapViolation)


def test_model_client_probe_is_refused_and_reports_offline(
    listener: socket.socket, guard: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    netguard.install()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"{base}/api/tags", timeout=1)
    assert ollama_available(base, timeout=1) is False
    client = LLMClient(LLMConfig(cache_dir=tmp_path, ollama_base=base))
    assert client.offline and "no provider API key" in (client.offline_reason or "")
    assert not _got_connection(listener)


def test_install_from_env_and_status_command(
    guard: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REPLAYBOOK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("REPLAYBOOK_AIRGAPPED", "false")
    assert netguard.install_from_env() is False
    assert not netguard.is_installed()

    monkeypatch.setenv("REPLAYBOOK_AIRGAPPED", "true")
    monkeypatch.setenv("REPLAYBOOK_ALLOWED_HOSTS", "ollama:11434, http://models.internal:8443/v1")
    assert netguard.install_from_env() is True
    assert netguard.status()["allowed_hosts"] == ["ollama:11434", "models.internal:8443"]

    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "air-gapped mode: on" in result.output
    assert "ollama:11434" in result.output
    assert "telemetry: none" in result.output
    assert "offline" in result.output
    assert "refused by this process: 1" in result.output, "the Ollama probe was refused"


def test_parse_allowlist_accepts_hosts_ports_and_urls() -> None:
    text = " OLLAMA:11434,, https://Models.internal/v1 , [::1]:8080, ::1"
    parsed = netguard.parse_allowlist(text)
    assert [str(p) for p in parsed] == ["ollama:11434", "models.internal", "::1:8080", "::1"]
    assert parsed[1].port is None and parsed[2].port == 8080


def test_summary_is_recorded_with_every_run(guard: None) -> None:
    assert netguard.summary() == {
        "air_gapped": False,
        "allowed_hosts": [],
        "refused_connections": 0,
    }
    netguard.install("ollama:11434")
    assert netguard.summary()["air_gapped"] is True


def test_uninstall_restores_the_socket_module(guard: None) -> None:
    before = (socket.socket.connect, socket.getaddrinfo)
    netguard.install()
    assert (socket.socket.connect, socket.getaddrinfo) != before
    netguard.uninstall()
    assert (socket.socket.connect, socket.getaddrinfo) == before
    netguard.uninstall()  # idempotent


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_network_code_outside_the_model_client() -> None:
    """No telemetry: nothing but the model client and the guard can touch the network."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in MAY_USE_THE_NETWORK:
            continue
        used = {name for name in _imports(path) if name in NETWORK_MODULES}
        if used:
            offenders.append(f"{path.relative_to(SRC)}: {sorted(used)}")
    assert not offenders, offenders


def test_litellm_telemetry_is_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("litellm")
    fake.telemetry = True  # type: ignore[attr-defined]
    fake.calls = []  # type: ignore[attr-defined]

    class Message:
        content = "hello"
        tool_calls = None

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    def completion(**kwargs: object) -> Response:
        fake.calls.append(kwargs)  # type: ignore[attr-defined]
        return Response()

    fake.completion = completion  # type: ignore[attr-defined]
    fake.completion_cost = lambda completion_response: 0.0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.delenv("LITELLM_TELEMETRY", raising=False)

    client = LLMClient(
        LLMConfig(model_fast="openai/gpt-4.1-mini", cache_dir=tmp_path), check_providers=False
    )
    response = client.complete([{"role": "user", "content": "hi"}])
    assert response.text == "hello" and fake.calls  # type: ignore[attr-defined]
    assert fake.telemetry is False  # type: ignore[attr-defined]
