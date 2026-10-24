"""MyMemo's hosted model for the OpenMyMemo Assistant (product-design.md §17.8) against a fake MyMemo service built
from tests/fixtures/cloud/assistant/ (tests/assistant_fake.py): signing in (token exchange for a computer linked to
OpenMyMemo Cloud, or PKCE in the browser with the iss check), tokens kept per computer and refreshed with rotation, the
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


def ask(a: Assistant, text: str = "m1: t") -> dict | None:
    """A ``sort`` call as the feature makes it: one memory block, MyMemo's ``sort`` task."""
    return a.json_call("sort", "x", [Data(text, label="memory")], SCHEMA, task="sort")


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
    assert not s["mymemo"]["pending"] and not cloud.load_link()  # OpenMyMemo Cloud isn't linked by this
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


def test_calls_carry_the_token_and_only_the_task_and_its_material(vault, fake):
    a = signed_in(vault, fake)
    assert ask(a, "some text") == {"items": [{"text": "ok"}]}
    sent = fake.tasks[-1]
    assert sent["headers"]["authorization"] == f"Bearer {raw()['mymemo_token']}"
    # No instructions, schema, model or token budget: the service writes the prompt.
    assert sent["body"] == {"task": "sort", "blocks": [{"label": "memory", "text": "some text"}], "inputs": {}}
    assert "x-openmymemo-feature" not in sent["headers"]
    call = [x for x in vault.activity("assistant") if x["action"] == "call"][-1]
    assert call["result"] == "ok" and call["kind"] == "mymemo" and call["model"] == MODELS["response"]["model"]


def test_a_401_refreshes_once_rotates_and_retries(vault, fake):
    a = signed_in(vault, fake)
    old_access, old_refresh = raw()["mymemo_token"], raw()["mymemo_refresh"]
    fake.expire_access()
    assert ask(a, "some text") is not None
    assert fake.task_auth == [f"Bearer {old_access}", f"Bearer {raw()['mymemo_token']}"]
    assert fake.token_forms[-1] == {**TOKEN["refresh_token"], "refresh_token": old_refresh}
    assert raw()["mymemo_refresh"] == fake.refresh != old_refresh  # rotated, and the new one kept
    assert a.settings.mymemo_token == raw()["mymemo_token"]


