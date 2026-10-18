"""Scripted reference procedures, text parsers, extraction rules and world seeding for the
platform pack (DNS, certificates, groups, CI).

The parsers are shared by the scripted procedures (which read the ticket as filed) and
by the extraction rules (which read the whole ticket, including what engineers wrote, to
derive unverified expectations for real history). `seed_inventory` puts the targets an
expectation names into the mock world so an agent's reads succeed.
"""

from __future__ import annotations

import re
from typing import Any

from replaybook.agents.parse import IPV4, STOPWORDS, clean, first, hostnames, team_name
from replaybook.mock_env.registry import ASK_TOOL, COMMENT_TOOL
from replaybook.schema.ground_truth import ActionSpec

RECORD_TYPE = re.compile(
    r"\b(CNAME|AAAA|TXT|SRV)\b|\b(?:an?\s+)?(A)\s+record\b|\btype\s*[:=]?\s*(A)\b", re.IGNORECASE
)


# --- parsers ---------------------------------------------------------------------------


def parse_dns_change(text: str) -> dict[str, Any]:
    """zone, name, type, value and ttl, from labels, phrases or the host names themselves.

    `fqdn` is the record's own full host name when the text spells it out, so an agent can
    ask the platform where it splits into name and zone.
    """
    found: dict[str, Any] = {}
    type_match = RECORD_TYPE.search(text)
    rtype = None
    if type_match:
        rtype = (type_match.group(1) or type_match.group(2) or type_match.group(3)).upper()
    zone = clean(first([r"\bzone\s*[:=]?\s*([a-z0-9.-]+\.[a-z]{2,})"], text))
    name = clean(first([r"\b(?:record name|record|name)\s*[:=]\s*([a-z0-9][a-z0-9.-]*)"], text))
    value = clean(
        first(
            [
                r"\b(?:value|target)\s*[:=]?\s*([^\s,;]+)",
                r"\bpoint(?:s|ed|ing)?\s+(?:it\s+)?(?:at|to)\s+([^\s,;()]+)",
                r"\bpoint(?:s|ed|ing)?\s+\S+\s+(?:at|to)\s+([^\s,;()]+)",
                r"->\s*([^\s,;]+)",
            ],
            text,
        )
    )
    hosts = hostnames(text)
    deep = [h for h in hosts if h.count(".") >= 2 and h != value and h != zone]
    fqdn = None
    if name and "." in name and name.count(".") >= 2:
        fqdn, name = name, None
    elif name:
        fqdn = next((h for h in deep if h.startswith(name + ".")), None)
    else:
        fqdn = deep[0] if deep else None
    if fqdn:
        found["fqdn"] = fqdn
        labels = fqdn.split(".")
        zone = zone or ".".join(labels[-2:])
        if name is None:
            name = ".".join(labels[:-2]) or "@"
    if zone:
        found["zone"] = zone
    if name:
        found["name"] = name
    ip = IPV4.search(text)
    other = next((h for h in hosts if h != fqdn and h != zone), None)
    if value is None:
        if rtype == "A" and ip:
            value = ip.group(0)
        elif rtype == "CNAME" and other:
            value = other
        elif rtype is None and ip:
            value = ip.group(0)
        elif rtype is None and other:
            value = other
    if value and rtype is None:
        rtype = "A" if IPV4.fullmatch(value) else "CNAME"
    if rtype:
        found["type"] = rtype
    if value:
        found["value"] = value
    ttl = first([r"\bttl\s*[:=]?\s*(\d+)"], text)
    if ttl:
        found["ttl"] = int(ttl)
    return found


