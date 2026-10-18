"""Air-gapped mode: refuse every outbound connection except an allowlisted model endpoint.

Set REPLAYBOOK_AIRGAPPED=true and put the endpoint you still allow in
REPLAYBOOK_ALLOWED_HOSTS (comma-separated host or host:port entries such as
"ollama:11434" or "localhost:11434"). Anything else this process tries to reach, name
lookups included, is refused before a packet leaves and counted, so the evidence pack can
say so. Nothing is allowed by default, not even localhost.

The guard patches the socket module of the current process. It is a seatbelt, not a
firewall: it covers Replaybook itself and the built-in LiteLLM agent, not an external
agent in its own process. For a hard boundary run the container with no network
(compose.yaml does) or on an internal network with the model endpoint.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

ENV_FLAG = "REPLAYBOOK_AIRGAPPED"
ENV_ALLOW = "REPLAYBOOK_ALLOWED_HOSTS"
TRUE_VALUES = ("1", "true", "yes", "on")
MAX_LOGGED = 50


class AirgapViolation(ConnectionRefusedError):
    """Air-gapped mode refused an outbound connection or name lookup."""


@dataclass(frozen=True)
class Allowed:
    host: str
    port: int | None

    def __str__(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


def parse_allowlist(text: str) -> list[Allowed]:
    """Parse "ollama:11434, http://models.internal:8443/v1, LOCALHOST" into entries."""
    entries: list[Allowed] = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            continue
        port: int | None
        if "://" in item:
            parts = urlsplit(item)
            host, port = parts.hostname or "", parts.port
        elif item.startswith("["):  # [::1]:11434
            host, _, rest = item[1:].partition("]")
            port = int(rest[1:]) if rest.startswith(":") else None
        elif item.count(":") == 1:
            host, _, port_text = item.partition(":")
            port = int(port_text)
        else:
            host, port = item, None
        if host:
            entries.append(Allowed(host.lower(), port))
    return entries


@dataclass
class _Guard:
    allowed: list[Allowed]
    # Addresses learnt by resolving an allowed name, so a connect to the resolved IP passes.
    resolved: dict[str, set[str]] = field(default_factory=dict)
    refused: list[str] = field(default_factory=list)
    refused_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def allows(self, host: Any, port: Any) -> bool:
        name = _as_name(host)
        port_number = _as_port(port)
        for entry in self.allowed:
            if entry.port is not None and port_number is not None and entry.port != port_number:
                continue
            if entry.host == name or name in self.resolved.get(entry.host, ()):
                return True
        return False

    def learn(self, name: str, addresses: Iterable[str]) -> None:
        with self.lock:
            self.resolved.setdefault(name.lower(), set()).update(a.lower() for a in addresses)

    def refuse(self, what: str) -> None:
        with self.lock:
            self.refused_count += 1
            if what not in self.refused and len(self.refused) < MAX_LOGGED:
                self.refused.append(what)
        raise AirgapViolation(f"air-gapped mode refused {what}")


_guard: _Guard | None = None
_original: dict[str, Any] = {}
_install_lock = threading.Lock()


def _as_name(host: Any) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).strip("[]").lower()


def _as_port(port: Any) -> int | None:
    if isinstance(port, int):
        return port
    if isinstance(port, str | bytes):
        text = port.decode() if isinstance(port, bytes) else port
        return int(text) if text.isdigit() else None
    return None


def _destination(sock: socket.socket, address: Any) -> tuple[str, int | None] | None:
    """The (host, port) of an INET address, or None for families that never leave the host."""
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return None
    if isinstance(address, tuple) and len(address) >= 2:
        return _as_name(address[0]), _as_port(address[1])
    return _as_name(address), None


def _check(sock: socket.socket, address: Any, verb: str) -> None:
    guard = _guard
    if guard is None:
        return
    destination = _destination(sock, address)
    if destination is None:
        return
    host, port = destination
    if not guard.allows(host, port):
        guard.refuse(f"{verb} to {host}:{port if port is not None else '?'}")


def _connect(self: socket.socket, address: Any) -> None:
    _check(self, address, "connect")
    return _original["connect"](self, address)


def _connect_ex(self: socket.socket, address: Any) -> int:
    _check(self, address, "connect")
    return _original["connect_ex"](self, address)


def _sendto(self: socket.socket, *args: Any) -> int:
    if args:
        _check(self, args[-1], "send")
    return _original["sendto"](self, *args)


def _lookup_allowed(name: str, port: Any, what: str) -> _Guard | None:
    guard = _guard
    if guard is None:
        return None
    if not guard.allows(name, port):
        guard.refuse(what)
    return guard


def _getaddrinfo(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> Any:
    if host in (None, "", b""):  # wildcard for bind; no lookup leaves the host
        return _original["getaddrinfo"](host, port, family, type, proto, flags)
    name = _as_name(host)
    guard = _lookup_allowed(name, port, f"lookup of {name}")
    results = _original["getaddrinfo"](host, port, family, type, proto, flags)
    if guard is not None:
        guard.learn(name, (str(item[4][0]) for item in results))
    return results


def _gethostbyname(name: Any) -> str:
    text = _as_name(name)
    guard = _lookup_allowed(text, None, f"lookup of {text}")
    address = _original["gethostbyname"](name)
    if guard is not None:
        guard.learn(text, [address])
    return address


def _gethostbyname_ex(name: Any) -> Any:
    text = _as_name(name)
    guard = _lookup_allowed(text, None, f"lookup of {text}")
    result = _original["gethostbyname_ex"](name)
    if guard is not None:
        guard.learn(text, result[2])
    return result


def _gethostbyaddr(address: Any) -> Any:
    text = _as_name(address)
    _lookup_allowed(text, None, f"reverse lookup of {text}")
    return _original["gethostbyaddr"](address)


def install(allowed: str | Iterable[str] = "") -> None:
    """Switch air-gapped mode on for this process. Calling it again replaces the allowlist."""
    global _guard
    text = allowed if isinstance(allowed, str) else ",".join(allowed)
    entries = parse_allowlist(text)
    with _install_lock:
        if _guard is not None:
            _guard.allowed = entries
            return
        _original.update(
            connect=socket.socket.connect,
            connect_ex=socket.socket.connect_ex,
            sendto=socket.socket.sendto,
            getaddrinfo=socket.getaddrinfo,
            gethostbyname=socket.gethostbyname,
            gethostbyname_ex=socket.gethostbyname_ex,
            gethostbyaddr=socket.gethostbyaddr,
        )
        _guard = _Guard(entries)
        socket.socket.connect = _connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = _sendto  # type: ignore[method-assign]
        socket.getaddrinfo = _getaddrinfo
        socket.gethostbyname = _gethostbyname
        socket.gethostbyname_ex = _gethostbyname_ex
        socket.gethostbyaddr = _gethostbyaddr


def uninstall() -> None:
    """Switch air-gapped mode off again (tests need this; a deployment never does)."""
    global _guard
    with _install_lock:
        if _guard is None:
            return
        socket.socket.connect = _original["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = _original["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = _original["sendto"]  # type: ignore[method-assign]
        socket.getaddrinfo = _original["getaddrinfo"]
        socket.gethostbyname = _original["gethostbyname"]
        socket.gethostbyname_ex = _original["gethostbyname_ex"]
        socket.gethostbyaddr = _original["gethostbyaddr"]
        _original.clear()
        _guard = None


def is_installed() -> bool:
    return _guard is not None


def enabled_in(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENV_FLAG, "").strip().lower() in TRUE_VALUES


def install_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """Install the guard when REPLAYBOOK_AIRGAPPED is set. Safe to call many times."""
    env = os.environ if environ is None else environ
    if not enabled_in(env):
        return False
    install(env.get(ENV_ALLOW, ""))
    return True


def status() -> dict[str, Any]:
    """What the evidence pack and `replaybook status` report about this process."""
    guard = _guard
    if guard is None:
        return {
            "air_gapped": False,
            "allowed_hosts": [],
            "refused_connections": 0,
            "refused": [],
        }
    with guard.lock:
        return {
            "air_gapped": True,
            "allowed_hosts": [str(entry) for entry in guard.allowed],
            "refused_connections": guard.refused_count,
            "refused": list(guard.refused),
        }


def summary() -> dict[str, Any]:
    """The short form recorded with every run."""
    current = status()
    return {
        "air_gapped": current["air_gapped"],
        "allowed_hosts": current["allowed_hosts"],
        "refused_connections": current["refused_connections"],
    }