def test_a_token_about_to_expire_is_refreshed_before_the_call(vault, fake):
    signed_in(vault, fake)
    stored = raw()
    stored["mymemo"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    settings_mod._write(stored)
    a = Assistant.from_settings(vault)
    assert ask(a, "some text") is not None
    assert fake.token_forms[-1]["grant_type"] == "refresh_token" and len(fake.task_auth) == 1


def test_another_process_refreshing_first_is_reused_not_refreshed_again(vault, fake):
    a = signed_in(vault, fake)
    b = Assistant.from_settings(vault)
    fake.expire_access()
    assert ask(a, "one") is not None
    refreshes = sum(f["grant_type"] == "refresh_token" for f in fake.token_forms)
    assert ask(b, "two") is not None  # its old token gets a 401: takes a's new one
    assert sum(f["grant_type"] == "refresh_token" for f in fake.token_forms) == refreshes
    assert not fake.spent & {raw()["mymemo_refresh"]}


def test_a_refused_refresh_signs_out_and_features_use_their_rules(vault, fake, api):
    a = signed_in(vault, fake)
    fake.expire_access()
    fake.refresh = None  # MyMemo revoked the grant (e.g. removed in the account settings)
    assert ask(a, "some text") is None
    assert a.last_error == mymemo.SIGNED_OUT and len(fake.task_auth) == 1
    stored = raw()
    assert "mymemo_token" not in stored and "mymemo_refresh" not in stored
    s = api.get("/api/assistant", headers=H).json()
    assert not s["mymemo"]["signed_in"] and s["mymemo"]["signed_out"] == "expired" and not s["ready"]
    items = features.import_preview(vault, "markdown", PASTE)
    assert items and len(fake.task_auth) == 1  # rules, no request


# ---- what the service says about the plan --------------------------------------------------------------------------


def test_no_plan_pauses_quietly_until_the_plan_is_active(vault, fake, api):
    a = signed_in(vault, fake)
    fake.fail("plan_required")
    fake.answer = split_answer
    items = features.import_preview(vault, "markdown", PASTE)
    assert all(it.by == "rules" for it in items) and len(fake.tasks) == 1
    s = api.get("/api/assistant", headers=H).json()
    hold = s["mymemo"]["hold"]
    assert hold["code"] == "plan_required" and hold["message"].startswith("Needs the OpenMyMemo Assistant plan")
    assert hold["plan_url"] == f"{fake.base}/openmymemo/assistant" and not s["ready"]
    # No more requests while paused: every feature takes its rule path.
    a = Assistant.from_settings(vault)
    assert not a.available("sort") and ask(a, "t") is None
    assert features.import_preview(vault, "markdown", PASTE) and len(fake.tasks) == 1
    # Opening the plan view sees the plan active (bought meanwhile): the pause is lifted.
    acct = api.get("/api/assistant/mymemo", headers=H).json()
    assert acct["account"]["ok"] and acct["account"]["plan"]["active"]
    assert acct["assistant"]["mymemo"]["hold"] is None and Assistant.from_settings(vault).available("sort")


def test_a_used_up_allowance_pauses_until_it_resets(vault, fake):
    a = signed_in(vault, fake)
    fake.fail("quota_exceeded")
    assert ask(a, "t") is None
    assert a.last_error.startswith("This month's included use is used up; it resets November 1")
    a = Assistant.from_settings(vault)
    assert a.unavailable_reason("sort").startswith("This month's included use is used up; it resets November 1")
    assert ask(a, "t") is None and len(fake.tasks) == 1
    h = mymemo.hold(a.settings)
    assert h["code"] == "quota_exceeded" and h["resets_at"] == "2026-11-01T00:00:00+00:00"
    call = [x for x in vault.activity("assistant") if x["action"] == "call"][-1]
    assert call["result"] == "quota_exceeded"


@pytest.mark.parametrize("name", ["rate_limit_exceeded", "model_unavailable"])
def test_busy_answers_respect_retry_after(name, vault, fake, monkeypatch):
    a = signed_in(vault, fake)
    fake.fail(name)
    start = mymemo._now()
    assert ask(a, "t") is None
    seconds = int(ERRORS[name]["retry_after"])
    h = mymemo.hold(a.settings)
    assert h["code"] == ("rate_limited" if name == "rate_limit_exceeded" else "unavailable")
    until = mymemo._parse(h["until"])
    assert start + timedelta(seconds=seconds - 2) <= until <= start + timedelta(seconds=seconds + 2)
    a = Assistant.from_settings(vault)
    assert ask(a, "t") is None and not a.test()["ok"] and len(fake.tasks) == 1
    monkeypatch.setattr(mymemo, "_now", lambda: start + timedelta(seconds=seconds + 3))
    a = Assistant.from_settings(vault)
    assert ask(a, "t") is not None and len(fake.tasks) == 2
    assert mymemo.hold(a.settings) is None and "hold" not in raw()["mymemo"]


def test_the_plan_view_shows_what_the_service_answers(vault, fake, api):
    signed_in(vault, fake)
    acct = api.get("/api/assistant/mymemo", headers=H).json()["account"]
    want = MODELS["response"]
    assert acct["display_name"] == want["display_name"] and acct["provider"] == want["provider"]
    assert acct["usage"] == want["usage"] and acct["limits"] == want["limits"]  # dollars and tokens, as answered
    assert isinstance(acct["usage"]["cost_usd"], float) and acct["usage"]["cost_usd_included"] == 10.0
    assert mymemo.cost_text(acct["usage"]) == "$0.00 of $10.00 used this month"
    assert acct["plan"]["status"] == "active" and acct["plan"]["plan_url"] == f"{fake.base}/openmymemo/assistant"
    fake.expire_access()  # refreshed once on 401, here too
    assert api.get("/api/assistant/mymemo", headers=H).json()["account"]["ok"]
    assert sum(f["grant_type"] == "refresh_token" for f in fake.token_forms) == 1


@pytest.mark.parametrize(("usage", "text"), [
    ({"cost_usd": 3.456, "cost_usd_included": 10}, "$3.45 of $10.00 used this month"),
    ({"cost_usd": 12, "cost_usd_included": None, "input_tokens_included": None, "output_tokens_included": None},
     "$12.00 used this month, no cap"),
    ({"cost_usd": 1.5, "cost_usd_included": None}, "$1.50 used this month"),  # the token caps say what is included
    ({}, None),  # an older service: tokens only
    ({"cost_usd": "1.50", "cost_usd_included": True}, None),
    ({"cost_usd": -1, "cost_usd_included": 10}, None),
])
def test_the_month_s_use_in_dollars(vault, fake, api, usage, text):
    signed_in(vault, fake)
    base = {k: v for k, v in fake.models["usage"].items() if not k.startswith("cost_")}
    fake.models["usage"] = {**base, **usage}
    acct = api.get("/api/assistant/mymemo", headers=H).json()["account"]
    assert acct["ok"] and mymemo.cost_text(acct["usage"]) == text
    assert acct["usage"]["input_tokens"] == base["input_tokens"]  # the tokens stay, as secondary
    if not usage:
        assert acct["usage"]["cost_usd"] is None and acct["usage"]["cost_usd_included"] is None


def test_cli_plan_shows_the_month_s_use_and_a_used_up_allowance(vault, fake):
    signed_in(vault, fake)
    fake.models["usage"] = {**fake.models["usage"], "cost_usd": 10.02}
    fake.fail("quota_exceeded")
    assert ask(Assistant.from_settings(vault), "t") is None
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "plan"]).stdout
    lines = out.splitlines()
    assert "Usage:  [██████████] 100%  $10.02 of $10.00 used this month: used up, resets November 1" in lines
    assert "        Limited-time offer: $10.00 of use this month instead of the regular $5.00." in lines  # models.json
    assert "        1,200 of 10,000 input tokens, 340 of 2,000 output tokens, 7 requests" in lines
    assert "Paused: This month's included use is used up; it resets November 1." in out

    fake.models["usage"] |= {"cost_usd": 6.2, "cost_usd_included": 5.0, "cost_usd_regular_included": 5.0}
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "plan"]).stdout
    assert "Usage:  [██████████] 100%  $6.20 of $5.00 used this month: used up, resets November 1" in out.splitlines()
    assert "offer" not in out  # the regular allowance: no offer wording
    fake.models["usage"] |= {"cost_usd": 3.1}
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "plan"]).stdout
    assert "Usage:  [██████░░░░] 62%  $3.10 of $5.00 used this month" in out.splitlines()

    for k in ("cost_usd", "cost_usd_included", "cost_usd_regular_included", "offer_ends_at"):  # an older service
        del fake.models["usage"][k]
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "plan"]).stdout
    assert "Usage:  1,200 of 10,000 input tokens, 340 of 2,000 output tokens, 7 requests" in out.splitlines()
    assert "used this month" not in out and "[" not in out and "offer" not in out


