"""手動で1銘柄を発注する検証スクリプト（特定口座ルーティング確認用）。

jp_acc_type指定が正しく効いているかをSIMULATE環境またはREAL環境で確認するための単発発注ツール。
本番フロー（src/broker/executor.py）と同じAPI呼び出しを再現する。

使い方:
    # SIMULATE（模擬取引）で1株買付 — 推奨
    python scripts/manual_order.py AAPL BUY 1 --env SIMULATE

    # REAL（本番口座）で発注 — 確認プロンプト必須
    python scripts/manual_order.py AAPL BUY 1 --env REAL

引数:
    ticker   : 銘柄コード（例: AAPL, MSFT）※ US. プレフィックスは自動付与
    side     : BUY または SELL
    quantity : 株数（整数）
    --env    : SIMULATE / REAL（省略時は .env の MOOMOO_TRADE_ENV を使用）

注意:
    - 本スクリプトはsrc/broker/executor.pyと同じ口座設定（acc_id / jp_acc_type）を使う
    - 成行注文（MARKET）として発注する
    - REAL指定時は取引パスワード（MD5）が .env に設定されている必要がある
    - trade_log / orders テーブルには記録されない（DB汚染を避けるため直接API呼び出し）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [yes/NO]: ").strip().lower()
    except EOFError:
        return False
    return ans == "yes"


def main() -> None:
    parser = argparse.ArgumentParser(description="手動発注検証スクリプト")
    parser.add_argument("ticker", help="銘柄コード（例: AAPL）")
    parser.add_argument("side", choices=["BUY", "SELL"], help="売買区分")
    parser.add_argument("quantity", type=int, help="株数")
    parser.add_argument(
        "--env",
        choices=["SIMULATE", "REAL"],
        default=None,
        help="取引環境（省略時は .env 値）",
    )
    args = parser.parse_args()

    trd_env_label = args.env or settings.moomoo_trade_env

    from moomoo import (
        RET_OK,
        OpenSecTradeContext,
        OrderType,
        SecurityFirm,
        SubAccType,
        TrdEnv,
        TrdMarket,
        TrdSide,
    )

    trd_env = TrdEnv.SIMULATE if trd_env_label == "SIMULATE" else TrdEnv.REAL
    side = TrdSide.BUY if args.side == "BUY" else TrdSide.SELL
    moomoo_ticker = f"US.{args.ticker}" if not args.ticker.startswith("US.") else args.ticker

    try:
        sub_acc_type = getattr(SubAccType, settings.moomoo_jp_acc_type)
    except AttributeError:
        print(f"[ERROR] 不正な MOOMOO_JP_ACC_TYPE: {settings.moomoo_jp_acc_type}")
        sys.exit(1)

    # 発注内容の事前確認
    print("=" * 60)
    print("  手動発注 — 内容確認")
    print("=" * 60)
    print(f"  実行日時        : {datetime.now().isoformat()}")
    print(f"  環境            : {trd_env_label}")
    print(f"  銘柄            : {moomoo_ticker}")
    print(f"  売買            : {args.side}")
    print(f"  株数            : {args.quantity}")
    print(f"  注文タイプ      : MARKET（成行）")
    print(f"  acc_id          : {settings.moomoo_acc_id}")
    print(f"  jp_acc_type     : {settings.moomoo_jp_acc_type}")
    print(f"  host:port       : {settings.moomoo_host}:{settings.moomoo_port}")
    print("=" * 60)

    if trd_env == TrdEnv.REAL:
        print("\n⚠️  REAL環境（本番口座）です。実際にお金が動きます。")
        if not _confirm("本当に発注しますか？（'yes' と入力）"):
            print("キャンセルしました。")
            sys.exit(0)
    else:
        if not _confirm("SIMULATE環境で発注を実行しますか？（'yes' と入力）"):
            print("キャンセルしました。")
            sys.exit(0)

    ctx = OpenSecTradeContext(
        host=settings.moomoo_host,
        port=settings.moomoo_port,
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.FUTUJP,
    )
    try:
        # unlock_trade（REAL時のみ）
        if trd_env == TrdEnv.REAL:
            if not settings.moomoo_trade_password_md5:
                print("[ERROR] MOOMOO_TRADE_PASSWORD_MD5 が未設定です。REAL発注には必須です。")
                sys.exit(1)
            ret, msg = ctx.unlock_trade(password_md5=settings.moomoo_trade_password_md5)
            if ret != RET_OK:
                print(f"[ERROR] unlock_trade失敗: {msg}")
                sys.exit(1)
            print("\n✓ unlock_trade 成功")

        # 発注
        ret, data = ctx.place_order(
            price=0,
            qty=args.quantity,
            code=moomoo_ticker,
            trd_side=side,
            order_type=OrderType.MARKET,
            trd_env=trd_env,
            acc_id=settings.moomoo_acc_id,
            jp_acc_type=sub_acc_type,
        )
        if ret != RET_OK:
            print(f"\n[ERROR] 発注失敗: {data}")
            sys.exit(1)

        order_id = str(data["order_id"].iloc[0])
        print(f"\n✓ 発注成功 — broker_order_id: {order_id}")
        print("\n[place_order 返り値]")
        print(data.to_string(index=False))

        # 約定確認（最大30秒）
        print("\n約定確認をポーリング中（最大30秒）...")
        for i in range(15):
            time.sleep(2)
            ret, olist = ctx.order_list_query(trd_env=trd_env, acc_id=settings.moomoo_acc_id)
            if ret == RET_OK and not olist.empty:
                rows = olist[olist["order_id"].astype(str) == order_id]
                if not rows.empty:
                    row = rows.iloc[0]
                    status = str(row["order_status"])
                    print(f"  [{i*2+2}s] order_status={status}")
                    if status == "FILLED_ALL":
                        print(f"\n✓ 約定完了 — 平均単価: ${float(row['dealt_avg_price']):.4f}")
                        print("\n[order_list_query 該当行]")
                        print(rows.to_string(index=False))
                        break
        else:
            print("\n⚠️  30秒以内に FILLED_ALL を確認できませんでした（成行でも時間外等の要因あり）")

    finally:
        ctx.close()

    print("\n完了。moomooアプリの取引明細で口座区分（一般/特定）を確認してください。")


if __name__ == "__main__":
    main()
