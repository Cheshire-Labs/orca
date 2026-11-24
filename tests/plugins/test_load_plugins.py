"""Plugins are enabled by server config, never by submitted code: one plugin per config entry."""

import pytest

from orca.daemon.app import plugins_from_env
from orca.plugins.config import WebhookPluginConfig
from orca.plugins.webhook import WebhookPlugin
from orca.runtime.startup import load_plugins


def test_no_configs_load_no_plugins() -> None:
    assert list(load_plugins([]).plugins) == []


def test_a_webhook_config_loads_a_webhook_plugin() -> None:
    plugins = load_plugins([
        WebhookPluginConfig(url="https://lims.example/hook", headers={"Authorization": "Bearer t"}),
    ]).plugins

    assert len(plugins) == 1
    assert isinstance(plugins[0], WebhookPlugin)


def test_the_daemon_reads_its_plugins_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_PLUGINS", '[{"type": "webhook", "url": "https://lims.example/hook"}]')

    plugins = plugins_from_env().plugins

    assert len(plugins) == 1
    assert isinstance(plugins[0], WebhookPlugin)


@pytest.mark.parametrize("raw", [
    '[{"type": "webhook"}]',
    '[{"type": "slack", "url": "https://x"}]',
    '[{"type": "webhook", "url": "https://x", "nope": 1}]',
], ids=["missing url", "unknown type", "unknown field"])
def test_a_bad_plugin_config_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("ORCA_PLUGINS", raw)

    with pytest.raises(ValueError, match="ORCA_PLUGINS"):
        plugins_from_env()
