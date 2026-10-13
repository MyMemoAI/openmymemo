import pytest

from openmymemo.vault import Vault


@pytest.fixture(autouse=True)
def _no_real_cloud_link(tmp_path_factory, monkeypatch):
    """Every test (and every subprocess it starts) gets its own home, its own MyMemo Cloud folder and a local
    service URL that nothing listens on, so no test reads or writes this computer's real cloud.json (or anything
    else in the real home) or reaches MyMemo. Kept outside `tmp_path`, which some tests list."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("OPENMYMEMO_CLOUD_DIR", str(tmp_path_factory.mktemp("cloud") / "config"))
    monkeypatch.setenv("OPENMYMEMO_CLOUD_URL", "http://127.0.0.1:9")


@pytest.fixture
def vault(tmp_path):
    return Vault.create(tmp_path / "vault")