def test_cli_plan_before_buying_says_what_it_includes(vault, fake):
    signed_in(vault, fake)
    fake.models["plan"] = {**fake.models["plan"], "active": False, "status": "none"}
    fake.models["usage"] |= {"cost_usd": 0, "offer_ends_at": "2026-11-30T00:00:00Z"}
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "plan"]).stdout
    lines = out.splitlines()
    assert "        The plan includes up to $5.00 of Claude Haiku 5.5 use a month." in lines
    assert "        Limited-time offer: $10.00 of use this month instead of the regular $5.00, until November 30." in lines
    assert "Get the plan:" in out and "[" not in out
    fake.models["usage"] |= {"cost_usd_regular_included": None}  # no regular amount named: no offer
    out = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "plan"]).stdout
    assert "        The plan includes up to $10.00 of Claude Haiku 5.5 use a month." in out.splitlines()
    assert "offer" not in out


@pytest.mark.parametrize(("usage", "offer"), [
    ({"cost_usd_included": 10.0, "cost_usd_regular_included": 5.0, "offer_ends_at": None},
     "Limited-time offer: $10.00 of use this month instead of the regular $5.00."),
    ({"cost_usd_included": 10, "cost_usd_regular_included": 5, "offer_ends_at": "2026-11-30T23:59:59Z"},
     "Limited-time offer: $10.00 of use this month instead of the regular $5.00, until November 30."),
    ({"cost_usd_included": 5.0, "cost_usd_regular_included": 5.0}, None),  # the regular allowance
    ({"cost_usd_included": 4.0, "cost_usd_regular_included": 5.0}, None),
    ({"cost_usd_included": None, "cost_usd_regular_included": 5.0}, None),  # no cap
    ({"cost_usd_included": 10.0, "cost_usd_regular_included": None}, None),
    ({"cost_usd_included": 10.0, "cost_usd_regular_included": -5, "offer_ends_at": "soon"}, None),
    ({"cost_usd_included": 10.0, "cost_usd_regular_included": "5"}, None),
    ({}, None),  # an older service
])
def test_a_limited_time_offer_is_named_only_when_the_service_includes_more_than_the_regular_use(
        vault, fake, api, usage, offer):
    signed_in(vault, fake)
    fake.models["usage"] = {**{k: v for k, v in fake.models["usage"].items()
                               if k not in ("cost_usd_included", "cost_usd_regular_included", "offer_ends_at")}, **usage}
    acct = api.get("/api/assistant/mymemo", headers=H).json()["account"]
    assert mymemo.offer_text(acct["usage"]) == offer
    ends = acct["usage"]["offer_ends_at"]
    assert ends in (None, "2026-11-30T23:59:59+00:00") and (ends is None) == (usage.get("offer_ends_at") in (None, "soon"))
    regular = acct["usage"]["cost_usd_regular_included"]
    assert regular is None or isinstance(regular, float) and regular >= 0


@pytest.mark.parametrize(("used", "included", "bar"), [
    (6.2, 10.0, "[██████░░░░] 62%"),
    (0.0003, 10.0, "[░░░░░░░░░░] 0%"),
    (9.96, 10.0, "[█████████░] 99%"),  # full only once it is used up
    (10.0, 10.0, "[██████████] 100%"),
    (12.5, 10.0, "[██████████] 100%"),
    (1.0, 0.0, "[██████████] 100%"),
    (1.0, None, None),  # no cap: no bar
    (None, 10.0, None),  # no dollars: no bar
])
def test_the_usage_bar(used, included, bar):
    assert mymemo.usage_bar({"cost_usd": used, "cost_usd_included": included}) == bar