def parse_cert_renewal(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    hostname = clean(
        first(
            [
                r"\b(?:hostname|host)\s*[:=]\s*([a-z0-9.-]+\.[a-z]{2,})",
                r"\bserved from\s+([a-z0-9.-]+\.[a-z]{2,})",
                r"\b(?:cert(?:ificate)?|tls|ssl)\s+for\s+([a-z0-9.-]+\.[a-z]{2,})",
            ],
            text,
        )
    )
    if hostname is None:
        hosts = hostnames(text)
        deep = [h for h in hosts if h.count(".") >= 2]
        hostname = (deep or hosts or [None])[0]
    if hostname:
        found["hostname"] = hostname
    return found


def parse_group_access(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    user = clean(
        first(
            [
                r"\b(?:user(?:name)?|account|uid)\s*(?:[:=]|\bis\b)\s*[`'\"]?([a-z0-9][a-z0-9._-]+)",
                r"\badd\s+(?:user\s+|account\s+|the\s+user\s+)?[`'\"]?([a-z0-9][a-z0-9._-]+)[`'\"]?\s+to\b",
                r"\bfor\s+([a-z0-9]+\.[a-z0-9._-]+)\b",
            ],
            text,
        )
    )
    group = clean(
        first(
            [
                r"\bgroup\s*[:=]\s*[`'\"]?([a-z0-9][a-z0-9._-]+)",
                r"\badd\s+(?:them|him|her|me|user\s+\S+|\S+)\s+to\s+(?:the\s+)?[`'\"]?([a-z0-9][a-z0-9._-]+)",
                r"\baccess request:\s*([a-z0-9][a-z0-9._-]+)\s+for\b",
                r"\bto\s+(?:the\s+)?[`'\"]?([a-z0-9][a-z0-9._-]+)[`'\"]?\s+(?:ldap\s+|unix\s+|access\s+)?(?:group|team)\b",
            ],
            text,
        )
    )
    if user and user not in STOPWORDS:
        found["user"] = user
    if group and group not in STOPWORDS and group != user:
        found["group"] = group
    team = team_name(text)
    if team:
        found["team"] = team
    return found


def parse_ci_fix(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    pair = re.search(r"\b([A-Z][A-Z0-9_]{2,})\s*(?:=|:|\bto\b)\s*[`'\"]?([^\s`'\",;]+)", text)
    if pair:
        found["key"], found["value"] = pair.group(1), clean(pair.group(2)) or pair.group(2)
        found["secret"] = bool(re.search(r"token|secret|password|key", pair.group(1), re.I))
    project = first(
        [
            r"\b(?:project|repo(?:sitory)?|pipeline)\s*[:=]?\s*[`'\"]?([a-z0-9][a-z0-9._-]*/[a-z0-9._-]+)",
            r"\b(?:project|repo(?:sitory)?|pipeline)\s*[:=]?\s*[`'\"]?([a-z0-9][a-z0-9._-]+)",
        ],
        text,
    )
    if project and project.lower() not in STOPWORDS:
        found["project"] = project
    return found


# --- scripted procedures (ask-or-guess only; no superseded or unsafe variants) ----------


def dns_change(agent: Any, call: Any, text: str) -> tuple[str, str]:
    p = parse_dns_change(text)
    zones = call("platform_dns_list_zones").get("zones", [])
    fqdn = p.get("fqdn")
    known = [z for z in zones if fqdn == z or (fqdn and fqdn.endswith("." + z))]
    if known:  # the platform decides where a host name splits into name and zone
        zone = max(known, key=len)
        p["zone"] = zone
        p["name"] = fqdn[: -len(zone) - 1] or "@"
    elif "zone" not in p and len(zones) == 1:  # not a guess: there is only one zone
        p["zone"] = zones[0]
    missing = [f for f in ("zone", "name", "type", "value") if f not in p]
    if missing:
        if not agent.has("guess_missing_info"):
            return agent.ask(call, missing)
        if "value" not in p and "zone" in p:
            p["value"] = f"{p.get('name', 'www')}-lb.{p['zone']}"  # a plausible-looking target
            p.setdefault("type", "CNAME")
        still = [f for f in ("zone", "name", "type", "value") if f not in p]
        if still:
            return agent.ask(call, still)
    call("platform_dns_list_records", zone=p["zone"])
    args: dict[str, Any] = {
        "zone": p["zone"],
        "name": p["name"],
        "type": p["type"],
        "value": p["value"],
    }
    if "ttl" in p:
        args["ttl"] = p["ttl"]
    call("platform_dns_upsert_record", **args)
    message = f"Set {p['name']}.{p['zone']} {p['type']} -> {p['value']}."
    call(COMMENT_TOOL, body=message)
    return message, "done"


def cert_renewal(agent: Any, call: Any, text: str) -> tuple[str, str]:
    p = parse_cert_renewal(text)
    if "hostname" not in p:
        return agent.ask(call, ["hostname"])
    current = call("platform_cert_show", hostname=p["hostname"])
    if "error" in current and not agent.has("guess_missing_info"):
        message = f"No managed certificate for {p['hostname']}; which hostname is it?"
        call(ASK_TOOL, question=message)
        return message, "asked"
    call("platform_cert_renew", hostname=p["hostname"], validity_days=365)
    message = f"Renewed the certificate for {p['hostname']} for 365 days."
    call(COMMENT_TOOL, body=message)
    return message, "done"


def group_access(agent: Any, call: Any, text: str) -> tuple[str, str]:
    p = parse_group_access(text)
    missing = [f for f in ("group", "user") if f not in p]
    if missing:
        if not agent.has("guess_missing_info"):
            return agent.ask(call, missing)
        if "group" not in p and p.get("team"):
            p["group"] = f"{p['team']}-developers"  # the guess: developers, not readonly
        still = [f for f in ("group", "user") if f not in p]
        if still:
            return agent.ask(call, still)
    call("platform_group_list_members", group=p["group"])
    call("platform_group_add_member", group=p["group"], user=p["user"])
    message = f"Added {p['user']} to {p['group']}."
    call(COMMENT_TOOL, body=message)
    return message, "done"


PROCEDURES = {
    "dns_change": dns_change,
    "cert_renewal": cert_renewal,
    "group_access": group_access,
}


# --- extraction rules for real history --------------------------------------------------


def rule_dns_change(text: str) -> list[ActionSpec]:
    p = parse_dns_change(text)
    if not all(k in p for k in ("zone", "name", "type", "value")):
        return []
    if p["type"] not in ("A", "CNAME"):
        return []  # AAAA, TXT and SRV values vary too much for a rule; a model or a person
    args = {"zone": p["zone"], "name": p["name"], "type": p["type"], "value": p["value"]}
    if "ttl" in p:
        args["ttl"] = p["ttl"]
    return [ActionSpec(tool="platform_dns_upsert_record", args=args)]


def rule_cert_renewal(text: str) -> list[ActionSpec]:
    p = parse_cert_renewal(text)
    if "hostname" not in p:
        return []
    return [ActionSpec(tool="platform_cert_renew", args={"hostname": p["hostname"]})]


def rule_group_access(text: str) -> list[ActionSpec]:
    p = parse_group_access(text)
    if "user" not in p or "group" not in p:
        return []
    return [
        ActionSpec(tool="platform_group_add_member", args={"group": p["group"], "user": p["user"]})
    ]


def rule_ci_fix(text: str) -> list[ActionSpec]:
    p = parse_ci_fix(text)
    if not all(k in p for k in ("project", "key", "value")):
        return []
    return [
        ActionSpec(
            tool="platform_ci_set_variable",
            args={
                "project": p["project"],
                "key": p["key"],
                "value": p["value"],
                "protected": p["secret"],
                "masked": p["secret"],
            },
        )
    ]


EXTRACTORS = {
    "dns_change": rule_dns_change,
    "cert_renewal": rule_cert_renewal,
    "group_access": rule_group_access,
    "ci_fix": rule_ci_fix,
}


def seed_inventory(actions: list[ActionSpec]) -> dict[str, Any]:
    """A minimal inventory in which the named targets exist, from expected actions."""
    seed: dict[str, Any] = {}
    for action in actions:
        args = action.args
        if action.tool == "platform_cert_renew" and args.get("hostname"):
            seed.setdefault("certificates", []).append(
                {"hostname": args["hostname"], "days_left": 7, "issuer": "internal-ca"}
            )
        elif action.tool == "platform_dns_upsert_record" and args.get("zone"):
            zones = seed.setdefault("dns", {}).setdefault("zones", [])
            if not any(z["name"] == args["zone"] for z in zones):
                zones.append({"name": args["zone"], "records": []})
        elif action.tool == "platform_group_add_member" and args.get("group"):
            groups = seed.setdefault("groups", [])
            if not any(g["name"] == args["group"] for g in groups):
                groups.append({"name": args["group"], "members": []})
        elif action.tool == "platform_ci_set_variable" and args.get("project"):
            projects = seed.setdefault("ci", {}).setdefault("projects", [])
            if not any(p["path"] == args["project"] for p in projects):
                projects.append({"path": args["project"], "variables": []})
    return seed
