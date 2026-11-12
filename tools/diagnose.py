"""Diagnostic des connexions, à lancer sur ton PC (aucune transaction n'est envoyée).

Utilisation (dans le dossier du projet) :
    python -m tools.diagnose
    python -m tools.diagnose --wallet <ADRESSE_PUBLIQUE_D_UN_TRADER>

Vérifie : clés API, RPC, WebSocket, prix SOL/EURC, devis Jupiter, DexScreener, RugCheck,
et (avec --wallet) que les swaps de ce trader sont bien détectés.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from websockets.asyncio.client import connect as ws_connect

from config.settings import load_settings
from core.logs import setup_logging
from core.paths import DB_PATH, ensure_dirs
from core.validation import is_valid_address
from data.database import Database
from data.market_data import MarketData
from data.providers.factory import build_providers
from data.swap_parser import parse_swap
from security.secrets import SecretsError, load_secrets, mask
from tools.common import fail, ok, setup_console, title, warn

JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"  # token de test très liquide


async def run(wallet: str | None) -> int:
    failures = 0
    ensure_dirs()
    db = Database(DB_PATH)
    settings = load_settings(db)

    title("1. Clés API (fichier .env)")
    try:
        secrets = load_secrets()
    except SecretsError as exc:
        fail(str(exc))
        return 1
    (ok if secrets.helius_api_key or secrets.rpc_url_override else fail)(
        f"Helius : {mask(secrets.helius_api_key)}")
    (ok if secrets.jupiter_api_key else warn)(
        f"Jupiter : {mask(secrets.jupiter_api_key)}" + ("" if secrets.jupiter_api_key else " (sans clé : 0,5 requête/s)"))
    print(f"  [INFO]   RugCheck : {mask(secrets.rugcheck_api_key)} (optionnelle)")
    try:
        providers = build_providers(settings, secrets, db)
    except SecretsError as exc:
        fail(str(exc))
        return 1

    tokens = settings["tokens"]
    try:
        title("2. RPC Solana")
        try:
            start = time.perf_counter()
            slot = await providers.rpc.get_slot()
            ok(f"Slot actuel {slot} (réponse en {1000 * (time.perf_counter() - start):.0f} ms)")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            fail(f"RPC injoignable : {exc}")

        title("3. WebSocket (temps réel)")
        try:
            async with ws_connect(providers.ws_url, open_timeout=10) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}))
                start = time.perf_counter()
                received = 0
                while received < 2:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    received += 1
                ok(f"Flux temps réel reçu ({1000 * (time.perf_counter() - start):.0f} ms, dernier message : "
                   f"{message.get('method', 'confirmation')})")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            fail(f"WebSocket indisponible : {type(exc).__name__}")

        market = MarketData(providers.rpc, providers.jupiter, providers.dex, providers.rug, tokens)
        title("4. Prix (Jupiter) et conversion en euros")
        rate, reason = await market.eur_per_sol()
        if rate:
            ok(f"1 SOL = {rate:.2f} €")
        else:
            failures += 1
            fail(f"Conversion SOL/EUR impossible : {reason}")

        title("5. Devis de swap (Jupiter, aucun achat)")
        try:
            quote = await providers.jupiter.get_order(tokens["sol_mint"], tokens["usdc_mint"], 10_000_000)
            if quote.ok:
                ok(f"0,01 SOL -> {quote.out_amount_raw / 1e6:.4f} USDC | frais {quote.fee_bps} bps | "
                   f"impact {quote.price_impact_pct}% | routeur {quote.router}")
            else:
                failures += 1
                fail(f"Devis refusé : {quote.error_message}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            fail(f"Devis impossible : {exc}")

        title("6. Données de marché d'un token test (JUP)")
        snap = await market.snapshot(JUP_MINT, use_cache=False)
        if snap.best_pair:
            ok(f"DexScreener : liquidité {snap.best_pair.liquidity_usd:,.0f} $ sur {snap.best_pair.dex_id}")
        if snap.mint_info:
            ok(f"Compte du token : autorité de création {'révoquée' if snap.mint_info.mint_authority is None else 'active'}"
               f", gel {'révoqué' if snap.mint_info.freeze_authority is None else 'actif'}")
        if snap.rug:
            ok(f"RugCheck : score {snap.rug.score_normalised}, {len(snap.rug.risks)} risque(s) listé(s)")
        for source, error in snap.errors.items():
            failures += 1
            fail(f"{source} : {error}")

        if wallet:
            title(f"7. Détection des swaps du wallet {wallet[:6]}...{wallet[-4:]}")
            if not is_valid_address(wallet):
                failures += 1
                fail("Adresse invalide.")
            else:
                signatures = await providers.rpc.get_signatures_for_address(wallet, limit=25)
                counts = {"BUY": 0, "SELL": 0, "OTHER": 0}
                examples: list[str] = []
                for item in signatures:
                    if item.get("err") is not None:
                        continue
                    tx = await providers.rpc.get_transaction(item["signature"])
                    event = parse_swap(tx, wallet, usdc_mint=tokens["usdc_mint"])
                    if event is None:
                        continue
                    counts[event.side] += 1
                    if event.side != "OTHER" and len(examples) < 5:
                        when = time.strftime("%d/%m %H:%M", time.localtime(event.block_time or 0))
                        unit = "SOL" if event.quote_mint == tokens["sol_mint"] else "USDC"
                        payer = "oui" if event.fee_paid_by_wallet else "non"
                        examples.append(f"{when}  {event.side:4}  {event.token_mint}  "
                                        f"{event.quote_amount or 0:.4f} {unit}  (frais payés par le wallet : {payer})")
                ok(f"{len(signatures)} transactions récentes analysées : {counts['BUY']} achats, "
                   f"{counts['SELL']} ventes, {counts['OTHER']} autres")
                for line in examples:
                    print(f"           {line}")
                if counts["BUY"] + counts["SELL"] == 0:
                    warn("Aucun achat/vente détecté : ce wallet ne trade peut-être pas en SOL/USDC, "
                         "ou ses trades passent par une autre adresse.")
    finally:
        await providers.aclose()
        db.close()

    title("Résultat")
    if failures:
        fail(f"{failures} problème(s) détecté(s). Envoie ce résultat (il ne contient aucune clé).")
        return 1
    ok("Toutes les connexions fonctionnent.")
    return 0


def main() -> int:
    setup_console()
    parser = argparse.ArgumentParser(description="Diagnostic des connexions du trading desk")
    parser.add_argument("--wallet", help="adresse publique d'un trader à tester")
    args = parser.parse_args()
    ensure_dirs()
    setup_logging("diagnose", console=False)
    return asyncio.run(run(args.wallet))


if __name__ == "__main__":
    raise SystemExit(main())
