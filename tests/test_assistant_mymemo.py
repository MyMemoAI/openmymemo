"""MyMemo's hosted model for the OpenMyMemo Assistant (product-design.md §17.8) against a fake MyMemo service built
from tests/fixtures/cloud/assistant/ (tests/assistant_fake.py): signing in (token exchange for a computer linked to
MyMemo Cloud, or PKCE in the browser with the iss check), tokens kept per computer and refreshed with rotation, the
endpoint's 401 / 402 / 429 / 503 answers, signing out, and privacy. Nothing here reaches MyMemo."""

import json
import os
import stat
import threading
import urllib.request
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from assistant_fake import ERRORS, MODELS, TOKEN, FakeAssistantService
from cloud_fake import fixture
from fastapi.testclient import TestClient
from test_assistant import PASTE, SCHEMA, split_answer
from typer.testing import CliRunner

from openmymemo import cloud
from openmymemo.assistant import (
    Assistant,
    Data,
    ensure_connector,
    features,
    load_settings,
    mymemo,
    save_settings,
)
from openmymemo.assistant import settings as settings_mod
from openmymemo.cli import app
from openmymemo.web import create_api

H = {"X-OpenMyMemo": "1"}
SECRET = "Takes sumatriptan for cluster headaches every Thursday morning"


@pytest.fixture
def fake(monkeypatch):
    service = FakeAssistantService().start()
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", service.base)
    yield service
    service.stop()


@pytest.fixture
def api(vault, fake):
    return TestClient(create_api(vault, 8787), base_url="http://127.0.0.1:8787")


def link_computer(vault, fake) -> cloud.Link:
    link = cloud.Link(service=fake.base, device_id="dev-1", device_token=fake.token, private_key="", key_id="",
                      vault=str(vault.root), member={"display_name": "Sam", "email_masked": "s***@example.com"})
    cloud.save_link(link)
    return link


def signed_in(vault, fake) -> Assistant:
    """MyMemo chosen and signed in by token exchange; the Assistant's connector exists."""
    link_computer(vault, fake)
    assert mymemo.exchange(cloud.load_link()) == "signed_in"
    save_settings(enabled=True, kind="mymemo")
    ensure_connector(vault, regrant=True)
    return Assistant.from_settings(vault)


def callback_path(url: str) -> str:
    parts = urlsplit(url)
    assert parts.netloc == "127.0.0.1:8787" and parts.path == "/cloud/callback"
    return f"{parts.path}?{parts.query}"


def raw() -> dict:
    return json.loads(settings_mod.settings_path().read_text())


# ---- signing in ----------------------------------------------------------------------------------------------------


def test_a_computer_linked_to_mymemo_cloud_signs_in_by_token_exchange(vault, fake, api):
    link_computer(vault, fake)
    s = api.patch("/api/assistant", headers=H, json={"enabled": True, "kind": "mymemo"}).json()
    assert s["mymemo"]["cloud_linked"] and not s["mymemo"]["signed_in"] and not s["ready"]
    r = api.post("/api/assistant/mymemo/signin", headers=H).json()
    assert r["signed_in"] and r["url"] is None and fake.authorize_params == []  # no browser
    form = fake.token_forms[-1]
    assert set(form) == set(TOKEN["token_exchange"]) and form["subject_token"] == fake.token
    assert {k: v for k, v in form.items() if k != "subject_token"} == {
        k: v for k, v in TOKEN["token_exchange"].items() if k != "subject_token"}
    m = r["assistant"]["mymemo"]
    assert m["via"] == "cloud" and m["account"] == {"display_name": "Sam", "email_masked": "s***@example.com"}
    assert r["assistant"]["ready"] and r["assistant"]["endpoint"] == f"{fake.base}/openmymemo/assistant/v1"
    # Kept per computer, readable by this user only, never in the folder; never shown.
    path = settings_mod.settings_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 or os.name == "nt"
    assert not str(path).startswith(str(vault.root))
    stored = raw()
    assert stored["mymemo_token"] in fake.access and stored["mymemo_refresh"] == fake.refresh
    assert stored["mymemo_token"] not in json.dumps(api.get("/api/assistant", headers=H).json())
    assert fake.refresh not in json.dumps(api.get("/api/assistant", headers=H).json())


def test_a_link_mymemo_no_longer_knows_falls_back_to_the_browser(vault, fake, api):
    link_computer(vault, fake)
    fake.device_known = False
    save_settings(enabled=True, kind="mymemo")
    r = api.post("/api/assistant/mymemo/signin", headers=H).json()
    assert not r["signed_in"] and r["url"].startswith(f"{fake.base}/openmymemo/oauth/authorize?")


