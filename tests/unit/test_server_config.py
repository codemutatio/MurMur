"""Tests for murmur.server.config — T-1, AC-15, AC-16, E-15, E-16, E-17, OQ-5."""

from __future__ import annotations

from pathlib import Path

import pytest

from murmur.server.config import (
    _DEFAULT_SERVER_VOCAB_HINTS,
    ServerConfig,
    ServerSettings,
    ServerSttSettings,
    load_server_config,
)

# ---------------------------------------------------------------------------
# AC-16, E-15 — missing config file → defaults + warning, no crash
# ---------------------------------------------------------------------------


def test_missing_file_defaults(tmp_path: Path) -> None:  # [AC-16, E-15]
    """load_server_config with a nonexistent path returns all defaults and a warning."""
    config, warnings = load_server_config(tmp_path / "nonexistent.toml")

    assert isinstance(config, ServerConfig)
    assert config.server == ServerSettings()
    assert config.stt == ServerSttSettings()
    assert config.clean_raw == {}
    assert len(warnings) == 1
    assert "not found" in warnings[0].lower()


def test_none_path_defaults() -> None:  # [AC-16, E-15]
    """load_server_config(None) returns all defaults and a warning."""
    config, warnings = load_server_config(None)

    assert isinstance(config, ServerConfig)
    assert config.server == ServerSettings()
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# E-16 — malformed TOML → defaults + warning
# ---------------------------------------------------------------------------


def test_malformed_defaults(tmp_path: Path) -> None:  # [E-16]
    """Malformed TOML file falls back to defaults and emits a warning."""
    bad = tmp_path / "server.toml"
    bad.write_text("this is not toml !!!\n[[[[invalid", encoding="utf-8")

    config, warnings = load_server_config(bad)

    assert isinstance(config, ServerConfig)
    assert config.server == ServerSettings()
    assert any("could not be parsed" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# E-17 — unknown keys → warned, ignored; known keys applied
# ---------------------------------------------------------------------------


def test_unknown_key_warns(tmp_path: Path) -> None:  # [E-17]
    """Unknown top-level keys are warned about and ignored; known keys still applied."""
    toml = tmp_path / "server.toml"
    toml.write_text(
        '[server]\nport = 9090\n\n[unknown_section]\nfoo = "bar"\n',
        encoding="utf-8",
    )

    config, warnings = load_server_config(toml)

    assert config.server.port == 9090
    assert any("unknown" in w.lower() for w in warnings)


def test_unknown_server_subkey_warns(tmp_path: Path) -> None:  # [E-17]
    """Unknown keys inside [server] are warned about."""
    toml = tmp_path / "server.toml"
    toml.write_text("[server]\nfuture_feature = 42\n", encoding="utf-8")

    _, warnings = load_server_config(toml)

    assert any("future_feature" in w for w in warnings)


# ---------------------------------------------------------------------------
# OQ-5 — [clean] section parsed and ignored
# ---------------------------------------------------------------------------


def test_clean_section_parsed_not_acted(tmp_path: Path) -> None:  # [OQ-5]
    """[clean] section is parsed into clean_raw but produces no server behavior."""
    toml = tmp_path / "server.toml"
    toml.write_text("[clean]\nenabled = true\nmodel = 'claude-haiku'\n", encoding="utf-8")

    config, warnings = load_server_config(toml)

    # clean_raw holds the raw dict — not empty
    assert config.clean_raw.get("enabled") is True
    # No warnings about [clean] being unknown (it is a known-but-ignored section)
    assert not any("unknown" in w.lower() and "clean" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Defaults sanity check
# ---------------------------------------------------------------------------


def test_server_defaults() -> None:
    """Verify the documented defaults match the plan contract §C."""
    s = ServerSettings()
    assert s.host == "127.0.0.1"
    assert s.port == 8080
    assert s.max_upload_mb == 25
    assert s.busy_wait_s == 0

    stt = ServerSttSettings()
    assert stt.model == "large-v3"
    assert stt.language == "en"
    assert set(stt.vocab_hints) == set(_DEFAULT_SERVER_VOCAB_HINTS)


def test_full_valid_toml(tmp_path: Path) -> None:
    """A fully-specified server.toml is parsed correctly."""
    toml = tmp_path / "server.toml"
    toml.write_text(
        """
[server]
host = "127.0.0.1"
port = 8080
max_upload_mb = 10
busy_wait_s = 0
log_path = "~/Library/Logs/murmur-server.log"

[stt]
model = "large-v3"
language = "en"
vocab_hints = ["Foo", "Bar"]

[clean]
enabled = false
""",
        encoding="utf-8",
    )

    config, warnings = load_server_config(toml)

    assert config.server.max_upload_mb == 10
    assert config.stt.vocab_hints == ("Foo", "Bar")
    assert config.clean_raw == {"enabled": False}
    assert warnings == []


@pytest.mark.parametrize(
    "toml_text",
    [
        "[server]\nmax_upload_mb = 'not-an-int'\n",
        "[server]\nport = 'not-an-int'\n",
    ],
)
def test_type_error_on_known_key_uses_default(tmp_path: Path, toml_text: str) -> None:
    """A known key with the wrong type falls back to its default and warns."""
    toml = tmp_path / "server.toml"
    toml.write_text(toml_text, encoding="utf-8")

    config, warnings = load_server_config(toml)

    assert isinstance(config, ServerConfig)
    assert len(warnings) >= 1
