"""Client JSON-RPC Solana (Helius par défaut, ou tout autre fournisseur RPC compatible).

Méthodes standard uniquement : elles fonctionnent avec n'importe quel RPC Solana.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from data.models import MintInfo
from data.providers.base import BudgetExceeded, HttpProvider, ProviderError

log = logging.getLogger(__name__)

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EJFLwy8RcBvGQX3rP2Pv"

# Codes d'erreur RPC temporaires (nœud en retard, slot ignoré, surcharge…)
_RETRYABLE_RPC_CODES = {-32004, -32005, -32007, -32009, -32014, -32016, 429}


class SolanaRpcClient(HttpProvider):
    name = "helius"

    def __init__(self, rpc_url: str, *, max_transaction_version: int = 0,
                 credit_budget: int | None = None, credits_used: Any = None, **kwargs: Any):
        url = httpx.URL(rpc_url)
        base = str(url.copy_with(query=None))
        super().__init__(base, params=dict(url.params), **kwargs)
        self.max_transaction_version = max_transaction_version
        self._credit_budget = credit_budget
        self._credits_used = credits_used  # fonction () -> crédits consommés ce mois-ci
        self._next_id = 0

    def _check_budget(self, critical: bool) -> None:
        if critical or self._credit_budget is None or self._credits_used is None:
            return
        if self._credits_used() >= self._credit_budget:
            raise BudgetExceeded(self.name, "budget mensuel de crédits atteint : requêtes non essentielles "
                                            "suspendues (les sorties de positions restent actives)")

    async def call(self, method: str, params: list[Any] | None = None, *, credits: int = 1,
                   critical: bool = False) -> Any:
        self._check_budget(critical)
        last: ProviderError | None = None
        for attempt in range(3):
            self._next_id += 1
            payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or []}
            data = await self._request("POST", "", json=payload, credits=credits)
            if not isinstance(data, dict):
                raise ProviderError(self.name, f"réponse inattendue pour {method}")
            err = data.get("error")
            if not err:
                return data.get("result")
            code = err.get("code") if isinstance(err, dict) else None
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            last = ProviderError(self.name, f"{method} : {message} (code {code})",
                                 retryable=code in _RETRYABLE_RPC_CODES)
            if not last.retryable:
                raise last
            await self._sleep(0.5 * 2 ** attempt)
        assert last is not None
        raise last

    # ----------------------------------------------------------- méthodes
    async def get_health(self) -> str:
        return str(await self.call("getHealth", critical=True))

    async def get_slot(self, commitment: str = "confirmed") -> int:
        return int(await self.call("getSlot", [{"commitment": commitment}], critical=True))

    async def get_balance(self, address: str, commitment: str = "confirmed") -> int:
        result = await self.call("getBalance", [address, {"commitment": commitment}], critical=True)
        return int(result["value"])

    async def get_signatures_for_address(self, address: str, *, limit: int = 100, before: str | None = None,
                                         until: str | None = None, commitment: str = "confirmed",
                                         critical: bool = False) -> list[dict[str, Any]]:
        options: dict[str, Any] = {"limit": max(1, min(limit, 1000)), "commitment": commitment}
        if before:
            options["before"] = before
        if until:
            options["until"] = until
        return list(await self.call("getSignaturesForAddress", [address, options], critical=critical) or [])

    async def get_transaction(self, signature: str, *, commitment: str = "confirmed",
                              critical: bool = False) -> dict[str, Any] | None:
        """Transaction décodée (jsonParsed). None si elle n'est pas (encore) connue du nœud."""
        for _ in range(2):
            options = {"encoding": "jsonParsed", "commitment": commitment,
                       "maxSupportedTransactionVersion": self.max_transaction_version}
            try:
                return await self.call("getTransaction", [signature, options], critical=critical)
            except ProviderError as exc:
                text = str(exc).lower()
                if "transaction version" in text and "not supported" in text and self.max_transaction_version < 1:
                    log.info("Transaction de version supérieure détectée : passage à la version 1.")
                    self.max_transaction_version = 1
                    continue
                raise
        return None

    async def get_signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        result = await self.call("getSignatureStatuses",
                                 [signatures, {"searchTransactionHistory": True}], critical=True)
        return list(result["value"])

    async def get_mint_info(self, mint: str) -> MintInfo | None:
        result = await self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                                 critical=True)
        value = (result or {}).get("value")
        if value is None:
            return None
        owner = value.get("owner")
        data = value.get("data")
        if not isinstance(data, dict) or data.get("parsed", {}).get("type") != "mint":
            raise ProviderError(self.name, f"le compte {mint} n'est pas un token reconnu")
        info = data["parsed"]["info"]
        extensions: list[str] = []
        transfer_fee_bps: int | None = None
        for ext in info.get("extensions", []) or []:
            name = ext.get("extension", "?")
            extensions.append(name)
            if name == "transferFeeConfig":
                state = ext.get("state", {})
                fee = state.get("newerTransferFee") or state.get("olderTransferFee") or {}
                transfer_fee_bps = int(fee.get("transferFeeBasisPoints", 0))
        return MintInfo(
            mint=mint,
            token_program=owner,
            is_token_2022=owner == TOKEN_2022_PROGRAM,
            decimals=int(info["decimals"]),
            supply_raw=int(info["supply"]),
            mint_authority=info.get("mintAuthority"),
            freeze_authority=info.get("freezeAuthority"),
            extensions=tuple(extensions),
            transfer_fee_bps=transfer_fee_bps,
        )

    async def get_token_largest_accounts(self, mint: str) -> list[tuple[str, int]]:
        result = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return [(item["address"], int(item["amount"])) for item in (result or {}).get("value", [])]

    async def get_token_balance(self, owner: str, mint: str) -> int:
        """Quantité brute d'un token détenue par ``owner`` (somme de ses comptes pour ce token)."""
        result = await self.call("getTokenAccountsByOwner",
                                 [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
                                 critical=True)
        total = 0
        for item in (result or {}).get("value", []):
            info = (((item.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            total += int((info.get("tokenAmount") or {}).get("amount") or 0)
        return total
