"""口座情報の取得（moomoo APIから残高・ポジションを取得、DRY_RUNではシミュレーション値を返す）"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass

from config.settings import settings
from src.utils.logger import logger

MOOMOO_TIMEOUT = 30  # seconds


@dataclass
class AccountInfo:
    """口座情報（総資産・現金・時価評価額・保有ポジション一覧）"""
    total_equity: float
    cash: float
    market_value: float
    positions: list[dict]  # [{ticker, qty, avg_price, market_value, pnl}]


def get_account_info() -> AccountInfo:
    """moomooから口座残高とポジションを取得（DRY_RUN時はシミュレーション値）"""
    if settings.dry_run:
        logger.info("DRY_RUN: returning simulated account info")
        return AccountInfo(
            total_equity=3300.0,
            cash=3300.0,
            market_value=0.0,
            positions=[],
        )

    try:
        from moomoo import OpenSecTradeContext, SecurityFirm, TrdEnv, TrdMarket

        trd_env = TrdEnv.SIMULATE if settings.moomoo_trade_env == "SIMULATE" else TrdEnv.REAL

        def _fetch() -> AccountInfo:
            ctx = OpenSecTradeContext(
                host=settings.moomoo_host,
                port=settings.moomoo_port,
                filter_trdmarket=TrdMarket.US,
                security_firm=SecurityFirm.FUTUINC,
            )
            try:
                ret, funds = ctx.accinfo_query(trd_env=trd_env)
                if ret != 0:
                    raise RuntimeError(f"Account query failed: {funds}")

                total_equity = float(funds["total_assets"].iloc[0])
                cash = float(funds["cash"].iloc[0])
                market_value = float(funds["market_val"].iloc[0])

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

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_fetch)
            try:
                return future.result(timeout=MOOMOO_TIMEOUT)
            except FuturesTimeoutError:
                msg = f"OpenD接続タイムアウト（{MOOMOO_TIMEOUT}秒）— OpenDのセッションを確認してください"
                logger.error(msg)
                _notify_opend_error(msg)
                raise RuntimeError(msg)

    except ImportError:
        logger.warning("moomoo-api not installed, returning simulated account")
        return AccountInfo(total_equity=3300.0, cash=3300.0, market_value=0.0, positions=[])
    except RuntimeError:
        raise
    except Exception as e:
        msg = f"OpenD接続エラー: {e}"
        logger.error(msg)
        _notify_opend_error(msg)
        raise RuntimeError(msg) from e


def _notify_opend_error(message: str) -> None:
    """OpenD接続失敗時にSlack通知を送る（循環importを避けるため遅延import）"""
    try:
        from src.notify.notifier import send_notification
        send_notification("OpenD接続エラー", message, level="error")
    except Exception as notify_err:
        logger.error(f"Slack通知送信失敗: {notify_err}")
