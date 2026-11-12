"""La clé GMGN générée doit être reconnue par une bibliothèque cryptographique standard."""
from __future__ import annotations

import base64

import pytest

from core.logs import redact
from security.gmgn_keys import GmgnKeyError, GmgnKeyStore

crypto = pytest.importorskip("cryptography.hazmat.primitives.serialization")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402


def test_generated_keys_are_standard_ed25519_pem(tmp_path):
    store = GmgnKeyStore(tmp_path, use_keyring=False)
    created = store.create()
    public = crypto.load_pem_public_key(created["public_pem"].encode())
    assert isinstance(public, Ed25519PublicKey)
    private_pem = (tmp_path / "gmgn_signing_key.pem").read_text()
    private = crypto.load_pem_private_key(private_pem.encode(), password=None)
    assert isinstance(private, Ed25519PrivateKey)
    # même clé publique reconstruite, et signature vérifiable par la clé publique
    assert store.public_pem() == created["public_pem"]
    message = "/v1/trade/swap:client_id=abc&timestamp=1:{\"chain\":\"sol\"}:1"
    public.verify(base64.b64decode(store.sign(message)), message.encode())
    # la clé secrète n'apparaît jamais dans les journaux
    assert private_pem.strip() not in redact(f"log {private_pem}")


def test_existing_key_is_never_replaced_silently(tmp_path):
    store = GmgnKeyStore(tmp_path, use_keyring=False)
    first = store.create()["public_pem"]
    with pytest.raises(GmgnKeyError):
        store.create()
    assert store.public_pem() == first
    assert store.create(replace=True)["public_pem"] != first
    store.delete()
    assert not store.exists() and store.public_pem() is None
    with pytest.raises(GmgnKeyError):
        store.sign("x")