def test_signing_in_with_the_browser_checks_the_state_and_iss(vault, fake, api):
    api.patch("/api/assistant", headers=H, json={"enabled": True, "kind": "mymemo"})
    url = api.post("/api/assistant/mymemo/signin", headers=H).json()["url"]
    params = dict(parse_qsl(urlsplit(url).query))
    assert params["client_id"] == "openmymemo-desktop" and params["scope"] == "assistant offline_access"
    assert params["redirect_uri"] == "http://127.0.0.1:8787/cloud/callback"
    assert params["resource"] == f"{fake.base}/openmymemo/assistant" and params["code_challenge_method"] == "S256"
    assert api.get("/api/assistant", headers=H).json()["mymemo"]["pending"] is True

    # An answer naming another server is ignored, and its code never exchanged.
    back = urlsplit(fake.authorize(url))
    forged = {**dict(parse_qsl(back.query)), "iss": "https://evil.example"}
    r = api.get(f"/cloud/callback?{urlencode(forged)}")
    assert r.status_code == 400 and "unexpected server" in r.text and fake.token_forms == []
    assert not load_settings().mymemo_token

    url = api.post("/api/assistant/mymemo/signin", headers=H).json()["url"]
    back = callback_path(fake.authorize(url))
    r = api.get(back)  # a browser navigation: no X-OpenMyMemo header
    assert r.status_code == 200 and "Signed in to MyMemo" in r.text and r.headers["cache-control"] == "no-store"
    form = fake.token_forms[-1]
    assert set(form) == set(TOKEN["authorization_code"]) and form["redirect_uri"] == params["redirect_uri"]
    s = api.get("/api/assistant", headers=H).json()
    assert s["mymemo"]["signed_in"] and s["mymemo"]["via"] == "browser" and s["ready"]
    assert not s["mymemo"]["pending"] and not cloud.load_link()  # MyMemo Cloud isn't linked by this
    assert api.get(back).status_code == 400  # a replay does nothing
    assert len(fake.token_forms) == 1


def test_the_cloud_callback_still_links_mymemo_cloud(vault, fake, api):
    url = api.post("/api/cloud/link", headers=H).json()["url"]
    assert api.get(callback_path(fake.authorize(url))).status_code == 200
    assert cloud.load_link() is not None and not load_settings().mymemo_token


def test_the_assistant_api_keeps_the_panel_guard(api):
    for method, path in (("GET", "/api/assistant/mymemo"), ("POST", "/api/assistant/mymemo/signin"),
                         ("DELETE", "/api/assistant/mymemo")):
        assert api.request(method, path).status_code == 403


# ---- calls, tokens and refresh -------------------------------------------------------------------------------------


def test_calls_carry_the_token_and_the_feature(vault, fake):
    a = signed_in(vault, fake)
    assert a.json_call("sort", "x", ["some text"], SCHEMA) == {"items": [{"text": "ok"}]}
    sent = fake.chats[-1]
    assert sent["headers"]["authorization"] == f"Bearer {raw()['mymemo_token']}"
    assert sent["headers"]["x-openmymemo-feature"] == "sort" and sent["body"]["messages"][-1]["role"] == "user"
    call = [x for x in vault.activity("assistant") if x["action"] == "call"][-1]
    assert call["result"] == "ok" and call["kind"] == "mymemo" and call["model"] == MODELS["response"]["model"]


def test_a_401_refreshes_once_rotates_and_retries(vault, fake):
    a = signed_in(vault, fake)
    old_access, old_refresh = raw()["mymemo_token"], raw()["mymemo_refresh"]
    fake.expire_access()
    assert a.json_call("sort", "x", ["some text"], SCHEMA) is not None
    assert fake.chat_auth == [f"Bearer {old_access}", f"Bearer {raw()['mymemo_token']}"]
    assert fake.token_forms[-1] == {**TOKEN["refresh_token"], "refresh_token": old_refresh}
    assert raw()["mymemo_refresh"] == fake.refresh != old_refresh  # rotated, and the new one kept
    assert a.settings.mymemo_token == raw()["mymemo_token"]


