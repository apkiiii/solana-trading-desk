"""Configuration commune des tests (aucun accès réseau : tout est simulé)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.database import Database  # noqa: E402

# Adresses Solana valides utilisées dans les tests
WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
OTHER_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
TOKEN = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
TOKEN_B = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()
