"""`onboarded` in settings.yaml: whether the panel opens on its first-run guide (MEMORY_FORMAT.md §6.1)."""

import pytest
import yaml
from typer.testing import CliRunner

from openmymemo.cli import app
from openmymemo.connectors import Connectors
from openmymemo.vault import Vault, VaultError


def stored(vault: Vault) -> dict:
    return yaml.safe_load(vault.settings_path.read_text(encoding="utf-8"))


def without_key(vault: Vault) -> Vault:
    """The folder as a version before the guide left it: settings.yaml has no `onboarded`."""
    data = stored(vault)
    data.pop("onboarded")
    vault.settings_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return Vault(vault.root)


def test_new_folders_start_not_onboarded(tmp_path):
    for vault in (Vault.create(tmp_path / "a"), Vault.open_or_create(tmp_path / "b")):
        assert stored(vault) == {"format": 1, "require_approval": False, "onboarded": False}
        assert vault.settings()["onboarded"] is False
    assert CliRunner().invoke(app, ["--vault", str(tmp_path / "c"), "init"]).exit_code == 0
    assert stored(Vault(tmp_path / "c"))["onboarded"] is False


def test_a_stored_value_wins_over_what_is_in_the_folder(vault):
    vault.add("work", "CTO at Acme")
    Connectors(vault).ensure("claude-code")
    assert vault.settings()["onboarded"] is False  # connecting an app during the guide doesn't end it


def test_without_the_key_an_empty_folder_is_not_onboarded(vault):
    assert without_key(vault).settings() == {"require_approval": False, "onboarded": False}


@pytest.mark.parametrize("status", ["active", "archived"])
def test_without_the_key_a_folder_with_memories_is_onboarded(vault, status):
    m = vault.add("work", "CTO at Acme")
    if status == "archived":
        vault.archive(m.id)
    assert without_key(vault).settings()["onboarded"] is True


@pytest.mark.parametrize("revoked", [False, True])
def test_without_the_key_a_folder_with_a_connector_is_onboarded(vault, revoked):
    Connectors(vault).ensure("claude-desktop", "Claude Desktop")
    if revoked:
        Connectors(vault).remove("claude-desktop")
        assert not Connectors(vault).list()
    assert without_key(vault).settings()["onboarded"] is True


def test_a_value_that_isnt_true_or_false_counts_as_missing(vault):
    vault.add("work", "CTO at Acme")
    vault.settings_path.write_text("format: 1\nrequire_approval: false\nonboarded:\n", encoding="utf-8")
    assert Vault(vault.root).settings()["onboarded"] is True


def test_update_persists_onboarded_and_keeps_unknown_keys(vault):
    vault.settings_path.write_text("format: 1\nrequire_approval: false\nonboarded: false\ntheme: dark\n",
                                   encoding="utf-8")
    assert vault.update_settings(onboarded=True) == {"require_approval": False, "onboarded": True}
    assert stored(vault) == {"format": 1, "require_approval": False, "onboarded": True, "theme": "dark"}
    assert Vault(vault.root).settings()["onboarded"] is True
    vault.update_settings(onboarded=False)
    assert Vault(vault.root).settings()["onboarded"] is False
    with pytest.raises(VaultError):
        vault.update_settings(theme="light")


def test_another_change_leaves_a_computed_value_computed(vault):
    vault = without_key(vault)
    assert vault.update_settings(require_approval=True) == {"require_approval": True, "onboarded": False}
    assert "onboarded" not in stored(vault)
    vault.add("work", "CTO at Acme")  # used later, e.g. from the CLI: counts as onboarded from then on
    assert vault.settings()["onboarded"] is True

