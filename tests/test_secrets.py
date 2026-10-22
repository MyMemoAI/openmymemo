import pytest

from openmymemo import secrets
from openmymemo.secrets import Finding, kinds_in, scan

# Built at run time, so no secret scanner mistakes the test file for a leak.
OPENAI = "sk-" + "proj-" + "Ab3" * 10
GITHUB = "ghp" + "_" + "x9Y" * 12
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N"])
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"


@pytest.mark.parametrize("text, kinds", [
    (f"My OpenAI key is {OPENAI}", ["key"]),
    (f"Use {GITHUB} for the deploy bot", ["key"]),
    (f"AWS: {AWS}", ["key"]),
    (f"Session cookie {JWT}", ["key"]),
    (f"The server's key starts with {PEM}", ["key"]),
    ("api_key = 9f8e7d6c5b4a39281706", ["key"]),
    ("Stripe secret: live_51HxQ2bC3dE4fG5hI", ["key"]),
    ("Wi-Fi password: hunter2", ["password"]),
    ("my bank password is Tr0ub4dor&3", ["password"]),
    ("家里路由器的密码是 abc12345", ["password"]),
    ("Visa 4111 1111 1111 1111, expires 2027", ["card"]),
    ("Amex 3782-822463-10005", ["card"]),
    ("Mastercard 2223000048400011", ["card"]),
    ("Mastercard 2223 0000 4840 0011", ["card"]),
    ("SSN 123-45-6789", ["id_number"]),
    ("身份证号 11010519491231002X", ["id_number"]),
    (f"password: s3cret!, key {OPENAI}", ["key", "password"]),
])
def test_finds_each_kind(text, kinds):
    assert kinds_in(text) == kinds


@pytest.mark.parametrize("text", [
    "Uses a password manager (1Password) for everything",
    "The password is long and changes every month",
    "Prefers tea over coffee",
    "A token of appreciation for the team",
    "The secret is outstandingperformance",  # no digit: a phrase, not a value
    "Order number 4111 1111 1111 1112",       # fails the Luhn check
    "Ticket 2223 0000 4840 0012",              # 2-series shape, fails the Luhn check
    "0000 0000 0000 0000",
    "Phone +1 415 555 0123",                   # phone numbers are left out (§18.2)
    "手机 13800138000",
    "Born 1949-12-31, moved 2020-01-15",
    "SSN-shaped but invalid: 000-12-3456 and 666-12-3456 and 900-12-3456",
    "身份证号 110105194912310021",              # wrong check character
    "Version 1.2.3, build 20261022",
    "sk-short",
])
def test_ordinary_text_is_not_a_secret(text):
    assert kinds_in(text) == []


def test_scan_skips_private_topics_and_private_memories(vault):
    assert {"health", "finance"} <= vault.private_topics()
    shown = vault.add("work", f"Deploy key {GITHUB}")
    vault.add("finance", "Visa 4111 1111 1111 1111")              # a private topic: where it belongs
    vault.add("work", "Wi-Fi password: hunter2", private=True)     # marked private
    archived = vault.add("work", "SSN 123-45-6789")
    vault.archive(archived.id)
    assert scan(vault) == [Finding(shown.id, "work", "key")]


def test_findings_never_carry_the_matched_text(vault):
    m = vault.add("work", f"password: hunter2 and {OPENAI}")
    found = scan(vault)
    assert {f.kind for f in found} == {"key", "password"} and {f.memory_id for f in found} == {m.id}
    for f in found:
        assert "hunter2" not in repr(f) and OPENAI not in repr(f)


def test_kinds_are_the_documented_ones():
    assert secrets.KINDS == ("key", "password", "card", "id_number")
