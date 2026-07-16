"""
Central configuration. Loads from environment variables so secrets never
live in source. Copy `.env.example` to `.env` and fill in values, or export
these directly in your shell / container.
"""
import os
from dataclasses import dataclass
from pathlib import Path

# Auto-load .env if present (no-op if file doesn't exist)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=_env_path, override=False)
except ImportError:
    pass  # python-dotenv optional; set env vars manually if not installed


@dataclass(frozen=True)
class KalshiConfig:
    # Public market data needs no auth. These are only required if/when
    # you move from read-only backtesting to placing real or demo orders.
    #
    # NOTE: The demo base URL must include the /trade-api/v2 path prefix
    # because httpx's base_url joining requires it to keep the subpath.
    base_url: str = os.getenv("KALSHI_BASE_URL", "https://external-api.kalshi.com/trade-api/v2")
    ws_url: str = os.getenv("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2")
    demo_base_url: str = os.getenv(
        "KALSHI_DEMO_BASE_URL", "https://external-api.demo.kalshi.co/trade-api/v2"
    )
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    # Default False: the public (prod) API is read-only and needs no credentials.
    use_demo: bool = os.getenv("KALSHI_USE_DEMO", "false").lower() == "true"


@dataclass(frozen=True)
class DBConfig:
    host: str = os.getenv("PGHOST", "localhost")
    port: int = int(os.getenv("PGPORT", "5432"))
    dbname: str = os.getenv("PGDATABASE", "pred_market_maker")
    user: str = os.getenv("PGUSER", "postgres")
    password: str = os.getenv("PGPASSWORD", "postgres")

    @property
    def dsn(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} "
            f"user={self.user} password={self.password}"
        )


@dataclass(frozen=True)
class StrategyConfig:
    # Risk-aversion parameter (gamma) for inventory-aware quoting.
    # Higher = more conservative, widens spread faster as inventory grows.
    risk_aversion: float = float(os.getenv("RISK_AVERSION", "0.1"))
    # Max contracts held (net) in any single market.
    max_position_per_market: int = int(os.getenv("MAX_POSITION_PER_MARKET", "50"))
    # Max aggregate net exposure (in contracts) across all open markets.
    max_aggregate_exposure: int = int(os.getenv("MAX_AGGREGATE_EXPOSURE", "200"))
    # Minimum spread (in probability points, 0-1 scale) we will ever quote.
    min_spread: float = float(os.getenv("MIN_SPREAD", "0.02"))
    # Maximum spread we will ever quote (beyond this we just pull the quote).
    max_spread: float = float(os.getenv("MAX_SPREAD", "0.15"))


KALSHI = KalshiConfig()
DB = DBConfig()
STRATEGY = StrategyConfig()
