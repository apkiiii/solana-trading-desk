from __future__ import annotations

from data.swap_parser import parse_swap
from tests.conftest import OTHER_WALLET, SOL, TOKEN, TOKEN_B, USDC, WALLET

POOL = "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2"
FEE = 5_000
RENT = 2_039_280


def tb(index, mint, owner, amount, decimals):
    return {"accountIndex": index, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(amount), "decimals": decimals}}


def make_tx(keys, pre_bal, post_bal, pre_tok=(), post_tok=(), *, fee=FEE, err=None, sig="sigA"):
    return {
        "slot": 123, "blockTime": 1_700_000_000,
        "meta": {"err": err, "fee": fee, "preBalances": list(pre_bal), "postBalances": list(post_bal),
                 "preTokenBalances": list(pre_tok), "postTokenBalances": list(post_tok)},
        "transaction": {"signatures": [sig], "message": {"accountKeys": [
            {"pubkey": k, "signer": i == 0, "writable": True, "source": "transaction"} for i, k in enumerate(keys)]}},
    }


def test_buy_with_sol_paid_by_wallet():
    spent = 1_000_000_000
    tx = make_tx([WALLET, "ata1", POOL], [10_000_000_000, 0, 50], [10_000_000_000 - spent - FEE - RENT, RENT, 50],
                 post_tok=[tb(1, TOKEN, WALLET, 5_000_000, 6)])
    event = parse_swap(tx, WALLET)
    assert event is not None and event.side == "BUY"
    assert event.token_mint == TOKEN and event.token_amount_raw == 5_000_000 and event.token_decimals == 6
    assert event.quote_mint == SOL
    assert event.quote_amount_raw == spent + RENT  # le dépôt du compte token est inclus (limite documentée)
    assert event.fee_paid_by_wallet is True
    assert event.signature == "sigA" and event.block_time == 1_700_000_000


def test_sell_to_sol_with_account_closed():
    received = 1_200_000_000
    tx = make_tx([WALLET, "ata1", POOL], [1_000_000_000, RENT, 50],
                 [1_000_000_000 + received - FEE + RENT, 0, 50],
                 pre_tok=[tb(1, TOKEN, WALLET, 5_000_000, 6)])
    event = parse_swap(tx, WALLET)
    assert event is not None and event.side == "SELL"
    assert event.token_amount_raw == 5_000_000
    assert event.quote_amount_raw == received + RENT


def test_buy_with_usdc():
    tx = make_tx([WALLET, "usdc_ata", "ata1"], [500_000_000, 0, 0], [500_000_000 - FEE, 0, 0],
                 pre_tok=[tb(1, USDC, WALLET, 100_000_000, 6), tb(2, TOKEN, WALLET, 0, 6)],
                 post_tok=[tb(1, USDC, WALLET, 50_000_000, 6), tb(2, TOKEN, WALLET, 1_000, 6)])
    event = parse_swap(tx, WALLET)
    assert event is not None and event.side == "BUY"
    assert event.quote_mint == USDC and event.quote_amount_raw == 50_000_000 and event.quote_decimals == 6


def test_gasless_trade_signed_by_an_app_relayer_is_detected():
    tx = make_tx([OTHER_WALLET, WALLET, "ata1"], [5_000_000_000, 800_000_000, 0],
                 [5_000_000_000 - FEE - RENT, 300_000_000, RENT],
                 post_tok=[tb(2, TOKEN, WALLET, 42, 6)])
    event = parse_swap(tx, WALLET)
    assert event is not None and event.side == "BUY"
    assert event.fee_paid_by_wallet is False
    assert event.quote_amount_raw == 500_000_000


def test_buy_paid_with_wrapped_sol():
    tx = make_tx([WALLET, "wsol_ata", "ata1"], [100_000_000, RENT, 0], [100_000_000 - FEE + RENT - RENT, 0, RENT],
                 pre_tok=[tb(1, SOL, WALLET, 1_000_000_000, 9)],
                 post_tok=[tb(2, TOKEN, WALLET, 777, 6)])
    event = parse_swap(tx, WALLET)
    assert event is not None and event.side == "BUY"
    assert event.quote_mint == SOL and event.quote_amount_raw == 1_000_000_000


def test_token_to_token_is_never_a_copyable_buy():
    tx = make_tx([WALLET, "a", "b"], [100_000_000, 0, 0], [100_000_000 - FEE, 0, 0],
                 pre_tok=[tb(1, TOKEN, WALLET, 100, 6), tb(2, TOKEN_B, WALLET, 0, 5)],
                 post_tok=[tb(1, TOKEN, WALLET, 0, 6), tb(2, TOKEN_B, WALLET, 200, 5)])
    event = parse_swap(tx, WALLET)
    assert event is not None and event.side == "OTHER"


def test_failed_transaction_is_ignored():
    tx = make_tx([WALLET, "ata1"], [10_000_000_000, 0], [10_000_000_000 - FEE, 0],
                 err={"InstructionError": [0, "Custom"]})
    assert parse_swap(tx, WALLET) is None


def test_wallet_not_involved_is_ignored():
    tx = make_tx([OTHER_WALLET, "ata1"], [10_000_000_000, 0], [9_000_000_000, 0],
                 post_tok=[tb(1, TOKEN, OTHER_WALLET, 5, 6)])
    assert parse_swap(tx, WALLET) is None


def test_plain_sol_transfer_is_not_a_swap():
    tx = make_tx([WALLET, OTHER_WALLET], [10_000_000_000, 0], [9_000_000_000 - FEE, 1_000_000_000])
    assert parse_swap(tx, WALLET) is None


def test_missing_transaction_is_ignored():
    assert parse_swap(None, WALLET) is None
