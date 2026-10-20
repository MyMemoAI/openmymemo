"""OpenMyMemo Cloud (product-design.md §16) against a fake service built from tests/fixtures/cloud/.

Every test has its own HOME, OPENMYMEMO_CLOUD_DIR and a local OPENMYMEMO_CLOUD_URL (conftest.py); nothing here
reaches MyMemo or touches this computer's real cloud.json.
"""

import inspect
import json
import os
import stat
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import anyio
import pytest
from cloud_fake import FakeService, fixture, wait_until
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import __version__, clients, cloud, mcp_server
from openmymemo.cli import app as cli
from openmymemo.cloud import Bridge, Cloud, Link, Timing
from openmymemo.connectors import Connectors
from openmymemo.mcp_server import build_server
from openmymemo.proposals import Proposals
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
FAST = Timing(backoff_base=0.02, backoff_cap=0.1, plan_retry=60.0, restart_jitter=0.05, open_timeout=5,
              welcome_timeout=5)
VECTOR = fixture("hpke_vector.json")
WELCOME = fixture("bridge/welcome.json")


@pytest.fixture
def fake():
    service = FakeService().start()
    yield service
    service.stop()


@pytest.fixture
def clouds():
    """Cloud objects a test starts; their bridges are stopped afterwards."""
    made: list[Cloud] = []
    yield made
    for c in made:
        c.stop()


def linked(vault, service: str) -> Link:
    """A cloud.json for `vault` with the fixtures' device and the HPKE vector's key pair."""
    link = Link(service=service, device_id=VECTOR["device_id"], device_token=fixture("link/response.json")["device_token"],
                private_key=VECTOR["private_key"], key_id=VECTOR["key_id"], vault=str(vault.root),
                member={"display_name": "Sam"}, plan={"tier": "ultra", "live": True, "queue": True},
                linked_at="2026-10-12T20:00:00+00:00")
    cloud.save_link(link)
    return link


def online(vault, fake, clouds, timing: Timing = FAST) -> tuple[Cloud, object]:
    linked(vault, fake.base)
    c = Cloud(vault, 8787, timing)
    clouds.append(c)
    assert c.start()
    conn = fake.connection()
    wait_until(lambda: c.status()["online"])
    return c, conn


def call(conn, frame: dict) -> dict:
    return conn.call(frame)


# ---- the wire format -------------------------------------------------------------------------------------------


def test_authorize_url_and_link_request_match_the_fixtures():
    expected = fixture("link/authorize_url.json")["url"]
    request = fixture("link/request.json")
    url = cloud.authorize_url("https://api.mymemo.ai", request["redirect_uri"], cloud.pkce_challenge(request["code_verifier"]),
                              "af0ifjsldkj", "Sam’s MacBook Air")
    assert url == expected
    assert cloud.link_request(request["code"], request["code_verifier"], request["redirect_uri"], request["public_key"],
                              app_version="0.3.0") == request
    assert list(cloud.link_request("c", "v", "r", "p")) == list(request)  # same fields, same order
    assert cloud.endpoint("https://api.mymemo.ai/") == "https://api.mymemo.ai/openmymemo/mcp"


def test_key_id_and_key_pair():
    assert cloud.key_id(cloud.unb64u(VECTOR["public_key"])) == VECTOR["key_id"]
    private, public = cloud.new_key_pair()
    assert len(cloud.unb64u(private)) == 32 and len(cloud.unb64u(public)) == 32 and private != public


def test_cloud_json_lives_per_computer_never_in_the_folder(tmp_path):
    home = tmp_path / "h"
    assert cloud.cloud_dir({}, "darwin", home) == home / "Library/Application Support/OpenMyMemo"
    assert cloud.cloud_dir({}, "linux", home) == home / ".config/openmymemo"
    assert cloud.cloud_dir({"XDG_CONFIG_HOME": "/x/conf"}, "linux", home) == Path("/x/conf/openmymemo")
    assert cloud.cloud_dir({"APPDATA": "C:/Users/s/AppData/Roaming"}, "win32", home) == \
        Path("C:/Users/s/AppData/Roaming/OpenMyMemo")
    assert cloud.cloud_dir({"OPENMYMEMO_CLOUD_DIR": "/elsewhere"}, "darwin", home) == Path("/elsewhere")
    assert cloud.link_path() == Path(os.environ["OPENMYMEMO_CLOUD_DIR"]) / "cloud.json"  # tests never see the real one


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_cloud_json_is_private_from_the_first_byte_and_written_atomically(vault):
    link = linked(vault, "http://127.0.0.1:9")
    path = cloud.link_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert {"service", "device_id", "device_token", "private_key", "key_id", "vault", "member", "plan"} <= set(data)
    assert cloud.load_link() == link
    assert sorted(p.name for p in path.parent.iterdir()) == [".cloud.lock", "cloud.json"]  # no temporary file left
    assert not cloud.delete_link("another-device") and path.exists()  # a newer link is never deleted by mistake
    assert cloud.delete_link(link.device_id) and not path.exists()


@pytest.mark.parametrize("change", ["delete", "update"])
def test_changes_to_cloud_json_wait_for_each_other(change, vault, monkeypatch):
    """The bridge's compare-and-delete (or update) and another process saving a new link don't interleave: without
    the lock, the new link would land between the check and the write and be deleted or overwritten."""
    import dataclasses

    old = linked(vault, "http://127.0.0.1:9")
    new = dataclasses.replace(old, device_id="new-device", device_token="omd_new")
    real = cloud.load_link
    saver: list[threading.Thread] = []

    def slow_load():
        current = real()
        if not saver:
            saver.append(threading.Thread(target=cloud.save_link, args=(new,)))
            saver[0].start()
            saver[0].join(0.3)  # gives a concurrent save every chance to slip in here
        return current

    monkeypatch.setattr(cloud, "load_link", slow_load)
    if change == "delete":
        assert cloud.delete_link(old.device_id)
    else:
        cloud._update_link(old.device_id, plan={"tier": "pro", "live": True, "queue": False})
    saver[0].join(5)
    assert real() == new


def test_hello_carries_the_cloud_variant_of_this_installations_tools(vault):
    hello = fixture("bridge/hello.json")
    link = linked(vault, "http://127.0.0.1:9")
    ours = anyio.run(Bridge(vault, link).hello)
    assert ours["type"] == "hello" and ours["v"] == 1 and ours["app_version"] == __version__
    assert ours["key_id"] == hello["key_id"] == VECTOR["key_id"] and ours["max_inflight"] == hello["max_inflight"]
    assert ours["tools"] == hello["tools"] and ours["instructions"] == hello["instructions"]
    # At launch (live relay only, cloud.FEATURES empty) the descriptors are the local tools' own: the service keeps
    # them and shows them to ChatGPT and Claude, so nothing in them may promise the offline queue.
    local = {t.name: inspect.cleandoc(t.description) for t in anyio.run(build_server(vault).list_tools)}
    assert {t["name"]: t["description"] for t in ours["tools"]} == local
    assert "queue" not in json.dumps(ours["tools"]) and "features" not in ours


def test_the_queue_is_described_to_web_apps_once_this_computer_takes_it(vault, queue_on):
    link = linked(vault, "http://127.0.0.1:9")
    ours = anyio.run(Bridge(vault, link).hello)
    assert ours["features"] == ["queue"]
    local = {t.name: inspect.cleandoc(t.description) for t in anyio.run(build_server(vault).list_tools)}
    cloud_tools = {t["name"]: t["description"] for t in ours["tools"]}
    assert {n for n in cloud_tools if cloud_tools[n] != local[n]} == {"memory_propose", "memory_proposals"}
    assert cloud_tools["memory_propose"] == local["memory_propose"] + mcp_server.CLOUD_QUEUED
    assert cloud_tools["memory_proposals"] == local["memory_proposals"] + "\n" + mcp_server.CLOUD_QUEUE_ID
    assert '"queued"' in cloud_tools["memory_propose"] and "`queue_id`" in cloud_tools["memory_proposals"]
    assert all("queue" not in d for d in local.values())  # apps on this computer never hear of it