@pytest.mark.parametrize(("used", "text", "bar"), [
    (9.999, "$9.99 of $10.00 used this month", "[█████████░] 99%"),  # never shows the cap before it is reached
    (0.29, "$0.29 of $10.00 used this month", "[░░░░░░░░░░] 3%"),  # not floored to $0.28 by floating point
    (10.0, "$10.00 of $10.00 used this month", "[██████████] 100%"),
    (10.029, "$10.02 of $10.00 used this month", "[██████████] 100%"),
])
def test_the_month_s_use_is_floored_to_the_cent(used, text, bar):
    usage = {"cost_usd": used, "cost_usd_included": 10.0}
    assert mymemo.cost_text(usage) == text and mymemo.usage_bar(usage) == bar


def test_offered_by_default_and_turned_off_by_the_environment(vault, fake, monkeypatch):
    from openmymemo.assistant import mymemo_offered

    assert mymemo_offered() and mymemo_offered(environ={"OPENMYMEMO_ASSISTANT_MYMEMO": "1"})
    for off in ("0", "false", "No", " off "):
        assert not mymemo_offered(environ={"OPENMYMEMO_ASSISTANT_MYMEMO": off})
        assert mymemo_offered("mymemo", environ={"OPENMYMEMO_ASSISTANT_MYMEMO": off})  # already chosen: kept
    # Offered is not chosen: nothing goes to MyMemo until the person picks it and signs in.
    assert load_settings().kind != "mymemo" and not fake.tasks and not fake.token_forms


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
    assert cloud.load_link() is not None  # OpenMyMemo Cloud stays linked


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
    assert "Signed in: Sam (s***@example.com) with this computer's OpenMyMemo Cloud link" in r.stdout
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
    assert a.json_call("sort", "x", blocks, SCHEMA, task="sort") is not None
    assert a.json_call("import", "x", [f"notes: {SECRET.lower()}"], SCHEMA, task="import.split") is None
    assert a.json_call("import", "x", [Data("y", topic="health")], SCHEMA, task="import.split") is None
    sent = json.dumps([c["body"] for c in fake.tasks], ensure_ascii=False)
    assert len(fake.tasks) == 1 and "sumatriptan" not in sent and "180k" not in sent
    assert "Leads the platform team" in sent


def test_the_fixtures_name_no_prices():
    # Only the use the service meters in dollars (`cost_usd`, `cost_usd_included`); nothing names a price.
    models = fixture("assistant/models.json")
    usage = models["response"]["usage"]
    assert all(isinstance(usage.pop(k), float) for k in ("cost_usd", "cost_usd_included", "cost_usd_regular_included"))
    text = json.dumps([models, fixture("assistant/errors.json")]).lower()
    assert "$" not in text and "usd" not in text and "price" not in text


# ---- a request is bound to the sign-in it started with ---------------------------------------------------------------


def grants(fake, kind: str = "refresh_token") -> int:
    return sum(f["grant_type"] == kind for f in fake.token_forms)


def test_signing_out_and_in_as_another_account_mid_request_stops_that_request(vault, fake):
    a = signed_in(vault, fake)  # as Sam, through the OpenMyMemo Cloud link
    old = raw()["mymemo_token"]
    bob = {}

    def switch():  # while the request is in flight: sign out, and sign in as someone else
        mymemo.sign_out()
        tokens = fake.tokens_for_keep()
        mymemo.keep(fake.base, tokens, via="browser", account={"display_name": "Bob"})
        bob.update(tokens)
        return "invalid_token"  # the request's own (revoked) token is refused

    fake.on_task = switch
    refreshes = grants(fake)
    assert ask(a, "some text") is None
    assert a.last_error == mymemo.CHANGED
    # The request never retried with Bob's token, nor spent anyone's refresh token.
    assert fake.task_auth == [f"Bearer {old}"] and grants(fake) == refreshes
    stored = raw()  # Bob's sign-in is intact
    assert stored["mymemo_token"] == bob["access"] and stored["mymemo_refresh"] == bob["refresh"]
    assert stored["mymemo"]["account"] == {"display_name": "Bob"} and "signed_out" not in stored["mymemo"]
    assert a.settings.mymemo_token == old  # the stopped request's settings didn't take Bob's token either
    fresh = Assistant.from_settings(vault)
    assert ask(fresh, "t") is not None and fake.task_auth[-1] == f"Bearer {bob['access']}"


def test_an_old_sign_ins_answer_never_pauses_a_newer_one(vault, fake):
    a = signed_in(vault, fake)

    def switch():
        mymemo.keep(fake.base, fake.tokens_for_keep(), via="browser")
        return "quota_exceeded"

    fake.on_task = switch
    assert ask(a, "t") is None
    assert "hold" not in raw()["mymemo"] and Assistant.from_settings(vault).available("sort")


