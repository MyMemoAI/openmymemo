import threading
import time

import openmymemo.vault as vault_mod
from openmymemo.vault import Vault


def test_an_app_starting_while_another_creates_the_vault_waits_for_it(tmp_path, monkeypatch):
    # Claude Desktop starts two copies of the server on first install; the second one used to find a
    # half-created folder and refuse it ("isn't empty and isn't an OpenMyMemo vault").
    first_write = threading.Event()
    real_write = vault_mod._atomic_write

    def slow_write(path, text):
        first_write.set()
        time.sleep(0.01)
        real_write(path, text)

    monkeypatch.setattr(vault_mod, "_atomic_write", slow_write)
    root = tmp_path / "OpenMyMemo"
    errors: list[Exception] = []

    def open_it():
        try:
            Vault.open_or_create(root)
        except Exception as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    first = threading.Thread(target=open_it)
    first.start()
    assert first_write.wait(10)  # the first app is now halfway through creating the vault
    later = [threading.Thread(target=open_it) for _ in range(3)]
    for t in later:
        t.start()
    for t in [first, *later]:
        t.join(timeout=30)
    assert errors == []
    v = Vault(root)
    assert v.is_initialized() and len(list(v.memories_dir.glob("*.md"))) == 14