def test_a_token_about_to_expire_is_refreshed_before_the_call(vault, fake):
    signed_in(vault, fake)
    stored = raw()
    stored["mymemo"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    settings_mod._write(stored)
    a = Assistant.from_settings(vault)
    assert a.json_call("sort", "x", ["some text"], SCHEMA) is not None
    assert fake.token_forms[-1]["grant_type"] == "refresh_token" and len(fake.chat_auth) == 1


def test_another_process_refreshing_first_is_reused_not_refreshed_again(vault, fake):
    a = signed_in(vault, fake)
    b = Assistant.from_settings(vault)
    fake.expire_access()
    assert a.json_call("sort", "x", ["one"], SCHEMA) is not None
    refreshes = sum(f["grant_type"] == "refresh_token" for f in fake.token_forms)
    assert b.json_call("sort", "x", ["two"], SCHEMA) is not None  # its old token gets a 401: takes a's new one
    assert sum(f["grant_type"] == "refresh_token" for f in fake.token_forms) == refreshes
    assert not fake.spent & {raw()["mymemo_refresh"]}


def test_a_refused_refresh_signs_out_and_features_use_their_rules(vault, fake, api):
    a = signed_in(vault, fake)
    fake.expire_access()
    fake.refresh = None  # MyMemo revoked the grant (e.g. removed in the account settings)
    assert a.json_call("sort", "x", ["some text"], SCHEMA) is None
    assert a.last_error == mymemo.SIGNED_OUT and len(fake.chat_auth) == 1
    stored = raw()
    assert "mymemo_token" not in stored and "mymemo_refresh" not in stored
    s = api.get("/api/assistant", headers=H).json()
    assert not s["mymemo"]["signed_in"] and s["mymemo"]["signed_out"] == "expired" and not s["ready"]
    items = features.import_preview(vault, "markdown", PASTE)
    assert items and len(fake.chat_auth) == 1  # rules, no request


# ---- what the service says about the plan --------------------------------------------------------------------------


def test_no_plan_pauses_quietly_until_the_plan_is_active(vault, fake, api):
    a = signed_in(vault, fake)
    fake.fail("plan_required")
    fake.answer = split_answer
    items = features.import_preview(vault, "markdown", PASTE)
    assert all(it.by == "rules" for it in items) and len(fake.chats) == 1
    s = api.get("/api/assistant", headers=H).json()
    hold = s["mymemo"]["hold"]
    assert hold["code"] == "plan_required" and hold["message"].startswith("Needs the OpenMyMemo Assistant plan")
    assert hold["plan_url"] == f"{fake.base}/openmymemo/assistant" and not s["ready"]
    # No more requests while paused: every feature takes its rule path.
    a = Assistant.from_settings(vault)
    assert not a.available("sort") and a.json_call("sort", "x", ["t"], SCHEMA) is None
    assert features.import_preview(vault, "markdown", PASTE) and len(fake.chats) == 1
    # Opening the plan view sees the plan active (bought meanwhile): the pause is lifted.
    acct = api.get("/api/assistant/mymemo", headers=H).json()
    assert acct["account"]["ok"] and acct["account"]["plan"]["active"]
    assert acct["assistant"]["mymemo"]["hold"] is None and Assistant.from_settings(vault).available("sort")


def test_a_used_up_allowance_pauses_until_it_resets(vault, fake):
    a = signed_in(vault, fake)
    fake.fail("quota_exceeded")
    assert a.json_call("sort", "x", ["t"], SCHEMA) is None
    assert a.last_error.startswith("This month's allowance is used up, resets November 1")
    a = Assistant.from_settings(vault)
    assert a.unavailable_reason("sort").startswith("This month's allowance is used up, resets November 1")
    assert a.json_call("sort", "x", ["t"], SCHEMA) is None and len(fake.chats) == 1
    h = mymemo.hold(a.settings)
    assert h["code"] == "quota_exceeded" and h["resets_at"] == "2026-11-01T00:00:00+00:00"
    call = [x for x in vault.activity("assistant") if x["action"] == "call"][-1]
    assert call["result"] == "quota_exceeded"


@pytest.mark.parametrize("name", ["rate_limit_exceeded", "model_unavailable"])
def test_busy_answers_respect_retry_after(name, vault, fake, monkeypatch):
    a = signed_in(vault, fake)
    fake.fail(name)
    start = mymemo._now()
    assert a.json_call("sort", "x", ["t"], SCHEMA) is None
    seconds = int(ERRORS[name]["retry_after"])
    h = mymemo.hold(a.settings)
    assert h["code"] == ("rate_limited" if name == "rate_limit_exceeded" else "unavailable")
    until = mymemo._parse(h["until"])
    assert start + timedelta(seconds=seconds - 2) <= until <= start + timedelta(seconds=seconds + 2)
    a = Assistant.from_settings(vault)
    assert a.json_call("sort", "x", ["t"], SCHEMA) is None and not a.test()["ok"] and len(fake.chats) == 1
    monkeypatch.setattr(mymemo, "_now", lambda: start + timedelta(seconds=seconds + 3))
    a = Assistant.from_settings(vault)
    assert a.json_call("sort", "x", ["t"], SCHEMA) is not None and len(fake.chats) == 2
    assert mymemo.hold(a.settings) is None and "hold" not in raw()["mymemo"]


def test_the_plan_view_shows_what_the_service_answers(vault, fake, api):
    signed_in(vault, fake)
    acct = api.get("/api/assistant/mymemo", headers=H).json()["account"]
    want = MODELS["response"]
    assert acct["display_name"] == want["display_name"] and acct["provider"] == want["provider"]
    assert acct["usage"] == want["usage"] and acct["limits"] == want["limits"]
    assert acct["plan"]["status"] == "active" and acct["plan"]["plan_url"] == f"{fake.base}/openmymemo/assistant"
    fake.expire_access()  # refreshed once on 401, here too
    assert api.get("/api/assistant/mymemo", headers=H).json()["account"]["ok"]
    assert sum(f["grant_type"] == "refresh_token" for f in fake.token_forms) == 1


def test_a_plan_url_that_isnt_a_web_page_is_dropped():
    assert mymemo._plan_url("javascript:alert(1)", "https://api.mymemo.ai") is None
    assert mymemo._plan_url("http://example.com/plan", "https://api.mymemo.ai") is None
    assert mymemo._plan_url("https://app.mymemo.ai/openmymemo/assistant", "https://api.mymemo.ai")


# ---- signing out ---------------------------------------------------------------------------------------------------


def test_signing_out_revokes_the_grant_and_forgets_the_tokens(vault, fake, api):
    signed_in(vault, fake)
    refresh, access = raw()["mymemo_refresh"], raw()["mymemo_token"]
    r = api.delete("/api/assistant/mymemo", headers=H).json()
    assert r["message"].startswith("Signed out of MyMemo")
    assert fake.revoked == [{"token": refresh, "client_id": "openmymemo-desktop", "token_type_hint": "refresh_token"}]
    assert access not in fake.access and fake.refresh is None
    stored = raw()
    assert "mymemo_token" not in stored and "mymemo_refresh" not in stored and stored["kind"] == "mymemo"
    s = r["assistant"]
    assert not s["mymemo"]["signed_in"] and s["mymemo"]["signed_out"] is None and not s["ready"]
    assert cloud.load_link() is not None  # MyMemo Cloud stays linked


def test_signing_out_while_mymemo_is_unreachable_still_forgets(vault, fake, monkeypatch):
    signed_in(vault, fake)
    monkeypatch.setattr(mymemo, "_oauth", lambda *a, **k: (_ for _ in ()).throw(cloud.CloudError("down")))
    assert "couldn't be told" in mymemo.sign_out()
    assert not load_settings().mymemo_token


# ---- the CLI -------------------------------------------------------------------------------------------------------


def test_cli_signin_by_exchange_plan_and_signout(vault, fake):
    link_computer(vault, fake)
    runner = CliRunner()
    v = ["--vault", str(vault.root)]
    r = runner.invoke(app, [*v, "assistant", "signin"])
    assert r.exit_code == 0, r.stdout
    assert "Signed in: Sam (s***@example.com) with this computer's MyMemo Cloud link" in r.stdout
    assert raw()["mymemo_token"] not in r.stdout
    r = runner.invoke(app, [*v, "assistant", "plan"])
    assert r.exit_code == 0 and "Claude Haiku 5.5 by Anthropic" in r.stdout and "Manage the plan:" in r.stdout
    r = runner.invoke(app, [*v, "assistant", "signout"])
    assert r.exit_code == 0 and "Signed out of MyMemo" in r.stdout and fake.revoked


def test_cli_signin_with_the_browser(vault, fake, monkeypatch):
    def browser(url: str) -> bool:  # the person allows it; the browser comes back to the CLI's own listener
        back = fake.authorize(url)
        assert urlsplit(back).path == "/cloud/callback"
        threading.Thread(target=lambda: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(back),
                         daemon=True).start()
        return True

    monkeypatch.setattr("webbrowser.open", browser)
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "signin"])
    assert r.exit_code == 0, r.stdout
    assert "Signed in to MyMemo" in r.stdout and fake.authorize_params[-1]["scope"] == "assistant offline_access"
    s = load_settings()
    assert s.enabled and s.kind == "mymemo" and s.mymemo_token in fake.access and s.mymemo["via"] == "browser"


# ---- privacy -------------------------------------------------------------------------------------------------------


def test_private_memories_never_reach_mymemo(vault, fake):
    vault.add("work", "Leads the platform team")
    vault.add("health", SECRET)
    vault.add("work", "Salary is 180k before bonus", private=True)
    a = signed_in(vault, fake)
    assert a.remote
    blocks = a.memory_data(vault.memories())
    assert a.json_call("sort", "x", blocks, SCHEMA) is not None
    assert a.json_call("import", "x", [f"notes: {SECRET.lower()}"], SCHEMA) is None
    assert a.json_call("import", "x", [Data("y", topic="health")], SCHEMA) is None
    sent = json.dumps([c["body"] for c in fake.chats], ensure_ascii=False)
    assert len(fake.chats) == 1 and "sumatriptan" not in sent and "180k" not in sent
    assert "Leads the platform team" in sent


def test_the_fixtures_name_no_prices():
    text = json.dumps([fixture("assistant/models.json"), fixture("assistant/errors.json")]).lower()
    assert "$" not in text and "usd" not in text and "price" not in text
