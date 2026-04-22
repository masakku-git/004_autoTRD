"""moomoo OpenAPIのget_acc_list()で口座一覧を取得して表示するスクリプト。

moomoo証券サポートに送付するための確認スクリプト。
現在のOpenDセッションに紐づく全口座（一般/特定/NISA等）の acc_id・区分を一覧表示する。

使い方:
    python scripts/check_acc_list.py

前提:
    - OpenDが起動していて、moomooにログイン済みであること
    - .env に moomoo_host / moomoo_port が正しく設定されていること
    - 本スクリプトは読み取り専用（発注・解約等は一切行わない）
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings


def _query_acc_list(trd_env_label: str) -> None:
    """指定したtrd_env（SIMULATE or REAL）で get_acc_list() を実行して結果を表示する。"""
    from moomoo import (
        RET_OK,
        OpenSecTradeContext,
        SecurityFirm,
        TrdEnv,
        TrdMarket,
    )

    trd_env = TrdEnv.SIMULATE if trd_env_label == "SIMULATE" else TrdEnv.REAL

    print(f"\n{'=' * 70}")
    print(f"  TrdEnv: {trd_env_label}")
    print(f"  Market: US / SecurityFirm: FUTUJP")
    print(f"  Host:   {settings.moomoo_host}:{settings.moomoo_port}")
    print("=" * 70)

    ctx = OpenSecTradeContext(
        host=settings.moomoo_host,
        port=settings.moomoo_port,
        filter_trdmarket=TrdMarket.US,
        security_firm=SecurityFirm.FUTUJP,
    )
    try:
        ret, data = ctx.get_acc_list()
        if ret == RET_OK:
            print("\n[全カラム一覧]")
            print(data.to_string(index=False))

            # trd_env で該当環境の口座にフィルタ
            try:
                filtered = data[data["trd_env"].astype(str).str.upper() == trd_env_label]
            except Exception:
                filtered = data

            print(f"\n[{trd_env_label}環境の口座のみ抽出]")
            if filtered.empty:
                print(f"  該当口座なし（{trd_env_label}環境）")
            else:
                print(filtered.to_string(index=False))

                print("\n[acc_id 一覧]")
                acc_ids = filtered["acc_id"].values.tolist()
                for i, acc_id in enumerate(acc_ids):
                    row = filtered.iloc[i]
                    details = ", ".join(
                        f"{col}={row[col]}" for col in filtered.columns if col != "acc_id"
                    )
                    print(f"  acc_id={acc_id}  ({details})")
        else:
            print(f"\n[ERROR] get_acc_list failed: {data}")
    finally:
        ctx.close()


def main() -> None:
    print("moomoo OpenAPI get_acc_list() 確認スクリプト")
    print(f"実行日時: {datetime.now().isoformat()}")
    print(f"設定されている trd_env (.env): {settings.moomoo_trade_env}")

    try:
        _query_acc_list("REAL")
    except Exception as e:
        print(f"\n[REAL環境クエリ失敗] {e}")

    try:
        _query_acc_list("SIMULATE")
    except Exception as e:
        print(f"\n[SIMULATE環境クエリ失敗] {e}")

    print("\n完了")


if __name__ == "__main__":
    main()
