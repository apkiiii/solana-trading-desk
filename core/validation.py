"""Validations simples réutilisées partout."""
from __future__ import annotations

from solders.pubkey import Pubkey


def is_valid_address(address: str) -> bool:
    """Vrai si ``address`` est une adresse publique Solana valide (base58, 32 octets)."""
    if not isinstance(address, str) or not (32 <= len(address) <= 44):
        return False
    try:
        Pubkey.from_string(address)
    except Exception:  # noqa: BLE001 - solders lève plusieurs types d'erreurs
        return False
    return True
