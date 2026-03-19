"""Account information retrieval from moomoo API."""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import settings
from src.utils.logger import logger


@dataclass
class AccountInfo:
    total_equity: float
    cash: float
    market_value: float
    positions: list[dict]  # [{ticker, qty, avg_price, market_value, pnl}]


def get_account_info() -> AccountInfo:
    """Fetch account balance and positions from moomoo."""
    if settings.dry_run:
        logger.info("DRY_RUN: returning simulated account info")
        return AccountInfo(
            total_equity=3300.0,
            cash=3300.0,
            market_value=0.0,
            positions=[],
        )

    try:
        from moomoo import OpenSecTradeContext, TrdEnv, TrdMarket

        trd_env = TrdEnv.SIMULATE if settings.moomoo_trade_env == "SIMULATE" else TrdEnv.REAL
        ctx = OpenSecTradeContext(
            host=settings.moomoo_host,
            port=settings.moomoo_port,
            filter_trdmarket=TrdMarket.US,
            security_firm=None,
        )

        try:
            # Get account funds
            ret, funds = ctx.accinfo_query(trd_env=trd_env)
            if ret != 0:
                logger.error(f"Failed to query account: {funds}")
                raise RuntimeError(f"Account query failed: {funds}")

            total_equity = float(funds["total_assets"].iloc[0])
            cash = float(funds["cash"].iloc[0])
            market_value = float(funds["market_val"].iloc[0])

            # Get positions
            ret, pos_df = ctx.position_list_query(trd_env=trd_env)
            positions = []
            if ret == 0 and not pos_df.empty:
                for _, row in pos_df.iterrows():
                    positions.append(
                        {
                            "ticker": row["code"],
                            "qty": int(row["qty"]),
                            "avg_price": float(row["cost_price"]),
                            "market_value": float(row["market_val"]),
                            "pnl": float(row["pl_val"]),
                        }
                    )

            return AccountInfo(
                total_equity=total_equity,
                cash=cash,
                market_value=market_value,
                positions=positions,
            )
        finally:
            ctx.close()

    except ImportError:
        logger.warning("moomoo-api not installed, returning simulated account")
        return AccountInfo(total_equity=3300.0, cash=3300.0, market_value=0.0, positions=[])
