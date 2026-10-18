"""OpenMyMemo Assistant: optional model help (product-design.md §17). Off by default; every feature that uses it
has a rule-based fallback, and nothing here runs until the person turns it on.

* ``settings``: per-computer settings in ``assistant.json`` (never in the folder), Keychain, environment overrides,
  monthly usage.
* ``client``: the endpoint client (OpenAI-compatible chat or Anthropic Messages API, over httpx2).
* ``schema``: checking the model's JSON against a fixed schema.
* ``core``: ``Assistant`` — ``available(feature)``, ``json_call(...)``, ``propose(...)`` — and the ``assistant``
  connector.
* ``features``: model-powered import splitting and sorting suggestions (§17.5).
* ``tidy``: Tidy up (§17.6), a batch of proposals, and its schedule in the panel's process.

The rest of OpenMyMemo imports from here.
"""

from .client import Failure
from .core import (
    CONNECTOR_ID,
    CONNECTOR_KIND,
    CONNECTOR_NAME,
    Assistant,
    Data,
    ensure_connector,
    where_text_goes,
)
from .settings import FEATURES, KINDS, PROTOCOLS, Settings, SettingsError, load_settings, save_settings

__all__ = [
    "CONNECTOR_ID", "CONNECTOR_KIND", "CONNECTOR_NAME", "FEATURES", "KINDS", "PROTOCOLS", "Assistant", "Data",
    "Failure", "Settings", "SettingsError", "ensure_connector", "load_settings", "save_settings",
    "where_text_goes",
]