@pytest.mark.parametrize("how", ["new sign-in", "service edited"])
def test_a_service_change_mid_request_never_sends_tokens_across_services(how, vault, fake):
    other = FakeAssistantService().start()
    try:
        a = signed_in(vault, fake)
        old = raw()["mymemo_token"]
        fake.expire_access()

        def switch():
            if how == "new sign-in":
                mymemo.keep(other.base, other.tokens_for_keep(), via="browser")
            else:  # same generation, another service written in (by hand, or a bug elsewhere)
                stored = raw()
                stored["mymemo"]["service"] = other.base
                settings_mod._write(stored)
            return None

        fake.on_task = switch
        assert ask(a, "t") is None and a.last_error == mymemo.CHANGED
        assert fake.task_auth == [f"Bearer {old}"] and grants(fake) == 0  # no refresh at either service
        assert other.task_auth == [] and other.model_calls == [] and grants(other) == 0
        assert all(f.get("refresh_token") != raw().get("mymemo_refresh") for f in fake.token_forms)
    finally:
        other.stop()


def test_a_refresh_by_a_second_process_mid_request_is_reused_once(vault, fake):
    import subprocess
    import sys

    a = signed_in(vault, fake)
    fake.expire_access()
    script = ("from openmymemo.assistant import load_settings, mymemo\n"
              "s = load_settings()\n"
              "print(mymemo.refresh(s, mymemo.session(s))[1])\n")

    def other_process():  # the request's token has expired; another process refreshes first
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                             env=os.environ.copy())
        assert out.stdout.strip() == "ok", out.stderr
        return None

    fake.on_task = other_process
    assert ask(a, "t") is not None
    assert grants(fake) == 1  # the in-flight request reused the second process's token instead of refreshing
    assert fake.task_auth[-1] == f"Bearer {raw()['mymemo_token']}" and raw()["mymemo_refresh"] == fake.refresh
    assert raw()["mymemo_refresh"] not in fake.spent


def test_a_refresh_due_before_the_request_checks_the_sign_in_too(vault, fake):
    signed_in(vault, fake)
    stored = raw()
    stored["mymemo"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    settings_mod._write(stored)
    a = Assistant.from_settings(vault)
    sess = mymemo.session(a.settings)
    mymemo.sign_out()  # between capturing the sign-in and refreshing it
    mymemo.keep(fake.base, fake.tokens_for_keep(), via="browser")
    newer = raw()
    assert mymemo.access_token(a.settings, sess) == (None, "changed")
    assert mymemo.refresh(a.settings, sess) == (None, "changed")
    assert raw() == newer and grants(fake) == 0


def test_a_token_that_doesnt_match_the_stored_sign_in_is_never_sent(vault, fake):
    a = signed_in(vault, fake)
    a.settings.mymemo_token = "oma_TEST_ONLY_from_elsewhere"  # e.g. read from the Keychain mid-change
    assert mymemo.session(a.settings) is None
    assert ask(a, "t") is None and fake.task_auth == []


def test_signing_out_never_forgets_a_sign_in_made_meanwhile(vault, fake, monkeypatch):
    signed_in(vault, fake)
    real = mymemo._oauth
    newer = {}

    def revoke_slowly(service, path, form):
        result = real(service, path, form)
        if path == "revoke":  # another process signs in while this one is still signing out
            newer.update(fake.tokens_for_keep())
            mymemo.keep(fake.base, newer, via="browser")
        return result

    monkeypatch.setattr(mymemo, "_oauth", revoke_slowly)
    mymemo.sign_out()
    assert raw()["mymemo_token"] == newer["access"] and load_settings().mymemo_token == newer["access"]


# ---- the account shown is only one this sign-in authenticated --------------------------------------------------------


def test_a_browser_sign_in_never_shows_the_mymemo_cloud_account(vault, fake, api):
    cloud.save_link(cloud.Link(service=fake.base, device_id="dev-1", device_token=fake.token, private_key="",
                               key_id="", vault=str(vault.root),
                               member={"display_name": "Alice", "email_masked": "a***@example.com"}))
    api.patch("/api/assistant", headers=H, json={"enabled": True, "kind": "mymemo"})
    url = api.post("/api/assistant/mymemo/signin", headers=H, json={"browser": True}).json()["url"]
    r = api.get(callback_path(fake.authorize(url)))  # the person signs in as Bob
    assert r.status_code == 200 and "Alice" not in r.text and "a***" not in r.text
    s = api.get("/api/assistant", headers=H).json()
    assert s["mymemo"]["signed_in"] and s["mymemo"]["account"] is None and s["mymemo"]["via"] == "browser"
    assert "Alice" not in settings_mod.settings_path().read_text()
    # Once the service names the account it signed in (optional `member` in /models), that is shown.
    fake.member = fixture("assistant/models.json")["member"]
    assert api.get("/api/assistant/mymemo", headers=H).json()["account"]["ok"]
    s = api.get("/api/assistant", headers=H).json()
    assert s["mymemo"]["account"] == {"display_name": "Bob", "email_masked": "b***@example.com"}
    assert "Alice" not in settings_mod.settings_path().read_text()


def test_the_cli_shows_no_account_for_a_browser_sign_in(vault, fake):
    mymemo.keep(fake.base, fake.tokens_for_keep(), via="browser")
    save_settings(enabled=True, kind="mymemo")
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "status"])
    assert "Signed in: a MyMemo account" in r.stdout


