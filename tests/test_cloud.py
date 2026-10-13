"""MyMemo Cloud (product-design.md §16) against a fake service built from tests/fixtures/cloud/.

Every test has its own HOME, OPENMYMEMO_CLOUD_DIR and a local OPENMYMEMO_CLOUD_URL (conftest.py); nothing here
reaches MyMemo or touches this computer's real cloud.json.
"""

import json
import os
import stat
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import anyio
import pytest
from cloud_fake import FakeService, fixture, wait_until
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from openmymemo import __version__, clients, cloud
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
    assert [p.name for p in path.parent.iterdir()] == ["cloud.json"]  # no temporary file left behind
    assert not cloud.delete_link("another-device") and path.exists()  # a newer link is never deleted by mistake
    assert cloud.delete_link(link.device_id) and not path.exists()


def test_hello_carries_the_cloud_variant_of_this_installations_tools(vault):
    hello = fixture("bridge/hello.json")
    link = linked(vault, "http://127.0.0.1:9")
    ours = anyio.run(Bridge(vault, link).hello)
    assert ours["type"] == "hello" and ours["v"] == 1 and ours["app_version"] == __version__
    assert ours["key_id"] == hello["key_id"] == VECTOR["key_id"] and ours["max_inflight"] == hello["max_inflight"]
    assert ours["tools"] == hello["tools"] and ours["instructions"] == hello["instructions"]
    # Only memory_propose differs from the local tools: it mentions "queued", which only the cloud returns.
    local = {t.name: t for t in anyio.run(build_server(vault).list_tools)}
    for tool in ours["tools"]:
        description = local[tool["name"]].description
        assert (tool["description"] != description) == (tool["name"] == "memory_propose")
    assert '"queued"' in next(t for t in ours["tools"] if t["name"] == "memory_propose")["description"]


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

    back = callback_path(fake.authorize(url))
    r = api.get(back)  # a browser navigation: no X-OpenMyMemo header
    assert r.status_code == 200 and "Linked" in r.text and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"

    sent = fake.link_requests[-1]
    assert list(sent) == list(fixture("link/request.json")) and sent["app_version"] == __version__
    assert sent["redirect_uri"] == params["redirect_uri"] and sent["key_alg"] == "x25519"
    link = cloud.load_link()
    response = fixture("link/response.json")
    assert link.device_id == response["device_id"] and link.device_token == response["device_token"]
    assert link.service == fake.base and link.vault == str(vault.root) and link.display_name == "Sam"
    assert link.plan == response["plan"] and link.key_id == cloud.key_id(cloud.unb64u(sent["public_key"]))
    assert stat.S_IMODE(cloud.link_path().stat().st_mode) == 0o600 or os.name == "nt"
    assert not (vault.root / "cloud.json").exists()

    # The panel connects right away.
    conn = fake.connection()
    assert conn.hello["key_id"] == link.key_id and conn.headers["authorization"] == f"Bearer {link.device_token}"
    assert "origin" not in conn.headers
    status = wait_until(lambda: (s := api.get("/api/cloud", headers=H).json())["online"] and s)
    assert status["account"] == {"display_name": "Sam"} and status["plan"]["tier"] == "ultra"
    assert status["endpoint"] == f"{fake.base}/openmymemo/mcp" and status["this_folder"] and not status["pending"]

    # The same callback again (a replay) does nothing.
    again = api.get(back)
    assert again.status_code == 400 and "valid anymore" in again.text
    assert len(fake.link_requests) == 1 and cloud.load_link() == link


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

    c.linker.ttl = 0.0  # ten minutes have passed
    url = api.post("/api/cloud/link", headers=H).json()["url"]
    time.sleep(0.01)
    assert api.get(callback_path(fake.authorize(url))).status_code == 400
    assert fake.link_requests == [] and cloud.load_link() is None

    c.linker.ttl = cloud.LINK_TTL
    fake.link_response = {"error": "plan_required", "message": "MyMemo Cloud is part of Pro and Ultra."}
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
    assert out["message"].startswith("Unlinked this computer from MyMemo Cloud")
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
    assert cloud.unlink() == "This computer isn't linked to MyMemo Cloud."


def test_the_catalog_lists_mymemo_cloud(vault, fake, panel):
    api, _ = panel
    row = next(r for r in clients.catalog(vault) if r["id"] == clients.CLOUD)
    assert row["method"] == "account" and row["description"] == "Use your memory in ChatGPT and claude.ai on the web"
    assert row["connected"] is False
    assert clients.connect(vault, clients.CLOUD).status == "manual"
    linked(vault, fake.base)
    Connectors(vault).ensure("cloud-chatgpt", "ChatGPT (web)", kind="cloud")
    row = next(r for r in clients.catalog(vault) if r["id"] == clients.CLOUD)
    assert row["connected"] is True and row["via"] == "account"
    listed = api.get("/api/clients", headers=H).json()
    assert listed["custom"] == []  # web apps are listed under MyMemo Cloud, not as apps added by name
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
    assert status["last_call_at"] and status["queued"] == WELCOME["queued"]


def test_a_revoked_web_app_is_told_so(vault, fake, clouds):
    _, conn = online(vault, fake, clouds)
    call(conn, {**fixture("bridge/call_propose.json"), "id": "first"})
    Connectors(vault).remove("cloud-claude")
    assert call(conn, fixture("bridge/call_propose.json")) == fixture("bridge/result_error.json")
    assert Connectors(vault).list() == []  # not silently registered again


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


def test_a_refused_handshake_means_the_token_is_gone(vault, fake, clouds):
    linked(vault, fake.base)
    fake.refuse_upgrades = True
    c = Cloud(vault, 8787, FAST)
    clouds.append(c)
    c.start()
    wait_until(lambda: not c.status()["linked"])
    assert fake.handshakes and fake.conns == [] and cloud.load_link() is None


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
    assert "Linked as Sam (Ultra · live relay and offline queue)." in out.output
    assert f"{fake.base}/openmymemo/mcp" in out.output and "Developer Mode" in out.output
    redirect = dict(parse_qsl(urlsplit(opened[0]).query))["redirect_uri"]
    assert redirect.startswith("http://127.0.0.1:") and redirect.endswith("/cloud/callback")
    assert cloud.load_link().vault == str(vault.root)

    out = runner.invoke(cli, [*base, "status", "--port", str(port)])
    assert out.exit_code == 0 and "Linked as Sam" in out.output and "not running" in out.output

    out = runner.invoke(cli, [*base, "unlink", "--yes", "--port", str(port)])
    assert out.exit_code == 0 and "Unlinked" in out.output and cloud.load_link() is None
    assert fake.unlinks == [f"Bearer {fake.token}"]
    assert "Not linked" in runner.invoke(cli, [*base, "status"]).output
