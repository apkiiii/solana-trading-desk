from __future__ import annotations

import logging

import pytest

from core.logs import RedactingFormatter, redact, register_secret
from security.secrets import SecretsError, load_secrets, mask


def test_api_key_in_url_is_masked():
    text = "GET https://mainnet.helius-rpc.com/?api-key=abcd1234efgh5678 failed"
    assert "abcd1234efgh5678" not in redact(text)


def test_registered_secret_is_masked_everywhere():
    register_secret("SuperSecretValue123")
    assert "SuperSecretValue123" not in redact("valeur=SuperSecretValue123 ; encore SuperSecretValue123")


def test_keypair_int_array_is_masked():
    keypair = "[" + ",".join(str(i % 256) for i in range(64)) + "]"
    assert redact(f"cle {keypair}") == "cle [CLE MASQUEE]"


def test_formatter_masks_exceptions():
    register_secret("TracebackSecret999")
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError("boom TracebackSecret999")
    except RuntimeError:
        import sys
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "erreur", None, sys.exc_info())
    assert "TracebackSecret999" not in formatter.format(record)


def test_env_with_private_key_is_refused(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HELIUS_API_KEY=abc12345678\nWALLET_PRIVATE_KEY=5abc\n", encoding="utf-8")
    with pytest.raises(SecretsError):
        load_secrets(env, environ={})


def test_env_loading_and_masking(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HELIUS_API_KEY=helius-key-1234567\nJUPITER_API_KEY=\n", encoding="utf-8")
    secrets = load_secrets(env, environ={})
    assert secrets.helius_api_key == "helius-key-1234567"
    assert secrets.jupiter_api_key == ""
    assert "helius-key-1234567" not in repr(secrets)
    assert mask("helius-key-1234567").startswith("heli")
    assert "helius-key-1234567" not in redact("cle helius-key-1234567")


def test_save_api_keys_keeps_other_lines_and_refuses_unknown(tmp_path):
    from security.secrets import save_api_keys
    env = tmp_path / ".env"
    env.write_text("# commentaire\nHELIUS_API_KEY=old-value-123\n", encoding="utf-8")
    save_api_keys({"HELIUS_API_KEY": "new-value-456", "JUPITER_API_KEY": "jup-value-789"}, env)
    text = env.read_text(encoding="utf-8")
    assert "# commentaire" in text and "HELIUS_API_KEY=new-value-456" in text and "old-value-123" not in text
    assert load_secrets(env, environ={}).jupiter_api_key == "jup-value-789"
    with pytest.raises(SecretsError):
        save_api_keys({"WALLET_PRIVATE_KEY": "x"}, env)
    with pytest.raises(SecretsError):
        save_api_keys({"HELIUS_API_KEY": "a b"}, env)