# ---- a token from OPENMYMEMO_ASSISTANT_API_KEY has pauses of its own ---------------------------------------------------


def env_token(vault, fake, monkeypatch) -> str:
    """MyMemo chosen, with an access token from the environment (a script's), not a stored sign-in."""
    token = fake.tokens_for_keep()["access"]
    monkeypatch.setenv("OPENMYMEMO_ASSISTANT_API_KEY", token)
    save_settings(enabled=True, kind="mymemo")
    ensure_connector(vault, regrant=True)
    assert load_settings().key_store == "env"
    return token


@pytest.mark.parametrize("name", ["rate_limit_exceeded", "model_unavailable", "plan_required", "quota_exceeded"])
def test_an_environment_token_keeps_its_pause_across_fresh_settings(name, vault, fake, monkeypatch):
    env_token(vault, fake, monkeypatch)
    fake.fail(name)
    assert ask(Assistant.from_settings(vault), "t") is None
    fresh = Assistant.from_settings(vault)  # the next feature, settings loaded again
    assert not fresh.available("sort") and ask(fresh, "t") is None
    assert features.import_preview(vault, "markdown", PASTE) and len(fake.tasks) == 1
    if name in ("rate_limit_exceeded", "model_unavailable"):  # the Test button respects Retry-After too
        assert not Assistant.from_settings(vault).test()["ok"] and len(fake.tasks) == 1


def test_another_environment_token_or_service_isnt_paused_by_the_first(vault, fake, monkeypatch):
    env_token(vault, fake, monkeypatch)
    fake.fail("rate_limit_exceeded")
    assert ask(Assistant.from_settings(vault), "t") is None
    monkeypatch.setenv("OPENMYMEMO_ASSISTANT_API_KEY", fake.tokens_for_keep()["access"])  # another token
    assert ask(Assistant.from_settings(vault), "t") is not None
    fake.fail("rate_limit_exceeded")
    assert ask(Assistant.from_settings(vault), "t") is None
    other = FakeAssistantService().start()
    try:  # the same token's identity at another service is another identity
        token = other.tokens_for_keep()["access"]
        monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", other.base)
        monkeypatch.setenv("OPENMYMEMO_ASSISTANT_API_KEY", token)
        a = Assistant.from_settings(vault)
        assert a.available("sort") and ask(a, "t") is not None
        assert other.task_auth == [f"Bearer {token}"]
    finally:
        other.stop()


def test_environment_failures_never_touch_a_stored_sign_in_and_vice_versa(vault, fake, monkeypatch):
    signed_in(vault, fake)
    before = raw()["mymemo"]
    env_token(vault, fake, monkeypatch)
    fake.fail("plan_required")
    assert ask(Assistant.from_settings(vault), "t") is None
    assert raw()["mymemo"] == before  # no pause, account or tokens changed for the stored sign-in
    monkeypatch.delenv("OPENMYMEMO_ASSISTANT_API_KEY")
    stored = Assistant.from_settings(vault)
    assert stored.available("sort") and ask(stored, "t") is not None
    fake.fail("quota_exceeded")  # the stored sign-in's pause ...
    assert ask(Assistant.from_settings(vault), "t") is None
    assert raw()["mymemo"]["hold"]["code"] == "quota_exceeded"
    paused_env = raw().get("mymemo_env")
    env_token(vault, fake, monkeypatch)  # ... doesn't pause the environment's token, nor change its slot
    assert Assistant.from_settings(vault).available("sort")
    assert ask(Assistant.from_settings(vault), "t") is not None
    assert raw()["mymemo"]["hold"]["code"] == "quota_exceeded" and raw().get("mymemo_env") == paused_env


def use_env(monkeypatch, token: str) -> None:
    monkeypatch.setenv("OPENMYMEMO_ASSISTANT_API_KEY", token)


def env_hold_for(token: str, service: str, code: str = "rate_limited", seconds: int = 600) -> tuple[str, dict]:
    until = mymemo._iso(mymemo._now() + timedelta(seconds=seconds))
    return mymemo._env_identity(token, service), {"code": code, "resets_at": None, "plan_url": None, "until": until}


