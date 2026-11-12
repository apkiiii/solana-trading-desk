"""Détection des swaps d'un wallet à partir d'une transaction ``getTransaction`` (jsonParsed).

Méthode : on compare les soldes AVANT et APRÈS la transaction pour le wallet suivi
(SOL natif, SOL enveloppé « wSOL » et tokens dont il est propriétaire). On ne dépend donc
ni du DEX utilisé, ni de qui a signé la transaction (utile pour les apps « gasless »
qui signent à la place de l'utilisateur).

- BUY  : le wallet perd du SOL ou de l'USDC et reçoit exactement un autre token.
- SELL : le wallet perd exactement un token et reçoit du SOL ou de l'USDC.
- OTHER : échange token contre token, ou cas ambigu -> jamais copié.

Limite connue : pour un achat payé en SOL, le montant inclut le dépôt (rent) d'ouverture
du compte token (environ 0,0015 SOL), car il sort du même solde.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from data.models import SwapEvent

SOL_MINT = "So11111111111111111111111111111111111111112"   # aussi l'adresse du wSOL
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_DECIMALS = 9
DUST_LAMPORTS = 100_000          # 0,0001 SOL : en dessous, variation ignorée
DUST_USDC_RAW = 10_000           # 0,01 USDC


def _account_keys(tx: dict[str, Any]) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    raw_keys = message.get("accountKeys") or []
    keys = [key["pubkey"] if isinstance(key, dict) else str(key) for key in raw_keys]
    # En encodage jsonParsed (objets), les adresses des tables de correspondance sont déjà
    # incluses ; en encodage json simple (chaînes), il faut les ajouter.
    if raw_keys and not any(isinstance(key, dict) for key in raw_keys):
        loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
        keys.extend(loaded.get("writable") or [])
        keys.extend(loaded.get("readonly") or [])
    return keys


def _token_deltas(meta: dict[str, Any], owner: str) -> tuple[dict[str, int], dict[str, int]]:
    """Variation brute par mint pour les comptes token appartenant à ``owner`` + décimales."""
    pre: dict[int, tuple[str, int, int]] = {}
    post: dict[int, tuple[str, int, int]] = {}
    for source, target in ((meta.get("preTokenBalances") or [], pre), (meta.get("postTokenBalances") or [], post)):
        for bal in source:
            if bal.get("owner") != owner:
                continue
            amount = bal.get("uiTokenAmount") or {}
            target[int(bal["accountIndex"])] = (bal["mint"], int(amount.get("amount", "0")),
                                                int(amount.get("decimals", 0)))
    deltas: dict[str, int] = defaultdict(int)
    decimals: dict[str, int] = {}
    for index in set(pre) | set(post):
        before = after = 0
        if index in pre:
            mint, before, dec = pre[index]
        if index in post:
            mint, after, dec = post[index]
        deltas[mint] += after - before
        decimals[mint] = dec
    return dict(deltas), decimals


def parse_swap(tx: dict[str, Any] | None, wallet: str, *, usdc_mint: str = USDC_MINT) -> SwapEvent | None:
    """Retourne le swap effectué par ``wallet`` dans ``tx``, ou None (échec, pas de swap, wallet absent)."""
    if not tx:
        return None
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return None
    keys = _account_keys(tx)
    signatures = (tx.get("transaction") or {}).get("signatures") or []
    if not signatures:
        return None

    fee = int(meta.get("fee") or 0)
    fee_payer = keys[0] if keys else None
    paid_by_wallet = fee_payer == wallet

    native_delta = 0
    if wallet in keys:
        index = keys.index(wallet)
        pre_bal, post_bal = meta.get("preBalances") or [], meta.get("postBalances") or []
        if index < len(pre_bal) and index < len(post_bal):
            native_delta = int(post_bal[index]) - int(pre_bal[index])
            if paid_by_wallet:
                native_delta += fee  # on retire les frais réseau du calcul du montant échangé

    token_deltas, decimals = _token_deltas(meta, wallet)
    if wallet not in keys and not token_deltas:
        return None

    sol_delta = native_delta + token_deltas.pop(SOL_MINT, 0)
    usdc_delta = token_deltas.pop(usdc_mint, 0)
    others = {mint: d for mint, d in token_deltas.items() if d != 0}

    if not others:
        return None  # aucun token échangé : transfert de SOL/USDC ou opération sans swap

    quote_mint: str | None = None
    quote_delta = 0
    quote_decimals: int | None = None
    if abs(sol_delta) >= DUST_LAMPORTS:
        quote_mint, quote_delta, quote_decimals = SOL_MINT, sol_delta, SOL_DECIMALS
    elif abs(usdc_delta) >= DUST_USDC_RAW:
        quote_mint, quote_delta, quote_decimals = usdc_mint, usdc_delta, decimals.get(usdc_mint, 6)

    side = "OTHER"
    token_mint: str | None = None
    token_amount = 0
    if quote_mint is not None and len(others) == 1:
        token_mint, delta = next(iter(others.items()))
        if quote_delta < 0 < delta:
            side, token_amount = "BUY", delta
        elif delta < 0 < quote_delta:
            side, token_amount = "SELL", -delta
    if side == "OTHER":
        token_mint = next(iter(others))
        token_amount = abs(others[token_mint])

    return SwapEvent(
        signature=signatures[0],
        wallet=wallet,
        slot=int(tx.get("slot") or 0),
        block_time=int(tx["blockTime"]) if tx.get("blockTime") is not None else None,
        side=side,  # type: ignore[arg-type]
        token_mint=token_mint,
        token_amount_raw=int(token_amount),
        token_decimals=decimals.get(token_mint) if token_mint else None,
        quote_mint=quote_mint,
        quote_amount_raw=abs(int(quote_delta)),
        quote_decimals=quote_decimals,
        fee_lamports=fee,
        fee_paid_by_wallet=paid_by_wallet,
    )
