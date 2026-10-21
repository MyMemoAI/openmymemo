import threading

import pytest

from openmymemo import cloud
from openmymemo.vault import Vault


@pytest.fixture(autouse=True)
def _no_real_cloud_link(tmp_path_factory, monkeypatch):
    """Every test (and every subprocess it starts) gets its own home, its own OpenMyMemo Cloud folder and a local
    service URL that nothing listens on, so no test reads or writes this computer's real cloud.json (or anything
    else in the real home) or reaches MyMemo. Kept outside `tmp_path`, which some tests list."""
    home = str(tmp_path_factory.mktemp("home"))
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)  # what Path.home() reads on Windows
    monkeypatch.setenv("OPENMYMEMO_CLOUD_DIR", str(tmp_path_factory.mktemp("cloud") / "config"))
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", "http://127.0.0.1:9")
    # The Assistant: settings in that same folder, never the real Keychain, and no variables from the shell.
    import os

    for name in [n for n in os.environ if n.startswith("OPENMYMEMO_ASSISTANT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("OPENMYMEMO_ASSISTANT_KEYCHAIN", "0")


@pytest.fixture(autouse=True)
def _no_bridge_outlives_its_test(monkeypatch):
    """Every OpenMyMemo Cloud bridge a test starts (directly, or through the panel's link callback) is stopped when the
    test ends. A bridge left running re-reads cloud.json from whatever folder the next test points it at, and the
    fixtures all share one device id, so it would quietly connect to a later test's fake service."""
    started: list[cloud.Cloud] = []
    start = cloud.Cloud.start

    def tracked_start(self, *args, **kwargs):
        started.append(self)
        return start(self, *args, **kwargs)

    monkeypatch.setattr(cloud.Cloud, "start", tracked_start)
    yield
    for c in started:
        c.stop()
    leaked = [t for t in threading.enumerate() if t.name == "openmymemo-cloud" and t.is_alive()]
    assert not leaked, "an OpenMyMemo Cloud bridge is still running after the test"


@pytest.fixture
def vault(tmp_path):
    return Vault.create(tmp_path / "vault")