def test_each_environment_token_keeps_its_own_pause(vault, fake, monkeypatch):
    a = env_token(vault, fake, monkeypatch)
    b = fake.tokens_for_keep()["access"]
    fake.fail("rate_limit_exceeded")
    assert ask(Assistant.from_settings(vault), "t") is None  # A paused
    use_env(monkeypatch, b)
    fake.fail("rate_limit_exceeded")
    assert ask(Assistant.from_settings(vault), "t") is None  # B paused
    use_env(monkeypatch, a)
    fresh = Assistant.from_settings(vault)
    assert not fresh.available("sort") and ask(fresh, "t") is None  # A still paused
    assert len(fake.tasks) == 2 and len(raw()["mymemo_env"]["holds"]) == 2


def test_lifting_one_tokens_pause_leaves_the_others(vault, fake, monkeypatch):
    a = env_token(vault, fake, monkeypatch)
    b = fake.tokens_for_keep()["access"]
    fake.fail("rate_limit_exceeded")
    assert ask(Assistant.from_settings(vault), "t") is None
    use_env(monkeypatch, b)
    fake.fail("plan_required")
    assert ask(Assistant.from_settings(vault), "t") is None
    assert mymemo.account(load_settings())["ok"]  # B's plan is active: B's pause is lifted
    assert Assistant.from_settings(vault).available("sort")
    use_env(monkeypatch, a)
    assert not Assistant.from_settings(vault).available("sort")
    assert list(raw()["mymemo_env"]["holds"]) == [mymemo._env_identity(a, fake.base)]


def test_expired_pauses_are_dropped_and_the_map_is_capped(vault, fake, monkeypatch):
    a = env_token(vault, fake, monkeypatch)
    stale = dict([env_hold_for("old-token", fake.base, seconds=-60)])
    many = dict(env_hold_for(f"tok-{i}", fake.base, seconds=1000 + i) for i in range(mymemo.ENV_HOLDS_MAX))
    stored = raw()
    stored["mymemo_env"] = {"holds": {**stale, **many}}
    settings_mod._write(stored)
    fake.fail("rate_limit_exceeded")  # A's pause ends in 20 s: sooner than all the others, and still kept
    assert ask(Assistant.from_settings(vault), "t") is None
    holds = raw()["mymemo_env"]["holds"]
    me = mymemo._env_identity(a, fake.base)
    assert len(holds) == mymemo.ENV_HOLDS_MAX and me in holds and not set(stale) & set(holds)
    assert mymemo._env_identity("tok-0", fake.base) not in holds  # the one ending soonest went
    assert mymemo._env_identity(f"tok-{mymemo.ENV_HOLDS_MAX - 1}", fake.base) in holds


def test_a_pause_in_the_single_slot_of_earlier_builds_is_still_honoured(vault, fake, monkeypatch):
    a = env_token(vault, fake, monkeypatch)
    identity, h = env_hold_for(a, fake.base)
    stored = raw()
    stored["mymemo_env"] = {"identity": identity, "hold": h}
    settings_mod._write(stored)
    assert not Assistant.from_settings(vault).available("sort")
    use_env(monkeypatch, fake.tokens_for_keep()["access"])  # another token's pause: written beside it, as a map
    fake.fail("model_unavailable")
    assert ask(Assistant.from_settings(vault), "t") is None
    assert raw()["mymemo_env"]["holds"][identity] == h and len(raw()["mymemo_env"]["holds"]) == 2
    use_env(monkeypatch, a)
    assert not Assistant.from_settings(vault).available("sort")


def test_with_an_environment_token_the_stored_sign_ins_account_isnt_shown(vault, fake, api, monkeypatch):
    signed_in(vault, fake)  # as Sam, through the OpenMyMemo Cloud link
    env_token(vault, fake, monkeypatch)
    m = api.get("/api/assistant", headers=H).json()["mymemo"]
    assert m["signed_in"] and m["via"] == "env" and m["account"] is None and m["signed_in_at"] is None
    r = CliRunner().invoke(app, ["--vault", str(vault.root), "assistant", "status"])
    assert "Sam" not in r.stdout and "OPENMYMEMO_ASSISTANT_API_KEY" in r.stdout


# ---- with the other Assistant features --------------------------------------------------------------------------------


def test_mymemo_offers_no_embeddings_and_recall_never_asks_it_for_any(vault, fake, api):
    signed_in(vault, fake)
    save_settings(embeddings_model="text-embedding-3-small", features={"recall": True})
    s = load_settings()
    assert s.kind == "mymemo" and not s.embeddings_available
    assert api.get("/api/assistant", headers=H).json()["embeddings"] is False
    vault.add("interests", "Prefers tea over coffee")
    from openmymemo.recall import search

    found = search(vault, None, "what do I like to drink", limit=5, model=True)
    assert found.mode != "hybrid"  # no vectors from MyMemo
    assert not any(p.endswith("/embeddings") for p in fake.paths)  # query expansion may use a task; embeddings never
    assert fake.tasks and all(c["body"]["task"].startswith("recall.") for c in fake.tasks)
    expand = next(c["body"] for c in fake.tasks if c["body"]["task"] == "recall.expand")
    assert expand == {"task": "recall.expand", "blocks": [{"label": "query", "text": "what do I like to drink"}],
                      "inputs": {"languages": ["en"]}}


