"""Send feedback (product-design.md §21): the links carry the version and system, never anything from the folder."""

from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import __version__, feedback
from openmymemo.cli import app
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}


@pytest.mark.parametrize(("plat", "patches", "expected"), [
    ("darwin", {"mac_ver": lambda: ("15.4", ("", "", ""), "arm64"), "machine": lambda: "arm64"}, "macOS 15.4 (arm64)"),
    ("win32", {"release": lambda: "10", "version": lambda: "10.0.22631", "machine": lambda: "AMD64"},
     "Windows 11 (AMD64)"),
    ("win32", {"release": lambda: "10", "version": lambda: "10.0.19045", "machine": lambda: "AMD64"},
     "Windows 10 (AMD64)"),
    ("linux", {"freedesktop_os_release": lambda: {"PRETTY_NAME": "Ubuntu 24.04 LTS"}, "machine": lambda: "x86_64"},
     "Ubuntu 24.04 LTS (x86_64)"),
])
def test_the_system_is_named_as_people_know_it(monkeypatch, plat, patches, expected):
    monkeypatch.setattr(feedback.sys, "platform", plat)
    for name, fn in patches.items():
        monkeypatch.setattr(feedback.platform, name, fn)
    assert feedback.system() == expected


def test_a_linux_without_os_release_still_has_a_name(monkeypatch):
    monkeypatch.setattr(feedback.sys, "platform", "linux")

    def missing():
        raise OSError("no os-release")

    monkeypatch.setattr(feedback.platform, "freedesktop_os_release", missing)
    monkeypatch.setattr(feedback.platform, "system", lambda: "Linux")
    monkeypatch.setattr(feedback.platform, "release", lambda: "6.8.0")
    monkeypatch.setattr(feedback.platform, "machine", lambda: "")
    assert feedback.system() == "Linux 6.8.0"


def test_the_links_fill_in_the_version_and_system_and_nothing_else(monkeypatch, vault):
    vault.add("work", "Secret project codename Bluebird")
    monkeypatch.setattr(feedback, "system", lambda: "macOS 15.4 (arm64)")
    monkeypatch.setattr(feedback.sys, "platform", "darwin")
    f = feedback.links(app=True)
    issue = urlsplit(f["issue_url"])
    assert f"{issue.scheme}://{issue.netloc}{issue.path}" == feedback.ISSUE_FORM
    assert parse_qs(issue.query) == {"template": ["feedback.yml"], "version": [__version__],
                                     "os": ["macOS 15.4 (arm64)"]}
    assert "+" not in issue.query  # spaces as %20, which every form reads as spaces
    mail = urlsplit(f["email_url"])
    assert mail.scheme == "mailto" and mail.path == feedback.EMAIL
    fields = {k: unquote(v) for k, v in (p.split("=", 1) for p in mail.query.split("&"))}
    assert fields["subject"] == "OpenMyMemo feedback"
    assert fields["body"].endswith(f"OpenMyMemo {__version__} · macOS 15.4 (arm64) · Mac app\n")
    assert f["about"] == f"OpenMyMemo {__version__} · macOS 15.4 (arm64) · Mac app"
    assert "Bluebird" not in str(f)

    monkeypatch.setattr(feedback.sys, "platform", "linux")  # no desktop app there to name
    assert feedback.links(app=True)["about"] == f"OpenMyMemo {__version__} · macOS 15.4 (arm64)"
    assert "app" not in feedback.links()["about"]


def test_the_panel_gets_the_links_behind_its_guard(vault):
    api = TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")
    assert api.get("/api/feedback").status_code == 403
    r = api.get("/api/feedback?app=true", headers=H).json()
    assert r["issue_url"].startswith(feedback.ISSUE_FORM) and r["email"] == feedback.EMAIL
    assert r["version"] == __version__ and r["system"] == feedback.system()


def test_openmymemo_feedback_prints_both_ways_and_opens_only_when_asked(monkeypatch, vault):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "feedback"])
    assert r.exit_code == 0 and opened == []
    assert feedback.links()["issue_url"] in r.stdout and feedback.EMAIL in r.stdout
    assert "don't include your memories" in r.stdout
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "feedback", "--open"])
    assert r.exit_code == 0 and opened == [feedback.links()["issue_url"]]
