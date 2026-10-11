import pytest

from openmymemo.vault import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault.create(tmp_path / "vault")