@pytest.mark.parametrize("name", ["rate_limit_exceeded", "plan_required", "quota_exceeded", "model_unavailable"])
def test_a_mymemo_pause_stops_checks_chat_import_session_reading_and_recall(name, vault, fake, monkeypatch):
    from test_assistant_writes import FIXTURES, app_connector, place_transcript

    from openmymemo.assistant import chats
    from openmymemo.assistant import session as sessions
    from openmymemo.assistant.checks import check_proposal
    from openmymemo.recall import search

    signed_in(vault, fake)
    save_settings(features={"check": True, "session": True, "import": True, "recall": True},
                  session_apps={"claude-code": "allowed"})
    fake.fail(name)
    assert ask(Assistant.from_settings(vault), "t") is None  # MyMemo says: pause
    assert len(fake.tasks) == 1 and mymemo.hold(load_settings())

    con = app_connector(vault)
    assert check_proposal(vault, con, "add", content="Prefers pnpm over npm", memory_id=None, topic=None) is None
    convs = chats.parse_export("claude-export", (FIXTURES / "claude_conversations.json").read_text())
    job = chats.ChatImport(vault, "claude-export", convs)
    plan = job.plan()
    assert not plan["ready"] and plan["reason"] == mymemo.hold_message(mymemo.hold(load_settings()))
    assert job.run() == []
    path = place_transcript()
    assert sessions.work(vault, "claude-code", path, "s-1")["result"] in ("off", "limit")
    vault.add("interests", "Prefers tea over coffee")
    search(vault, None, "tea", limit=5, model=True)
    assert len(fake.tasks) == 1  # nothing more went to MyMemo while paused


def test_every_feature_sends_mymemo_a_task_it_takes(vault, fake, monkeypatch):
    """Each feature's request, as the strict fake checks it against tasks.json (the task, its labels and counts, its
    inputs, nothing else): MyMemo answers every one instead of refusing it as a bad request."""
    from test_assistant_writes import FIXTURES, app_connector, place_transcript

    from openmymemo.assistant import chats, tell, tidy
    from openmymemo.assistant import session as sessions
    from openmymemo.assistant.checks import check_proposal
    from openmymemo.recall import search

    signed_in(vault, fake)
    save_settings(features={"import": True, "sort": True, "recall": True, "check": True, "session": True,
                            "tidy": True, "tell": True}, session_apps={"claude-code": "allowed"})
    for text in ("Prefers tea over coffee", "Prefers green tea", "Lives in Berlin", "喜欢喝茶"):
        vault.add("interests", text)
    features.import_preview(vault, "markdown", PASTE)
    ask(Assistant.from_settings(vault))  # sort_plan only asks about imported memories; the request is the same
    check_proposal(vault, app_connector(vault), "add", content="Prefers pnpm over npm", memory_id=None, topic=None)
    search(vault, None, "what do I like to drink", limit=5, model=True)
    tidy.run(vault, max_calls=1)
    convs = chats.parse_export("claude-export", (FIXTURES / "claude_conversations.json").read_text())
    chats.ChatImport(vault, "claude-export", convs).run()
    sessions.work(vault, "claude-code", place_transcript(), "s-1")
    Assistant.from_settings(vault).test()
    tell.preview(vault, "I live in Berlin now and drink green tea")
    sent = {c["body"]["task"] for c in fake.tasks}
    assert {"import.split", "sort", "check", "recall.expand", "tidy", "import.chats", "session.extract",
            "test", "tell"} <= sent, sent
    assert len(fake.task_auth) == len(fake.tasks)  # none was refused as a bad request


def test_the_tasks_fixture_pins_the_schemas_this_app_checks_answers_with():
    """MyMemo asks the model for the schema in tasks.json; the app cleans the answer with its own. They must agree."""
    from openmymemo.assistant import chats, checks, tell, tidy
    from openmymemo.recall import hybrid

    tasks = fixture("assistant/tasks.json")["tasks"]
    local = {"tell": tell.TELL_SCHEMA, "import.split": features.SPLIT_SCHEMA, "sort": features.SORT_SCHEMA, "check": checks.CHECK_SCHEMA,
             "import.chats": chats.EXTRACT_SCHEMA, "session.extract": chats.EXTRACT_SCHEMA, "tidy": tidy.SCHEMA,
             "recall.expand": hybrid.EXPAND_SCHEMA, "recall.rerank": hybrid.RERANK_SCHEMA}
    for name, schema in local.items():
        assert json.loads(json.dumps(schema)) == tasks[name]["schema"], name
    assert tasks["recall.expand"]["inputs"] == ["languages"]
    assert set(tasks) == {*local, "test"}
