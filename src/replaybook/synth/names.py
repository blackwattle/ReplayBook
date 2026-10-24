"""Deterministic name pools for synthetic data. Everything here is made up."""

from __future__ import annotations

import random
from collections.abc import Sequence

SERVICES = (
    "payments",
    "ledger",
    "kyc",
    "fraud-scoring",
    "customer-portal",
    "statements",
    "card-auth",
    "settlements",
    "notifications",
    "risk-engine",
    "pricing",
    "onboarding",
    "claims",
    "policy-admin",
    "reporting",
    "data-platform",
    "identity",
    "treasury",
    "mortgage-origination",
    "super-contributions",
    "open-banking-gateway",
    "collections",
)
TEAMS = (
    "payments-platform",
    "lending",
    "data-eng",
    "risk-tech",
    "digital",
    "core-banking",
    "insurance-claims",
    "wealth",
    "sre",
    "security-eng",
    "channels",
    "finance-systems",
)
ENVS = (("dev", 0.3), ("test", 0.2), ("uat", 0.2), ("prod", 0.3))
PRIORITIES = (("Low", 0.2), ("Medium", 0.55), ("High", 0.2), ("Critical", 0.05))

# Pseudonymous handles. Requesters write tickets; engineers work them.
REQUESTERS = tuple(
    f"{first}.{last}"
    for first, last in [
        ("ana", "silva"),
        ("alex", "okafor"),
        ("chloe", "nguyen"),
        ("dan", "mehta"),
        ("eli", "rossi"),
        ("fay", "kowalski"),
        ("gus", "tanaka"),
        ("hana", "ibrahim"),
        ("ivan", "petrov"),
        ("jo", "fitzgerald"),
        ("kai", "lindqvist"),
        ("lena", "moreau"),
        ("max", "ovalle"),
        ("nia", "adeyemi"),
        ("omar", "haddad"),
        ("pia", "schulz"),
        ("quinn", "doyle"),
        ("ravi", "iyer"),
        ("sam", "whitford"),
        ("tara", "bennett"),
        ("uma", "krishnan"),
        ("vic", "andersen"),
        ("wen", "zhao"),
        ("xavi", "puig"),
        ("yara", "costa"),
        ("zed", "mackay"),
        ("ali", "rahimi"),
        ("bea", "ferreira"),
        ("cal", "murphy"),
        ("dee", "oyelaran"),
        ("eve", "lambert"),
        ("fin", "gallagher"),
    ]
)
ENGINEERS = tuple(f"eng-{i:02d}" for i in range(1, 13))

NOISE_PEOPLE_EMAILS = (
    "someone@example.com",
    "oncall@example.com",
    "vendor.support@example.net",
)


def pick[T](rng: random.Random, items: Sequence[T]) -> T:
    return items[rng.randrange(len(items))]


def weighted[T](rng: random.Random, items: Sequence[tuple[T, float]]) -> T:
    total = sum(weight for _, weight in items)
    roll = rng.random() * total
    for item, weight in items:
        roll -= weight
        if roll <= 0:
            return item
    return items[-1][0]


def project_name(team: str, env: str) -> str:
    return f"{team}-{env}"