def test_backoff_is_exponential_with_full_jitter():
    assert [cloud.backoff_delay(n, rand=lambda: 1.0) for n in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    assert cloud.backoff_delay(3, rand=lambda: 0.5) == 4 and cloud.backoff_delay(500, rand=lambda: 1.0) == 60
    assert cloud.backoff_delay(0, rand=lambda: 0.0) == 0


def test_web_app_names():
    assert cloud.web_name({"slug": "chatgpt", "name": "ChatGPT", "verified": True}) == "ChatGPT (web)"
    assert cloud.web_name({"slug": "x-1a2b", "name": "Foo (unverified)", "verified": False}) == "Foo (web, unverified)"


# ---- linking through the panel ----------------------------------------------------------------------------------


@pytest.fixture
def panel(vault, clouds):
    c = Cloud(vault, 8787, FAST)
    clouds.append(c)
    return TestClient(create_api(vault, 8787, c), base_url="http://127.0.0.1:8787"), c


def callback_path(url: str) -> str:
    parts = urlsplit(url)
    assert parts.netloc == "127.0.0.1:8787" and parts.path == "/cloud/callback"
    return f"{parts.path}?{parts.query}"


def test_linking_from_the_panel_writes_cloud_json_and_connects(vault, fake, panel, monkeypatch):
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", fake.base)
    api, c = panel
    assert api.get("/api/cloud", headers=H).json()["linked"] is False

    url = api.post("/api/cloud/link", headers=H).json()["url"]
    params = dict(parse_qsl(urlsplit(url).query))
    assert url.startswith(f"{fake.base}/openmymemo/oauth/authorize?response_type=code&client_id=openmymemo-desktop&")
    assert params["redirect_uri"] == "http://127.0.0.1:8787/cloud/callback"
    assert params["resource"] == f"{fake.base}/openmymemo/bridge" and params["code_challenge_method"] == "S256"
    assert params["device_name"] and len(params["device_name"]) <= 64
    assert api.get("/api/cloud", headers=H).json()["pending"] is True
    stale = api.post("/api/cloud/link", headers=H).json()["url"]  # a second Sign in, left open in another tab

    back = callback_path(fake.authorize(url))
    r = api.get(back)  # a browser navigation: no X-OpenMyMemo header
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "<h1>Linked to Sam (s***@example.com)</h1>" in r.text and "isn&#x27;t your MyMemo account" in r.text
    assert r.headers["referrer-policy"] == "no-referrer"

    sent = fake.link_requests[-1]
    assert list(sent) == list(fixture("link/request.json")) and sent["app_version"] == __version__
    assert sent["redirect_uri"] == params["redirect_uri"] and sent["key_alg"] == "x25519"
    link = cloud.load_link()
    response = fixture("link/response.json")
    assert link.device_id == response["device_id"] and link.device_token == response["device_token"]
    assert link.service == fake.base and link.vault == str(vault.root) and link.member == response["member"]
    assert link.plan == response["plan"] and link.key_id == cloud.key_id(cloud.unb64u(sent["public_key"]))
    assert stat.S_IMODE(cloud.link_path().stat().st_mode) == 0o600 or os.name == "nt"
    assert not (vault.root / "cloud.json").exists()

    # The panel connects right away.
    conn = fake.connection()
    assert conn.hello["key_id"] == link.key_id and conn.headers["authorization"] == f"Bearer {link.device_token}"
    assert "origin" not in conn.headers
    status = wait_until(lambda: (s := api.get("/api/cloud", headers=H).json())["online"] and s)
    assert status["account"] == {"display_name": "Sam", "email_masked": "s***@example.com"}
    assert status["plan"]["tier"] == "ultra" and cloud.load_link() == link  # welcome names Sam only: the email stays
    assert [(a["connector"], a["action"], a["account"], a["email_masked"]) for a in vault.activity()] == [
        ("mymemo-cloud", "link", "Sam", "s***@example.com")]
    assert status["endpoint"] == f"{fake.base}/openmymemo/mcp" and status["this_folder"] and not status["pending"]

    # The same callback again (a replay) does nothing.
    again = api.get(back)
    assert again.status_code == 400 and "valid anymore" in again.text
    assert len(fake.link_requests) == 1 and cloud.load_link() == link
    # So does the other tab's sign-in: linking made it stale.
    assert api.get(callback_path(fake.authorize(stale))).status_code == 400
    assert len(fake.link_requests) == 1 and cloud.load_link() == link


def test_two_sign_in_tabs_answered_at_once_link_once(vault, fake, panel, monkeypatch):
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", fake.base)
    api, _ = panel
    first, second = (callback_path(fake.authorize(api.post("/api/cloud/link", headers=H).json()["url"]))
                     for _ in range(2))
    real, calls = cloud._request, []

    def slow_first(method, url, **kw):  # the first exchange's answer arrives after the second's would
        calls.append(url)
        if len(calls) == 1:
            time.sleep(0.5)
        return real(method, url, **kw)

    monkeypatch.setattr(cloud, "_request", slow_first)
    answers: dict[str, int] = {}
    threads = [threading.Thread(target=lambda n=n, path=path: answers.__setitem__(n, api.get(path).status_code))
               for n, path in (("first", first), ("second", second))]
    threads[0].start()
    time.sleep(0.1)
    threads[1].start()
    for t in threads:
        t.join(10)
    assert answers == {"first": 200, "second": 400}  # the second waited, and found its sign-in stale
    assert len(fake.link_requests) == 1 and cloud.load_link().device_token == fake.token


def test_a_stale_bridge_forgetting_its_link_leaves_a_newer_link_and_its_web_apps_alone(vault):
    old = linked(vault, "http://127.0.0.1:9")
    bridge = Bridge(vault, old)
    new = Link(**{**old.__dict__, "device_id": "new-device", "device_token": "omd_TEST_ONLY_new"})
    cloud.save_link(new)  # linked again (from another process) before the old bridge heard it was gone
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    bridge._forget()
    assert cloud.load_link() == new and [c.id for c in Connectors(vault).list()] == ["cloud-chatgpt"]
    Bridge(vault, new)._forget()  # its own link: the web apps go with it
    assert cloud.load_link() is None and Connectors(vault).list() == []


def test_a_link_saved_while_the_old_one_is_forgotten_waits_for_it(vault, monkeypatch):
    old = linked(vault, "http://127.0.0.1:9")
    new = Link(**{**old.__dict__, "device_id": "new-device", "device_token": "omd_TEST_ONLY_new"})
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    real, saved = cloud.revoke_web_apps, threading.Event()

    def revoke_while_relinking(v):
        threading.Thread(target=lambda: (cloud.save_link(new), saved.set()), daemon=True).start()
        assert not saved.wait(0.3)  # the replacement can't be saved between the check and the revocation
        return real(v)

    monkeypatch.setattr(cloud, "revoke_web_apps", revoke_while_relinking)
    Bridge(vault, old)._forget()
    assert saved.wait(5) and cloud.load_link() == new  # saved once the old link was gone, and kept


def test_the_account_is_kept_up_to_date_from_welcome_and_shown_with_its_masked_email(vault, fake, clouds):
    fake.welcome = {**fake.welcome, "member": {"display_name": "Sam Lee", "email_masked": "s***@example.org"}}
    c, _ = online(vault, fake, clouds)  # linked() knows only the display name "Sam"
    wait_until(lambda: cloud.load_link().email_masked == "s***@example.org")
    assert cloud.load_link().member == {"display_name": "Sam Lee", "email_masked": "s***@example.org"}
    assert c.status()["account"] == {"display_name": "Sam Lee", "email_masked": "s***@example.org"}

    assert cloud.account_label({"display_name": "Sam", "email_masked": "s***@example.com"}) == "Sam (s***@example.com)"
    assert cloud.account_label({"email_masked": "s***@example.com"}) == "s***@example.com"
    assert cloud.account_label({}) == "your MyMemo account"
    assert cloud.member_fields({"display_name": " Sam\n\tLee ", "email_masked": 7, "plan": "x"}) == {
        "display_name": "Sam Lee"}


def test_the_callback_needs_a_state_this_process_started_recently(vault, fake, panel, monkeypatch):
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", fake.base)
    api, c = panel
    assert api.get("/cloud/callback?code=omc_x&state=nobody-started-this").status_code == 400
    assert api.get("/cloud/callback").status_code == 400

    url = api.post("/api/cloud/link", headers=H).json()["url"]
    state = dict(parse_qsl(urlsplit(url).query))["state"]
    r = api.get(f"/cloud/callback?error=access_denied&state={state}")
    assert r.status_code == 400 and "cancelled" in r.text
    assert api.get(f"/cloud/callback?code=omc_x&state={state}").status_code == 400  # used up by the denial

    url = api.post("/api/cloud/link", headers=H).json()["url"]
    back = fake.authorize(url).replace(f"iss={fake.base.replace(':', '%3A').replace('/', '%2F')}", "iss=https%3A%2F%2Fevil.example")
    assert api.get(callback_path(back)).status_code == 400

    url = api.post("/api/cloud/link", headers=H).json()["url"]
    parts = urlsplit(fake.authorize(url))
    without_iss = {k: v for k, v in parse_qsl(parts.query) if k != "iss"}  # RFC 9207: MyMemo always sends it
    r = api.get(f"{parts.path}?{urlencode(without_iss)}")
    assert r.status_code == 400 and "didn" in r.text

    c.linker.ttl = 0.0  # ten minutes have passed
    url = api.post("/api/cloud/link", headers=H).json()["url"]
    time.sleep(0.01)
    assert api.get(callback_path(fake.authorize(url))).status_code == 400
    assert fake.link_requests == [] and cloud.load_link() is None

    c.linker.ttl = cloud.LINK_TTL
    fake.link_response = {"error": "plan_required", "message": "OpenMyMemo Cloud is part of Pro and Ultra."}
    url = api.post("/api/cloud/link", headers=H).json()["url"]
    r = api.get(callback_path(fake.authorize(url)))
    assert r.status_code == 502 and cloud.load_link() is None
    assert api.get("/api/cloud", headers=H).json()["link_error"]


def test_the_panel_guard_still_applies_to_every_cloud_api(vault, panel):
    api, _ = panel
    for method, path in (("GET", "/api/cloud"), ("POST", "/api/cloud/link"), ("DELETE", "/api/cloud"),
                         ("POST", "/api/cloud/reconnect"), ("POST", "/api/cloud/apps/cloud-chatgpt/allow")):
        assert api.request(method, path).status_code == 403, path
        assert api.request(method, path, headers={**H, "Origin": "https://evil.example"}).status_code == 403, path
    evil = TestClient(create_api(vault, 8787), base_url="http://evil.example:8787")
    assert evil.get("/cloud/callback?state=x&code=y").status_code == 421  # the Host check stays
    assert api.post("/cloud/callback?state=x&code=y").status_code == 405  # only a GET navigation


def test_unlinking_tells_mymemo_deletes_cloud_json_and_revokes_the_web_apps(vault, fake, panel, clouds):
    api, c = panel
    linked(vault, fake.base)
    assert c.start()
    conn = fake.connection()
    call(conn, fixture("bridge/call.json"))
    assert [x.id for x in Connectors(vault).list()] == ["cloud-chatgpt"]

    out = api.delete("/api/cloud", headers=H).json()
    assert out["message"].startswith("Unlinked this computer from OpenMyMemo Cloud")
    assert fake.unlinks == [f"Bearer {fake.token}"] and cloud.load_link() is None
    assert conn.closed.wait(5) and conn.client_close == 1000 and len(fake.conns) == 1
    assert Connectors(vault).list() == []
    apps = out["cloud"]["apps"]
    assert out["cloud"]["linked"] is False and [(a["id"], a["revoked"]) for a in apps] == [("cloud-chatgpt", True)]

    # Allowing it again is an explicit re-grant.
    after = api.post("/api/cloud/apps/cloud-chatgpt/allow", headers=H).json()
    assert [(a["id"], a["revoked"], a["name"]) for a in after["apps"]] == [("cloud-chatgpt", False, "ChatGPT (web)")]
    assert api.post("/api/cloud/apps/cursor/allow", headers=H).status_code == 404


def test_unlinking_while_mymemo_is_unreachable_still_forgets_the_link(vault):
    linked(vault, "http://127.0.0.1:9")
    assert "couldn't be told" in cloud.unlink() and cloud.load_link() is None
    assert cloud.unlink() == "This computer isn't linked to OpenMyMemo Cloud."


def cloud_rows(vault) -> list[dict]:
    return [r for r in clients.catalog(vault) if r["id"] == clients.CLOUD]


def test_openmymemo_cloud_is_listed_until_hidden_and_always_while_linked(vault, fake, panel):
    api, _ = panel
    assert len(cloud_rows(vault)) == 1 and api.get("/api/cloud", headers=H).json()["shown"] is True
    assert not cloud.settings_path().exists()  # listed by default: nothing written, nothing sent anywhere
    assert sum(a["id"] == clients.CLOUD for a in api.get("/api/clients", headers=H).json()["apps"]) == 1
    out = api.patch("/api/cloud/settings", json={"show": False}, headers=H).json()
    assert out["shown"] is False and out["settings"]["show"] is False and cloud_rows(vault) == []
    assert all(a["id"] != clients.CLOUD for a in api.get("/api/clients", headers=H).json()["apps"])
    linked(vault, fake.base)  # linked from the CLI while hidden: listed, so it can be seen and unlinked
    assert len(cloud_rows(vault)) == 1 and api.get("/api/cloud", headers=H).json()["shown"] is True
    cloud.unlink()
    assert cloud_rows(vault) == []  # hidden again: the person's choice stays
    assert api.patch("/api/cloud/settings", json={"show": True}, headers=H).json()["shown"] is True
    assert len(cloud_rows(vault)) == 1
    assert stat.S_IMODE(cloud.settings_path().stat().st_mode) == 0o600


@pytest.mark.parametrize("old", [
    {"show": False, "service": None},  # what 0.2.8 and older wrote after any change while hidden, its default
    {"show": False, "service": "https://memo.example.org", "later": 1},
    {"show": True, "service": None},
    {"show": False},
])
def test_an_older_settings_files_show_false_isnt_a_hide(vault, monkeypatch, old):
    monkeypatch.delenv("OPENMYMEMO_CLOUD_URL")
    cloud.settings_path().parent.mkdir(parents=True, exist_ok=True)
    cloud.settings_path().write_text(json.dumps(old))
    assert cloud.load_settings()["show"] is True and cloud.shown() and len(cloud_rows(vault)) == 1
    assert cloud.service_url() == (old.get("service") or cloud.DEFAULT_SERVICE)  # the service chosen stays
    cloud.save_settings()  # only reading them: the file stays as it was
    assert json.loads(cloud.settings_path().read_text()) == old

    # Any change writes format 2, listed unless the person hides it now; a hide from now on is kept.
    cloud.save_settings(service="https://other.example.org")
    assert json.loads(cloud.settings_path().read_text()) == \
        {**old, "format": 2, "show": True, "service": "https://other.example.org"}
    cloud.save_settings(show=False)
    assert cloud.load_settings()["show"] is False and not cloud.shown() and cloud_rows(vault) == []
    cloud.save_settings(service="")
    assert json.loads(cloud.settings_path().read_text()) == {**old, "format": 2, "show": False, "service": None}
    assert not cloud.shown()


def test_a_newer_settings_format_keeps_its_hide(vault):
    cloud.settings_path().parent.mkdir(parents=True, exist_ok=True)
    cloud.settings_path().write_text('{"format": 3, "show": false, "service": null}')
    assert not cloud.shown()
    cloud.save_settings(show=True)
    assert json.loads(cloud.settings_path().read_text())["format"] == 3 and cloud.shown()


def test_a_new_install_signs_in_from_the_listed_row_with_no_settings_or_environment(vault, fake, panel, monkeypatch):
    """What a person gets after installing: no OPENMYMEMO_CLOUD_URL, no settings.json, MyMemo's own service (the fake
    stands in for it), and a plan with the live relay only, as MyMemo offers it at launch."""
    monkeypatch.delenv("OPENMYMEMO_CLOUD_URL")
    monkeypatch.setattr(cloud, "DEFAULT_SERVICE", fake.base)
    live_only = {"tier": "pro", "live": True, "queue": False}
    fake.link_response = {**fake.link_response, "plan": live_only}
    fake.welcome = {**fake.welcome, "plan": live_only, "queued": 0}
    api, _ = panel

    # Listed in Connectors, and nothing has talked to MyMemo yet.
    row = next(a for a in api.get("/api/clients", headers=H).json()["apps"] if a["id"] == clients.CLOUD)
    assert row["name"] == "OpenMyMemo Cloud" and row["method"] == "account" and not row["connected"]
    s = api.get("/api/cloud", headers=H).json()
    assert s["shown"] and not s["linked"] and s["service"] == fake.base and s["settings"]["service_from_env"] is None
    assert not fake.authorize_params and not fake.link_requests and not fake.handshakes

    # Sign in → the browser consents on MyMemo → /cloud/callback → POST /openmymemo/device/link → connected.
    url = api.post("/api/cloud/link", headers=H).json()["url"]
    assert url.startswith(f"{fake.base}/openmymemo/oauth/authorize?")
    r = api.get(callback_path(fake.authorize(url)))
    assert r.status_code == 200 and "Connectors → OpenMyMemo Cloud → Unlink" in r.text
    link = cloud.load_link()
    assert link.service == fake.base and link.plan == live_only and len(fake.link_requests) == 1
    conn = fake.connection()
    assert "features" not in conn.hello  # the offline queue isn't offered: the service queues nothing for us
    status = wait_until(lambda: (s := api.get("/api/cloud", headers=H).json())["online"] and s)
    assert status["plan"] == live_only and status["queued"] is None and status["queue_enabled"] is False
    assert status["endpoint"] == f"{fake.base}/openmymemo/mcp"
    assert next(a for a in api.get("/api/clients", headers=H).json()["apps"] if a["id"] == clients.CLOUD)["connected"]
    assert not cloud.settings_path().exists()  # signing in changes no preference


def test_choosing_the_service(vault, fake, panel, monkeypatch):
    api, c = panel
    monkeypatch.delenv("OPENMYMEMO_CLOUD_URL")
    s = api.get("/api/cloud", headers=H).json()
    assert (s["service"], s["settings"]["service"], s["settings"]["service_from_env"]) == (cloud.DEFAULT_SERVICE, "", None)
    for bad in ("http://example.com", "ftp://example.com", "https://user@example.com", "https://example.com/?x=1",
                "https://example.com/#x", "example.com", "https://", "https://exa mple.com"):
        r = api.patch("/api/cloud/settings", json={"service": bad}, headers=H)
        assert r.status_code == 400, bad
    s = api.patch("/api/cloud/settings", json={"service": "https://memo.example.org/"}, headers=H).json()
    assert (s["service"], s["endpoint"], s["settings"]["service"]) == \
        ("https://memo.example.org", "https://memo.example.org/openmymemo/mcp", "https://memo.example.org")
    assert c.linker.begin(vault.root, c.redirect_uri).startswith("https://memo.example.org/openmymemo/oauth/authorize?")
    assert api.patch("/api/cloud/settings", json={"service": cloud.DEFAULT_SERVICE + "/"}, headers=H).json()[
        "settings"]["service"] == ""  # MyMemo's own is stored as no choice
    api.patch("/api/cloud/settings", json={"service": fake.base}, headers=H)  # http on this computer is fine
    assert cloud.service_url() == fake.base

    # A linked computer keeps its service: choosing another one waits until it is unlinked.
    linked(vault, fake.base)
    r = api.patch("/api/cloud/settings", json={"service": ""}, headers=H)
    assert r.status_code == 409 and "Unlink" in r.json()["detail"] and cloud.service_url() == fake.base
    assert api.patch("/api/cloud/settings", json={"service": fake.base, "show": True}, headers=H).status_code == 200
    cloud.unlink()
    assert api.patch("/api/cloud/settings", json={"service": ""}, headers=H).json()["service"] == cloud.DEFAULT_SERVICE

    # OPENMYMEMO_CLOUD_URL decides it when set.
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", fake.base)
    s = api.get("/api/cloud", headers=H).json()
    assert s["service"] == fake.base and s["settings"]["service_from_env"] == fake.base
    assert api.patch("/api/cloud/settings", json={"service": "https://memo.example.org"}, headers=H).status_code == 409


def test_unusable_settings_fall_back_to_listed_and_mymemos_own(vault, monkeypatch):
    monkeypatch.delenv("OPENMYMEMO_CLOUD_URL")
    cloud.settings_path().parent.mkdir(parents=True, exist_ok=True)
    for text in ("not json", "[]", '{"show": "yes", "service": "http://example.com"}',
                 '{"format": 2, "show": "no", "service": "ftp://example.com"}', '{"format": "2", "show": false}'):
        cloud.settings_path().write_text(text)
        assert (cloud.load_settings()["show"], cloud.service_url(), cloud.shown()) == (True, cloud.DEFAULT_SERVICE, True)
    cloud.settings_path().write_text('{"format": 2, "show": true, "later": 1}')
    cloud.save_settings(service="https://memo.example.org")
    assert json.loads(cloud.settings_path().read_text()) == \
        {"format": 2, "show": True, "later": 1, "service": "https://memo.example.org"}  # keys a later version adds are kept


def test_the_catalog_lists_mymemo_cloud(vault, fake, panel):
    api, _ = panel
    cloud.save_settings(show=True)
    row = next(r for r in clients.catalog(vault) if r["id"] == clients.CLOUD)
    assert row["method"] == "account" and row["description"] == "Use your memory in ChatGPT and claude.ai on the web"
    assert row["connected"] is False
    assert clients.connect(vault, clients.CLOUD).status == "manual"
    linked(vault, fake.base)
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    row = next(r for r in clients.catalog(vault) if r["id"] == clients.CLOUD)
    assert row["connected"] is True and row["via"] == "account"
    listed = api.get("/api/clients", headers=H).json()
    assert listed["custom"] == []  # web apps are listed under OpenMyMemo Cloud, not as apps added by name
    assert any(a["id"] == clients.CLOUD for a in listed["apps"])


# ---- relayed calls ----------------------------------------------------------------------------------------------


def test_relayed_calls_run_the_local_tools_as_the_web_apps_connector(vault, fake, clouds):
    m = vault.add("how-to-work-with-me", "Prefers short answers")
    vault.add("health", "Allergic to penicillin")  # a private topic: not for web apps by default
    c, conn = online(vault, fake, clouds)

    ctx = call(conn, fixture("bridge/call.json"))
    assert set(ctx) == {"type", "id", "result"} and ctx["id"] == fixture("bridge/call.json")["id"]
    assert set(ctx["result"]) == set(fixture("bridge/result_ok.json")["result"])  # content, structuredContent, isError
    text = ctx["result"]["content"][0]["text"]
    assert "Prefers short answers" in text and "penicillin" not in text and ctx["result"]["isError"] is False
    chatgpt = Connectors(vault).get("cloud-chatgpt")
    assert (chatgpt.name, chatgpt.kind, chatgpt.write, chatgpt.read_topics, chatgpt.include_private) == \
        ("ChatGPT (web)", "cloud", "propose", "all", False)
    assert vault.activity("cloud-chatgpt")[0]["action"] == "context"

    prop = call(conn, fixture("bridge/call_propose.json"))["result"]
    out = prop["structuredContent"]
    assert prop["isError"] is False and out["status"] == "applied" and "review_url" not in out
    p = Proposals(vault).get(out["proposal_id"])
    assert (p.connector_id, p.connector_name, p.idempotency_key) == ("cloud-claude", "Claude (web)", "claude-7f3e")
    assert p.content == "Prefers answers with the conclusion first." and p.status == "applied"

    found = call(conn, {"type": "call", "id": "s1", "client": {"slug": "chatgpt", "name": "ChatGPT", "verified": True},
                        "params": {"name": "memory_search", "arguments": {"query": "short"}}, "deadline_ms": 20000})
    assert found["result"]["structuredContent"]["results"][0]["id"] == m.id
    status = c.status()
    # welcome.json says 1 is queued, but this computer didn't ask for the queue: nothing about it is shown.
    assert status["last_call_at"] and WELCOME["queued"] == 1 and status["queued"] is None
    assert status["queue_enabled"] is False


def test_a_service_that_reports_a_queue_this_computer_didnt_ask_for_shows_none(vault, fake, clouds):
    """At launch the computer advertises no `queue` (cloud.FEATURES), so whatever the service says about one, its
    count and its plan mode, nothing promises it: not the panel's status, nor the CLI's plan line."""
    fake.welcome = {**fake.welcome, "queued": 3, "plan": {"tier": "ultra", "live": True, "queue": True}}
    c, conn = online(vault, fake, clouds)
    assert "features" not in conn.hello and not cloud.queue_enabled()
    status = wait_until(lambda: (s := c.status())["online"] and s)
    assert status["queued"] is None and status["queue_enabled"] is False
    assert status["plan"]["queue"] is True  # kept as MyMemo said it; the panel goes by queue_enabled
    assert cloud.plan_label(status["plan"]) == "Ultra · live relay"
    assert cloud.plan_label({"tier": "pro", "live": True, "queue": True}) == "Pro · live relay"


def test_the_queue_shows_once_this_computer_asks_for_it(vault, fake, clouds, queue_on):
    fake.welcome = {**fake.welcome, "queued": 3}
    c, conn = online(vault, fake, clouds)
    assert conn.hello["features"] == ["queue"]
    status = c.status()
    assert status["queued"] == 3 and status["queue_enabled"] is True
    assert cloud.plan_label(status["plan"]) == "Ultra · live relay and offline queue"


def test_a_revoked_web_app_is_told_so(vault, fake, clouds):
    _, conn = online(vault, fake, clouds)
    call(conn, {**fixture("bridge/call_propose.json"), "id": "first"})
    Connectors(vault).remove("cloud-claude")
    assert call(conn, fixture("bridge/call_propose.json")) == fixture("bridge/result_error.json")
    assert Connectors(vault).list() == []  # not silently registered again


def test_cloud_ids_are_kept_for_web_apps(vault, panel):
    from openmymemo.vault import VaultError

    cons = Connectors(vault)
    mine, _ = cons.create("Cloud Claude", "http")  # what "Add another app" makes of that name
    assert mine.id == "app-cloud-claude"
    with pytest.raises(VaultError):
        cons.create("Claude", "stdio", connector_id="cloud-claude")
    with pytest.raises(VaultError):
        cons.create("Claude", "cloud")  # a web app's id always starts with cloud-
    assert cons.ensure("cloud-claude", "Claude (web)", kind="cloud").kind == "cloud"
    api, _ = panel
    assert api.post("/api/connectors", headers=H, json={"name": "Cloud thing", "kind": "cloud"}).status_code == 400


def test_a_web_app_never_answers_as_an_app_of_another_kind_with_its_id(vault, fake, clouds):
    from openmymemo.connectors import Connector

    # A folder from before cloud- was kept: the person's own HTTP app happens to be called cloud-claude.
    cons = Connectors(vault)
    cons._save([Connector(id="cloud-claude", name="Cloud Claude", kind="http", include_private=True, write="auto",
                          token_hash="0" * 64, created_at="2026-10-01T00:00:00+00:00")])
    secret = vault.add("identity", "Secret thing in a private memory", private=True)
    _, conn = online(vault, fake, clouds)
    client = {"slug": "claude", "name": "Claude", "verified": True}
    got = call(conn, {"type": "call", "id": "g1", "client": client,
                      "params": {"name": "memory_get", "arguments": {"ids": [secret.id]}}})
    assert got == {"type": "result", "id": "g1",
                   "error": {"code": "revoked", "message": "The person revoked Claude (web)'s access in OpenMyMemo."}}
    wrote = call(conn, {**fixture("bridge/call_propose.json"), "id": "g2"})
    assert wrote["error"]["code"] == "revoked" and Proposals(vault).list() == []
    assert vault.activity("cloud-claude") == []
    assert cloud.revoke_web_apps(vault) == [] and cons.get("cloud-claude").kind == "http"  # left alone


def test_bad_calls_and_tool_errors(vault, fake, clouds):
    _, conn = online(vault, fake, clouds)
    client = {"slug": "chatgpt", "name": "ChatGPT", "verified": True}
    unknown = call(conn, {"type": "call", "id": "u1", "client": client, "params": {"name": "rm_rf", "arguments": {}}})
    assert unknown["error"]["code"] == "bad_request"
    no_client = call(conn, {"type": "call", "id": "u2", "params": {"name": "memory_context", "arguments": {}}})
    assert no_client["error"]["code"] == "bad_request"
    bad_slug = call(conn, {"type": "call", "id": "u3", "client": {"slug": "../x"}, "params": {"name": "memory_context"}})
    assert bad_slug["error"]["code"] == "bad_request"

    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    Connectors(vault).update("cloud-chatgpt", write="none")
    ro = call(conn, {"type": "call", "id": "u4", "client": client,
                     "params": {"name": "memory_propose", "arguments": {"op": "add", "content": "x y z"}}})
    assert ro["result"]["isError"] is True and "read-only" in ro["result"]["content"][0]["text"]
    invalid = call(conn, {"type": "call", "id": "u5", "client": client,
                          "params": {"name": "memory_propose", "arguments": {"op": "explode"}}})
    assert invalid["result"]["isError"] is True


def test_pings_are_answered(vault, fake, clouds):
    _, conn = online(vault, fake, clouds)
    conn.send(fixture("bridge/ping.json"))
    assert conn.wait_frame(lambda f: f["type"] == "pong") == fixture("bridge/pong.json")


# ---- staying connected ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", [1000, 1001, 4400, 4408, 4429])
def test_it_reconnects_with_backoff_after(code, vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    conn.close(code)
    second = fake.connection(1)
    assert second is not conn and second.hello["key_id"] == VECTOR["key_id"]
    wait_until(lambda: c.status()["online"])
    assert cloud.load_link() is not None


def test_backoff_grows_while_refused_and_starts_over_after_a_good_connection(vault, fake, clouds, monkeypatch):
    attempts: list[int] = []
    real = cloud.backoff_delay

    def recorded(attempt, *args, **kwargs):
        attempts.append(attempt)
        return real(attempt, *args, **kwargs)

    monkeypatch.setattr(cloud, "backoff_delay", recorded)
    fake.close_after_hello = 4429  # too many connections, again and again
    linked(vault, fake.base)
    c = Cloud(vault, 8787, Timing(backoff_base=0.01, backoff_cap=0.04, reset_after=0.3, welcome_timeout=5))
    clouds.append(c)
    c.start()
    wait_until(lambda: len(attempts) >= 4)
    assert attempts[:4] == [0, 1, 2, 3]
    fake.close_after_hello = None
    wait_until(lambda: c.status()["online"])
    conn = fake.connection()
    time.sleep(0.4)  # connected longer than reset_after
    seen = len(attempts)
    conn.close(1000)
    wait_until(lambda: len(attempts) > seen)
    assert attempts[seen] == 0


def test_a_result_too_large_to_relay_is_replaced_by_a_message(vault, fake, clouds):
    fake.welcome = {**WELCOME, "limits": {**WELCOME["limits"], "result_max_bytes": 300}}
    vault.add("work", "Works on " + "a very long project name " * 20)
    _, conn = online(vault, fake, clouds)
    r = call(conn, fixture("bridge/call.json"))["result"]
    assert r == {"content": [{"type": "text", "text": cloud.TOO_LARGE}], "isError": True}


def test_a_service_restart_reconnects_after_retry_after_ms(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    conn.send({**fixture("bridge/bye.json"), "retry_after_ms": 400})
    time.sleep(0.1)
    closed_at = time.monotonic()
    conn.close(1012, "service restart")
    second = fake.connection(1)
    assert time.monotonic() - closed_at >= 0.35 and second is not conn


def test_4401_means_unlinked_elsewhere(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    call(conn, fixture("bridge/call.json"))
    fake.device_known = False  # unlinked on the website
    conn.close(4401, "unlinked")
    wait_until(lambda: c.status()["linked"] is False)
    assert cloud.load_link() is None and Connectors(vault).list() == []  # web apps kept as revoked tombstones
    time.sleep(0.3)
    assert len(fake.conns) == 1 and not c.running


def test_4409_means_another_openmymemo_took_over(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    conn.close(4409, "replaced")
    wait_until(lambda: c.status()["reason"] == "replaced")
    time.sleep(0.3)
    assert len(fake.conns) == 1 and cloud.load_link() is not None
    c.reconnect()  # the panel's "Use this computer" takes it back
    fake.connection(1)


def test_linking_again_from_another_process_replaces_the_connection_and_keeps_the_web_apps(vault, fake, clouds,
                                                                                            monkeypatch):
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", fake.base)
    c, conn = online(vault, fake, clouds)
    call(conn, fixture("bridge/call.json"))
    old = cloud.load_link()

    def browser(url: str) -> None:  # `openmymemo cloud link` without a panel: its own listener takes the callback
        back = fake.authorize(url)
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()

    outcome = cloud.link_with_listener(vault, browser)
    assert outcome.link is not None and outcome.link.device_id != old.device_id
    assert conn.closed.wait(5) and conn.client_close == 4409  # the service replaced the old device
    assert cloud.load_link().device_id == outcome.link.device_id
    wait_until(lambda: c.status()["online"])  # the running panel picks up the new link
    new = fake.connection()
    assert new is not conn and new.headers["authorization"] == f"Bearer {outcome.link.device_token}"
    assert [x.id for x in Connectors(vault).list()] == ["cloud-chatgpt"]  # nothing revoked
    assert call(new, fixture("bridge/call.json"))["result"]["isError"] is False


def test_the_cli_finds_a_panel_for_this_folder_on_the_mac_apps_ports(vault, tmp_path, monkeypatch):
    import re
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from openmymemo import cli as cli_module

    core = (Path(__file__).parent.parent / "desktop/src-tauri/src/core.rs").read_text()
    lo, hi = re.search(r"pub const PORTS: Range<u16> = (\d+)\.\.(\d+);", core).groups()
    assert cli_module.PANEL_PORTS == range(int(lo), int(hi))  # the same ports as the Mac app

    served = {"vault": str(tmp_path / "another-folder")}

    class State(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps(served).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), State)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        nothing = s.getsockname()[1]
    try:
        monkeypatch.setattr(cli_module, "PANEL_PORTS", range(port, port + 1))
        assert cli_module._panel_for(vault.root, nothing) is None  # a panel for another folder isn't used
        served["vault"] = str(vault.root)
        assert cli_module._panel_for(vault.root, nothing) == f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def test_4410_asks_to_link_again_and_keeps_the_link(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    conn.close(4410, "key_id mismatch")
    wait_until(lambda: c.status()["reason"] == "key")
    time.sleep(0.3)
    assert len(fake.conns) == 1 and cloud.load_link() is not None


def test_4403_waits_for_the_plan_until_the_panel_asks(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    fake.close_after_hello = 4403
    conn.close(4403, "plan")
    wait_until(lambda: c.status()["reason"] == "plan")
    time.sleep(0.3)
    assert len(fake.conns) == 1  # nothing before the hourly retry
    fake.close_after_hello = None
    c.reconnect()
    fake.connection(1)
    wait_until(lambda: c.status()["online"])


def test_a_refused_handshake_unlinks_only_when_mymemo_says_the_device_is_gone(vault, fake, clouds):
    linked(vault, fake.base)
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    fake.device_known = False  # unlinked on the website while this computer was off
    c = Cloud(vault, 8787, FAST)
    clouds.append(c)
    c.start()
    wait_until(lambda: not c.status()["linked"])
    assert len(fake.handshakes) == 1 and fake.device_checks == [f"Bearer {fake.token}"]
    assert cloud.load_link() is None and Connectors(vault).list() == []  # the web apps are revoked, as for 4401


@pytest.mark.parametrize("why", ["live", "firewall", "redirect"])
def test_a_refusal_mymemo_doesnt_confirm_keeps_the_link(why, vault, fake, clouds):
    linked(vault, fake.base)
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    fake.refuse_upgrades = 10**6  # live: the device exists but the upgrade is turned away (a deploy, a flag)
    fake.waf = why == "firewall"  # an HTML 403 for everything, the device check included
    fake.device_redirect = why == "redirect"  # the check is redirected to a page that says invalid_token
    c = Cloud(vault, 8787, FAST)
    clouds.append(c)
    c.start()
    wait_until(lambda: c.status()["reason"] == "refused" and len(fake.handshakes) >= 3)
    assert cloud.load_link() is not None and [x.id for x in Connectors(vault).list()] == ["cloud-chatgpt"]
    assert c.status()["linked"] and not c.status()["online"]
    fake.refuse_upgrades, fake.waf = 0, False
    c.reconnect()  # the panel's "Try again"
    wait_until(lambda: c.status()["online"])


@pytest.mark.parametrize("status", [429, 503])
def test_a_busy_service_is_given_the_time_it_asks_for(status, vault, fake, clouds):
    linked(vault, fake.base)
    fake.busy = (status, "1")
    c = Cloud(vault, 8787, FAST)
    clouds.append(c)
    c.start()
    wait_until(lambda: len(fake.handshake_times) >= 2)
    # Measured where the requests arrive, not by polling for them: FAST alone backs off in milliseconds.
    assert fake.handshake_times[1] - fake.handshake_times[0] >= 0.95
    assert cloud.load_link() is not None and c.status()["reason"] == "offline"  # never taken as a refusal
    fake.busy = None
    c.reconnect()
    wait_until(lambda: c.status()["online"])


def test_retry_after_reads_seconds_and_caps_them():
    class R:
        def __init__(self, v):
            self.headers = {"Retry-After": v} if v is not None else {}
    assert [cloud.retry_after(R(v)) for v in ("2", "0", "-5", "86400", "Wed, 21 Oct 2026 07:28:00 GMT", None)] == \
        [2.0, 0.0, 0.0, cloud.RETRY_AFTER_CAP, 0.0, 0.0]
    assert cloud.retry_after(None) == 0.0


def test_mymemo_must_say_invalid_token_itself():
    unreachable = Link(service="http://127.0.0.1:9", device_id="d", device_token="omd_x", private_key="", key_id="",
                       vault="/nowhere")
    assert cloud.device_gone(unreachable) is False


def test_4401_without_mymemo_confirming_keeps_the_link(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    call(conn, fixture("bridge/call.json"))
    conn.close(4401, "unlinked")  # but GET /openmymemo/device still knows this computer
    wait_until(lambda: c.status()["reason"] == "refused" or c.status()["online"])
    assert cloud.load_link() is not None and [x.id for x in Connectors(vault).list()] == ["cloud-chatgpt"]
    fake.connection(1)


def test_a_service_that_echoes_no_subprotocol_is_fine(vault, fake, clouds):
    fake.choose_subprotocol = False  # handshake.json names only the Authorization header
    _, conn = online(vault, fake, clouds)
    assert conn.headers["sec-websocket-protocol"] == cloud.SUBPROTOCOL
    assert call(conn, fixture("bridge/call.json"))["result"]["isError"] is False


def test_closing_says_1000(vault, fake, clouds):
    c, conn = online(vault, fake, clouds)
    c.stop()
    assert conn.closed.wait(5) and conn.client_close == 1000
    assert c.status()["state"] == "stopped" and len(fake.conns) == 1


def test_only_the_linked_folder_connects(vault, fake, clouds, tmp_path):
    from openmymemo.vault import Vault

    linked(vault, fake.base)
    other = Vault.create(tmp_path / "other")
    c = Cloud(other, 8787, FAST)
    clouds.append(c)
    assert c.start() is False
    s = c.status()
    assert s["linked"] and s["reason"] == "folder" and not s["this_folder"] and fake.handshakes == []


def test_the_panel_server_connects_once_its_port_is_bound_and_says_1000_when_it_stops(vault, fake):
    import socket

    from openmymemo.web import panel_server

    linked(vault, fake.base)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = panel_server(vault, port)
    server.cloud.timing = FAST
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    conn = fake.connection()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/cloud", headers=H)
    status = wait_until(lambda: (s := json.load(urllib.request.urlopen(req)))["online"] and s)
    assert status["account"]["display_name"] == "Sam"
    server.should_exit = True
    thread.join(10)
    assert conn.closed.wait(5) and conn.client_close == 1000


# ---- the offline queue (phase 2) ------------------------------------------------------------------------------


def seal(plaintext: dict | bytes, qid: str) -> str:
    """What the service does with a memory_propose while the computer is offline (hpke_vector.json's suite)."""
    from cryptography.hazmat.primitives import hpke
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    suite = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
    key = X25519PublicKey.from_public_bytes(cloud.unb64u(VECTOR["public_key"]))
    info = b"openmymemo-queue/v1|" + VECTOR["device_id"].encode() + b"|" + qid.encode()
    raw = plaintext if isinstance(plaintext, bytes) else json.dumps(plaintext).encode()
    return cloud.b64u(suite.encrypt(raw, key, info=info))


def item(qid: str, plaintext: dict | bytes | None = None, **extra) -> dict:
    frame = {**fixture("bridge/item.json"), "qid": qid, **extra}
    if plaintext is not None:
        frame["ct"] = seal(plaintext, qid)
    return frame


def ack(conn, qid: str) -> dict:
    return conn.wait_frame(lambda f: f.get("type") == "ack" and f.get("qid") == qid)


def test_the_hpke_vector_opens():
    pt = cloud.open_item(VECTOR["private_key"], VECTOR["device_id"], VECTOR["qid"], VECTOR["ct"])
    assert pt == cloud.unb64u(VECTOR["plaintext_b64u"]) and json.loads(pt) == VECTOR["plaintext"]
    assert VECTOR["info"].encode() == cloud.QUEUE_INFO + VECTOR["device_id"].encode() + b"|" + VECTOR["qid"].encode()
    with pytest.raises(cloud.Undecryptable):  # bound to the row: another qid doesn't open it
        cloud.open_item(VECTOR["private_key"], VECTOR["device_id"], "another-qid", VECTOR["ct"])
    with pytest.raises(cloud.Undecryptable):  # and to the device
        cloud.open_item(VECTOR["private_key"], "another-device", VECTOR["qid"], VECTOR["ct"])


QUEUE_NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)  # the fixtures' items were queued the evening before


@pytest.fixture
def queue_clock(monkeypatch):
    """Queued times are kept within the last 31 days, so tests about the fixtures' times run on their day."""
    monkeypatch.setattr(cloud, "_now", lambda: QUEUE_NOW)
    return QUEUE_NOW


@pytest.fixture
def queue_on(monkeypatch):
    """The offline queue as it works once this computer asks for it (`cloud.FEATURES`; not offered at launch)."""
    monkeypatch.setattr(cloud, "FEATURES", ("queue",))


def test_queued_changes_this_computer_didnt_ask_for_are_left_with_the_service(vault, fake, clouds, queue_clock):
    """At launch (no `queue` in hello): an `item` the service sends anyway is neither filed nor acknowledged, so it
    stays with the service, which sends it again once this computer takes the queue."""
    c, conn = online(vault, fake, clouds)
    conn.send(fixture("bridge/item.json"))
    assert call(conn, fixture("bridge/call.json"))["result"]["isError"] is False  # handled after the item
    time.sleep(0.2)
    assert not [f for f in conn.frames if f.get("type") == "ack"]
    assert Proposals(vault).list(connector_id="cloud-chatgpt") == [] and c.status()["online"]


def test_queued_proposals_are_filed_as_their_web_app_with_their_time_and_acknowledged(vault, fake, clouds, queue_clock,
                                                                                    queue_on):
    c, conn = online(vault, fake, clouds)
    assert conn.hello["features"] == ["queue"] and c.status()["queue_enabled"]
    assert c.status()["queued"] == 1  # welcome.json
    qid = VECTOR["qid"]
    conn.send(fixture("bridge/item.json"))
    assert ack(conn, qid) == fixture("bridge/ack.json")  # {"type": "ack", "qid": …, "outcome": "stored"}
    p = Proposals(vault).list(connector_id="cloud-chatgpt")[0]
    assert (p.connector_name, p.op, p.content) == ("ChatGPT (web)", "add", "Is training for a half marathon in March.")
    assert p.created_at == "2026-10-12T21:04:05+00:00" and p.queue_id == qid
    assert p.idempotency_key == VECTOR["plaintext"]["params"]["arguments"]["idempotency_key"]  # the AI's own
    assert p.reason == "Mentioned while planning a week" and p.status == "applied"
    assert Connectors(vault).get("cloud-chatgpt").kind == "cloud"
    assert c.status()["queued"] == 0

    conn.frames.clear()
    conn.send(fixture("bridge/item.json"))  # sent again after a reconnect: at-least-once delivery
    assert ack(conn, qid)["outcome"] == "duplicate"
    assert len(Proposals(vault).list(connector_id="cloud-chatgpt")) == 1


def test_queued_items_that_cant_be_opened_or_filed(vault, fake, clouds, queue_on):
    _, conn = online(vault, fake, clouds)
    good = VECTOR["plaintext"]
    broken = fixture("bridge/item.json")
    raw = bytearray(cloud.unb64u(broken["ct"]))
    raw[-1] ^= 1
    cases = {
        "q-flipped": ({**broken, "qid": "q-flipped", "ct": cloud.b64u(bytes(raw))}, "undecryptable"),
        "q-other-row": ({**broken, "qid": "q-other-row"}, "undecryptable"),  # sealed for another qid
        "q-key": (item("q-key", {**good, "qid": "q-key"}, key_id="0000000000000000"), "undecryptable"),
        "q-garbage": ({**broken, "qid": "q-garbage", "ct": "%%%"}, "undecryptable"),
        "q-inner": (item("q-inner", {**good, "qid": "someone-else"}), "rejected"),
        "q-tool": (item("q-tool", {**good, "qid": "q-tool", "params": {"name": "memory_search", "arguments": {}}}),
                   "rejected"),
        "q-bad": (item("q-bad", {**good, "qid": "q-bad", "params": {"name": "memory_propose",
                                                                    "arguments": {"op": "explode"}}}), "rejected"),
        "q-deep": (item("q-deep", b"[" * 12000), "rejected"),  # json.loads raises RecursionError: still acked
        "q-text": (item("q-text", b"not json at all"), "rejected"),
        "q-list": (item("q-list", b"[1, 2, 3]"), "rejected"),
        "q-op": (item("q-op", {**good, "qid": "q-op", "params": {"name": "memory_propose",
                                                                  "arguments": {"op": ["add"], "content": 7}}}),
                 "rejected"),
    }
    for frame, _ in cases.values():
        conn.send(frame)
    for qid, (_, outcome) in cases.items():
        assert ack(conn, qid)["outcome"] == outcome, qid
    assert Proposals(vault).list() == []

    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    Connectors(vault).remove("cloud-chatgpt")
    conn.send(item("q-revoked", {**good, "qid": "q-revoked"}))
    assert ack(conn, "q-revoked")["outcome"] == "rejected"
    assert Proposals(vault).list() == [] and Connectors(vault).list() == []
    refused = [a for a in vault.activity() if a.get("result") == "rejected"]
    assert refused[0]["queue_id"] == "q-revoked" and refused[0]["connector"] == "cloud-chatgpt"


def test_a_queued_write_retried_live_is_one_proposal_and_traceable(vault, fake, clouds, queue_clock, queue_on):
    _, conn = online(vault, fake, clouds)
    good = VECTOR["plaintext"]
    args = {"op": "add", "content": "Keeps bees on the roof.", "topic": "goals", "idempotency_key": "ai-k1"}
    conn.send(item("q-k1", {**good, "qid": "q-k1", "params": {"name": "memory_propose", "arguments": args}}))
    assert ack(conn, "q-k1")["outcome"] == "stored"
    filed = Proposals(vault).list(connector_id="cloud-chatgpt")
    assert len(filed) == 1 and filed[0].queue_id == "q-k1" and filed[0].idempotency_key == "ai-k1"

    relayed = {**fixture("bridge/call.json"), "client": {"slug": "chatgpt", "name": "ChatGPT", "verified": True}}
    # ChatGPT retries the same call live once the computer is back: the same proposal, not a second one.
    again = call(conn, {**relayed, "id": "c-retry", "params": {"name": "memory_propose", "arguments": args}})
    assert again["result"]["structuredContent"]["proposal_id"] == filed[0].id
    assert len(Proposals(vault).list(connector_id="cloud-chatgpt")) == 1
    # memory_proposals matches the queued receipt to the proposal.
    rows = call(conn, {**relayed, "id": "c-list", "params": {"name": "memory_proposals", "arguments": {}}})
    rows = rows["result"]["structuredContent"]["proposals"]
    assert [(r["proposal_id"], r["queue_id"]) for r in rows] == [(filed[0].id, "q-k1")]

    # An item without a key of its own is filed under the queue id, so a replay is a duplicate.
    plain = {"op": "add", "content": "Likes rainy mornings.", "topic": "goals"}
    frame = item("q-plain", {**good, "qid": "q-plain", "params": {"name": "memory_propose", "arguments": plain}})
    conn.send(frame)
    assert ack(conn, "q-plain")["outcome"] == "stored"
    conn.frames.clear()
    conn.send(frame)
    assert ack(conn, "q-plain")["outcome"] == "duplicate"
    assert next(p for p in Proposals(vault).list() if p.queue_id == "q-plain").idempotency_key == "q-plain"


def test_a_passing_failure_stops_the_queue_until_it_is_sent_again_in_order(vault, fake, clouds, queue_clock, monkeypatch,
                                                                           queue_on):
    m = vault.add("how-to-work-with-me", "Drinks coffee.")
    _, conn = online(vault, fake, clouds)
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    Connectors(vault).update("cloud-chatgpt", write="auto")
    good = VECTOR["plaintext"]

    def update(qid, text):
        args = {"op": "update", "memory_id": m.id, "content": text}
        return item(qid, {**good, "qid": qid, "params": {"name": "memory_propose", "arguments": args}})

    real, failed = Proposals.propose, []
    both_sent = threading.Event()  # the failure, and the hang-up after it, wait until q-new is on its way too

    def once(self, *a, **k):
        if k.get("queue_id") == "q-old" and not failed:
            failed.append(True)
            assert both_sent.wait(5)
            raise OSError(28, "No space left on device")
        return real(self, *a, **k)

    monkeypatch.setattr(Proposals, "propose", once)
    conn.send(update("q-old", "Drinks black tea."))
    conn.send(update("q-new", "Drinks green tea."))
    both_sent.set()
    assert conn.closed.wait(5) and conn.client_close == 1011  # hung up rather than filing q-new first
    assert not [f for f in conn.frames if f.get("type") == "ack"] and vault.get(m.id).content == "Drinks coffee."

    again = fake.connection(1)  # the service sends the queue again, in order
    again.send(update("q-old", "Drinks black tea."))
    again.send(update("q-new", "Drinks green tea."))
    assert ack(again, "q-old")["outcome"] == ack(again, "q-new")["outcome"] == "stored"
    assert vault.get(m.id).content == "Drinks green tea."


@pytest.mark.parametrize("live_first", [True, False])
def test_the_queued_receipt_is_kept_whichever_copy_arrives_first(live_first, vault, fake, clouds, queue_clock,
                                                                 queue_on):
    _, conn = online(vault, fake, clouds)
    args = {"op": "add", "content": "Keeps bees on the roof.", "topic": "goals", "idempotency_key": "retry-key"}
    relayed = {**fixture("bridge/call.json"), "client": {"slug": "chatgpt", "name": "ChatGPT", "verified": True}}
    queued = item("q-race", {**VECTOR["plaintext"], "qid": "q-race",
                             "params": {"name": "memory_propose", "arguments": args}})
    live = {**relayed, "id": "c-live", "params": {"name": "memory_propose", "arguments": args}}
    if live_first:
        call(conn, live)
        conn.send(queued)
        assert ack(conn, "q-race")["outcome"] == "duplicate"
    else:
        conn.send(queued)
        assert ack(conn, "q-race")["outcome"] == "stored"
        call(conn, live)
    [p] = Proposals(vault).list(connector_id="cloud-chatgpt")
    assert p.queue_id == "q-race" and p.idempotency_key == "retry-key"


def test_a_long_idempotency_key_keeps_its_identity(vault, fake, clouds, queue_clock, queue_on):
    vault.update_settings(require_approval=True)
    _, conn = online(vault, fake, clouds)
    args = {"op": "add", "content": "Keeps bees on the roof.", "topic": "goals", "idempotency_key": "a" * 201}
    conn.send(item("q-long", {**VECTOR["plaintext"], "qid": "q-long",
                              "params": {"name": "memory_propose", "arguments": args}}))
    assert ack(conn, "q-long")["outcome"] == "stored"
    [p] = Proposals(vault).list(connector_id="cloud-chatgpt")
    assert p.idempotency_key == "a" * 201 and p.status == "pending"
    Proposals(vault).reject(p.id)
    relayed = {**fixture("bridge/call.json"), "client": {"slug": "chatgpt", "name": "ChatGPT", "verified": True}}
    again = call(conn, {**relayed, "id": "c-long", "params": {"name": "memory_propose", "arguments": args}})
    assert again["result"]["structuredContent"]["proposal_id"] == p.id  # the rejected one, not a second proposal
    assert len(Proposals(vault).list(connector_id="cloud-chatgpt")) == 1


def test_text_no_file_can_hold_is_rejected_and_the_queue_goes_on(vault, fake, clouds, queue_clock, queue_on):
    _, conn = online(vault, fake, clouds)
    good = VECTOR["plaintext"]
    bad = json.dumps({**good, "qid": "q-bad", "params": {"name": "memory_propose", "arguments": {
        "op": "add", "content": "Invalid \ud800", "topic": "goals"}}}).encode()
    assert b"\\ud800" in bad  # sealed exactly as JSON allows it
    conn.send(item("q-bad", bad))
    conn.send(item("q-next", {**good, "qid": "q-next", "params": {"name": "memory_propose", "arguments": {
        "op": "add", "content": "Likes rainy mornings.", "topic": "goals"}}}))
    assert ack(conn, "q-bad")["outcome"] == "rejected" and ack(conn, "q-next")["outcome"] == "stored"
    assert not conn.closed.is_set() and [p.content for p in Proposals(vault).list()] == ["Likes rainy mornings."]


def test_queued_items_are_filed_in_the_order_they_arrive(vault, fake, clouds, queue_on):
    _, conn = online(vault, fake, clouds)
    good = VECTOR["plaintext"]
    texts = ["Is learning Japanese", "Plays the cello on weekends", "Wants to run a marathon in Tokyo",
             "Is writing a book about bridges", "Plans to visit Iceland next summer"]
    for n, text in enumerate(texts):
        args = {"op": "add", "content": text, "topic": "goals"}
        conn.send(item(f"q{n}", {**good, "qid": f"q{n}", "params": {"name": "memory_propose", "arguments": args}}))
    for n in range(5):
        ack(conn, f"q{n}")
    acks = [f["qid"] for f in conn.frames if f["type"] == "ack"]
    assert acks == [f"q{n}" for n in range(5)]
    assert [p.content for p in Proposals(vault).list()][::-1] == texts


def test_queued_items_name_unknown_web_apps_as_unverified_and_keep_times_in_range(vault, fake, clouds, queue_clock,
                                                                                  queue_on):
    _, conn = online(vault, fake, clouds)
    good = VECTOR["plaintext"]

    def propose(qid: str, content: str, **over) -> dict:
        args = {"op": "add", "content": content, "topic": "goals"}
        return {**good, "qid": qid, "params": {"name": "memory_propose", "arguments": args}, **over}

    now = queue_clock
    cases = [
        # The sealed client has no `verified` flag: only chatgpt and claude are named as verified, by their own name.
        propose("q-x", "Is learning Japanese", client={"slug": "x-0123456789", "name": "ChatGPT"}),
        propose("q-gpt", "Plays the cello on weekends", client={"slug": "chatgpt", "name": "Totally ChatGPT"}),
        propose("q-svc", "Grows tomatoes on the balcony",  # how the service names an unverified client
                client={"slug": "x-abcdef0123", "name": "Notes Helper (unverified)"}),
        propose("q-future", "Wants to run a marathon in Tokyo", received_at="2999-01-01T00:00:00Z"),
        propose("q-old", "Is writing a book about bridges",
                received_at=(now - timedelta(days=90)).isoformat()),
        propose("q-odd", "Plans to visit Iceland next summer", received_at="0001-01-01T00:00:00+01:00"),
        propose("q-junk", "Keeps bees in the garden", received_at="9" * 5000),
    ]
    for c in cases:
        conn.send(item(c["qid"], c))
    assert [ack(conn, c["qid"])["outcome"] for c in cases] == ["stored"] * len(cases)
    names = {c.id: c.name for c in Connectors(vault).list()}
    assert names == {"cloud-x-0123456789": "ChatGPT (web, unverified)", "cloud-chatgpt": "ChatGPT (web)",
                     "cloud-x-abcdef0123": "Notes Helper (web, unverified)"}
    made = {p.content: datetime.fromisoformat(p.created_at) for p in Proposals(vault).list()}
    assert made["Is learning Japanese"] == datetime(2026, 10, 12, 21, 4, 5, tzinfo=UTC)  # received_at, as sent
    assert made["Wants to run a marathon in Tokyo"] == now  # never in the future
    assert made["Is writing a book about bridges"] == now - timedelta(days=31)  # nor older than the queue keeps
    for text in ("Plans to visit Iceland next summer", "Keeps bees in the garden"):  # not a usable time: filed now
        assert abs(made[text] - datetime.now(UTC)) < timedelta(seconds=30), text


# ---- the command line ---------------------------------------------------------------------------------------------


def test_cli_links_shows_status_and_unlinks(vault, fake, monkeypatch):
    import socket
    import webbrowser

    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", fake.base)
    opened: list[str] = []

    def browser(url: str) -> bool:  # the person signs in; MyMemo sends the browser back to the CLI's listener
        opened.append(url)
        back = fake.authorize(url)
        threading.Thread(target=lambda: urllib.request.urlopen(back, timeout=10).read(), daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", browser)
    with socket.socket() as s:  # no panel runs there
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    runner = CliRunner()
    base = ["--vault", str(vault.root), "cloud"]
    out = runner.invoke(cli, [*base, "link", "--port", str(port)])
    assert out.exit_code == 0, out.output
    # The service's plan says it has the queue (link/response.json), but this computer doesn't take it yet.
    assert "Linked to Sam (s***@example.com) · Ultra · live relay." in out.output and "queue" not in out.output
    assert "If that isn't your MyMemo account, run `openmymemo cloud unlink` now." in out.output
    assert [(a["action"], a["email_masked"]) for a in vault.activity("mymemo-cloud")] == [("link", "s***@example.com")]
    assert f"{fake.base}/openmymemo/mcp" in out.output and "Developer Mode" in out.output
    redirect = dict(parse_qsl(urlsplit(opened[0]).query))["redirect_uri"]
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/cloud/callback")
    assert cloud.load_link().vault == str(vault.root)

    out = runner.invoke(cli, [*base, "status", "--port", str(port)])
    assert out.exit_code == 0 and "Linked to Sam (s***@example.com) · Ultra · live relay\n" in out.output
    assert "not running" in out.output and "queue" not in out.output

    out = runner.invoke(cli, [*base, "unlink", "--yes", "--port", str(port)])
    assert out.exit_code == 0 and "Unlinked" in out.output and cloud.load_link() is None
    assert fake.unlinks == [f"Bearer {fake.token}"]
    assert "Not linked" in runner.invoke(cli, [*base, "status"]).output


def test_cloud_settings_from_the_cli(vault, monkeypatch):
    monkeypatch.delenv("OPENMYMEMO_CLOUD_URL")
    runner = CliRunner()
    apps = runner.invoke(cli, ["--vault", str(vault.root), "apps"])
    assert apps.exit_code == 0 and "OpenMyMemo Cloud (mymemo-cloud)" in apps.output
    assert "ChatGPT and claude.ai on the web: openmymemo cloud link (needs MyMemo Pro, Ultra or Lifetime)" in apps.output
    out = runner.invoke(cli, ["cloud", "settings"])
    assert out.exit_code == 0 and "Listed: yes" in out.output and not cloud.settings_path().exists()
    out = runner.invoke(cli, ["cloud", "settings", "--hide", "--service", "https://memo.example.org"])
    assert out.exit_code == 0 and "Listed: no" in out.output and "Service: https://memo.example.org\n" in out.output
    apps = runner.invoke(cli, ["--vault", str(vault.root), "apps"]).output
    assert "mymemo-cloud" not in apps and "cloud link" not in apps
    out = runner.invoke(cli, ["cloud", "settings", "--show", "--service", ""])
    assert "Listed: yes" in out.output and "(MyMemo's own)" in out.output
    assert "mymemo-cloud" in runner.invoke(cli, ["--vault", str(vault.root), "apps"]).output
    bad = runner.invoke(cli, ["cloud", "settings", "--service", "http://example.com"])
    assert bad.exit_code == 1 and "https://" in bad.output


def test_cloud_settings_says_when_the_environment_sets_the_service(monkeypatch):
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", "http://127.0.0.1:9911/")
    out = CliRunner().invoke(cli, ["cloud", "settings"])
    assert out.exit_code == 0
    assert "Service: http://127.0.0.1:9911 (set by OPENMYMEMO_CLOUD_URL)\n" in out.output  # not "MyMemo's own"
