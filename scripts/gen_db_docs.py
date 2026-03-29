"""DBドキュメント生成スクリプト
- doc/db_tables.tsv  : Google Spreadsheet貼り付け用TSV
- doc/er_diagram.png : ER図画像
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# テーブル定義データ
# ---------------------------------------------------------------------------

TABLES = {
    "orders": {
        "label": "orders\n（発注レコード）",
        "columns": [
            ("id",              "INTEGER",    "PK",   "",     "主キー"),
            ("broker_order_id", "VARCHAR(50)","",     "NULL", "moomoo側の注文ID"),
            ("ticker",          "VARCHAR(10)","INDEX","",     "銘柄コード"),
            ("side",            "VARCHAR(4)", "",     "",     "BUY / SELL"),
            ("order_type",      "VARCHAR(10)","",     "",     "MARKET / LIMIT"),
            ("quantity",        "INTEGER",    "",     "",     "注文数量"),
            ("price",           "FLOAT",      "",     "NULL", "指値価格"),
            ("status",          "VARCHAR(15)","",     "",     "PENDING/SUBMITTED/FILLED/FAILED/DRY_RUN"),
            ("filled_price",    "FLOAT",      "",     "NULL", "約定価格"),
            ("filled_at",       "DATETIME",   "",     "NULL", "約定日時"),
            ("strategy_name",   "VARCHAR(50)","",     "",     "生成元戦略名"),
            ("created_at",      "DATETIME",   "",     "",     "作成日時"),
        ],
    },
    "trade_log": {
        "label": "trade_log\n（トレード履歴）",
        "columns": [
            ("id",              "INTEGER",    "PK",   "",     "主キー"),
            ("ticker",          "VARCHAR(10)","INDEX","",     "銘柄コード"),
            ("entry_order_id",  "INTEGER",    "FK",   "",     "→ orders.id（買い注文）"),
            ("exit_order_id",   "INTEGER",    "FK",   "NULL", "→ orders.id（売り注文）"),
            ("entry_date",      "DATE",       "",     "",     "エントリー日"),
            ("exit_date",       "DATE",       "",     "NULL", "エグジット日"),
            ("entry_price",     "FLOAT",      "",     "",     "エントリー価格"),
            ("exit_price",      "FLOAT",      "",     "NULL", "エグジット価格"),
            ("quantity",        "INTEGER",    "",     "",     "保有数量"),
            ("pnl",             "FLOAT",      "",     "NULL", "損益（ドル）"),
            ("pnl_pct",         "FLOAT",      "",     "NULL", "損益率（%）"),
            ("strategy_name",   "VARCHAR(50)","",     "",     "戦略名"),
            ("stop_loss",       "FLOAT",      "",     "NULL", "ストップロス価格"),
            ("take_profit",     "FLOAT",      "",     "NULL", "利確ターゲット価格"),
            ("take_profit_1",   "FLOAT",      "",     "NULL", "段階利確 第1ターゲット"),
            ("max_hold_days",   "INTEGER",    "",     "NULL", "最大保有日数（デフォルト20）"),
            ("notes",           "TEXT",       "",     "NULL", "メモ（エグジット理由等）"),
            ("status",          "VARCHAR(10)","",     "",     "OPEN / CLOSED"),
        ],
    },
    "portfolio_snapshots": {
        "label": "portfolio_snapshots\n（日次資産スナップショット）",
        "columns": [
            ("id",            "INTEGER", "PK",     "",  "主キー"),
            ("date",          "DATE",    "UNIQUE", "",  "記録日"),
            ("total_equity",  "FLOAT",   "",       "",  "総資産（ドル）"),
            ("cash",          "FLOAT",   "",       "",  "現金（ドル）"),
            ("positions_json","JSONB",   "",       "",  "保有ポジション一覧"),
            ("num_positions", "INTEGER", "",       "",  "保有件数"),
        ],
    },
    "market_conditions": {
        "label": "market_conditions\n（日次市場環境）",
        "columns": [
            ("id",             "INTEGER",    "PK",     "",     "主キー"),
            ("date",           "DATE",       "UNIQUE", "",     "記録日"),
            ("sp500_trend",    "VARCHAR(10)","",       "",     "bull / bear / neutral"),
            ("vix_level",      "FLOAT",      "",       "",     "VIX値"),
            ("market_breadth", "FLOAT",      "",       "",     "市場の広がり指標"),
            ("regime",         "VARCHAR(20)","",       "",     "trending / range / volatile"),
            ("notes",          "TEXT",       "",       "NULL", "メモ"),
        ],
    },
    "screening_results": {
        "label": "screening_results\n（銘柄スクリーニング結果）",
        "columns": [
            ("id",            "INTEGER",    "PK",    "", "主キー"),
            ("run_date",      "DATE",       "INDEX", "", "実行日"),
            ("ticker",        "VARCHAR(10)","",      "", "銘柄コード"),
            ("score",         "FLOAT",      "",      "", "スコア"),
            ("criteria_json", "JSONB",      "",      "", "各指標の詳細（last_close, atr_pct等）"),
            ("selected",      "BOOLEAN",    "",      "", "上位15銘柄に選ばれたか"),
        ],
    },
    "strategy_metadata": {
        "label": "strategy_metadata\n（戦略メタデータ）",
        "columns": [
            ("id",            "INTEGER",    "PK",     "",  "主キー"),
            ("name",          "VARCHAR(50)","UNIQUE", "",  "戦略名"),
            ("description",   "TEXT",       "",       "",  "説明"),
            ("version",       "VARCHAR(10)","",       "",  "バージョン"),
            ("market_regime", "VARCHAR(20)","",       "",  "対象レジーム"),
            ("is_active",     "BOOLEAN",    "",       "",  "有効/無効"),
            ("created_at",    "DATETIME",   "",       "",  "登録日時"),
        ],
    },
    "backtest_results": {
        "label": "backtest_results\n（バックテスト結果）",
        "columns": [
            ("id",           "INTEGER", "PK", "",  "主キー"),
            ("strategy_id",  "INTEGER", "FK", "",  "→ strategy_metadata.id"),
            ("ticker",       "VARCHAR(10)", "", "", "銘柄コード"),
            ("start_date",   "DATE",    "",   "",  "開始日"),
            ("end_date",     "DATE",    "",   "",  "終了日"),
            ("total_return", "FLOAT",   "",   "",  "総リターン"),
            ("sharpe_ratio", "FLOAT",   "",   "",  "シャープレシオ"),
            ("max_drawdown", "FLOAT",   "",   "",  "最大ドローダウン"),
            ("win_rate",     "FLOAT",   "",   "",  "勝率"),
            ("num_trades",   "INTEGER", "",   "",  "取引回数"),
            ("params_json",  "JSONB",   "",   "",  "使用パラメータ"),
            ("run_at",       "DATETIME","",   "",  "実行日時"),
        ],
    },
}

# FK関係: (from_table, to_table, label)
FK_RELATIONS = [
    ("trade_log",       "orders",            "entry_order_id"),
    ("trade_log",       "orders",            "exit_order_id"),
    ("backtest_results","strategy_metadata", "strategy_id"),
]

# ---------------------------------------------------------------------------
# TSV生成（Google Spreadsheet用）
# ---------------------------------------------------------------------------

def gen_tsv(out_path: Path) -> None:
    lines = []
    header = ["テーブル名", "カラム名", "型", "KEY", "NULL許可", "説明"]
    lines.append("\t".join(header))
    lines.append("")  # 空行

    for tname, tdef in TABLES.items():
        for col in tdef["columns"]:
            col_name, col_type, key, nullable, desc = col
            lines.append("\t".join([tname, col_name, col_type, key, nullable, desc]))
        lines.append("")  # テーブル間の空行

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"TSV出力: {out_path}")


# ---------------------------------------------------------------------------
# ER図生成（Pillow）
# ---------------------------------------------------------------------------

# テーブルの配置（x, y）※ピクセル単位
TABLE_POSITIONS = {
    "orders":              (80,  160),
    "trade_log":           (80,  680),
    "portfolio_snapshots": (780, 160),
    "market_conditions":   (780, 680),
    "screening_results":   (1480, 160),
    "strategy_metadata":   (1480, 560),
    "backtest_results":    (1480, 1000),
}

# 色設定
COLOR_BG         = (250, 250, 252)
COLOR_TABLE_HDR  = (41,  98,  255)   # ヘッダー背景（青）
COLOR_TABLE_HDR2 = (70, 130, 255)    # サブヘッダー
COLOR_TABLE_BODY = (255, 255, 255)
COLOR_TABLE_ALT  = (245, 247, 255)   # 交互行
COLOR_BORDER     = (180, 190, 220)
COLOR_TEXT_HDR   = (255, 255, 255)
COLOR_TEXT_BODY  = (30,  30,  50)
COLOR_TEXT_KEY   = (200, 50,  50)    # PK/FK強調
COLOR_FK_LINE    = (80, 160, 80)
COLOR_SHADOW     = (210, 215, 230)

COL_WIDTHS = [160, 100, 50, 55]  # カラム名, 型, KEY, NULL
ROW_H      = 22
HDR_H      = 40
FONT_SIZE  = 13
FONT_SIZE_HDR = 15

IMG_W = 2200
IMG_H = 1500


def _load_font(size: int):
    """日本語フォントをロード。なければデフォルト。"""
    candidates = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode MS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _table_height(tname: str) -> int:
    return HDR_H + len(TABLES[tname]["columns"]) * ROW_H + 4


def _table_width() -> int:
    return sum(COL_WIDTHS) + 8


def _draw_table(draw: ImageDraw.Draw, x: int, y: int, tname: str,
                font, font_bold) -> dict:
    """テーブルボックスを描画し、各カラムの中心Y座標を返す。"""
    tw = _table_width()
    th = _table_height(tname)

    # 影
    draw.rectangle([x+4, y+4, x+tw+4, y+th+4], fill=COLOR_SHADOW)

    # ヘッダー
    draw.rectangle([x, y, x+tw, y+HDR_H], fill=COLOR_TABLE_HDR)
    label = TABLES[tname]["label"]
    lines = label.split("\n")
    ly = y + 4
    for line in lines:
        draw.text((x + tw//2, ly), line, fill=COLOR_TEXT_HDR, font=font_bold, anchor="mt")
        ly += FONT_SIZE_HDR + 2

    # カラムヘッダー行
    sub_y = y + HDR_H
    draw.rectangle([x, sub_y, x+tw, sub_y+ROW_H], fill=COLOR_TABLE_HDR2)
    headers = ["カラム名", "型", "KEY", "NULL"]
    cx = x + 4
    for i, h in enumerate(headers):
        draw.text((cx + 2, sub_y + 3), h, fill=COLOR_TEXT_HDR, font=font)
        cx += COL_WIDTHS[i]

    # データ行
    col_centers = {}  # カラム名→中心Y
    for ri, col in enumerate(TABLES[tname]["columns"]):
        col_name, col_type, key, nullable, _ = col
        ry = sub_y + ROW_H + ri * ROW_H
        bg = COLOR_TABLE_BODY if ri % 2 == 0 else COLOR_TABLE_ALT
        draw.rectangle([x, ry, x+tw, ry+ROW_H], fill=bg)

        cx = x + 4
        values = [col_name, col_type, key, nullable]
        for ci, val in enumerate(values):
            color = COLOR_TEXT_KEY if (ci == 2 and val in ("PK","FK")) else COLOR_TEXT_BODY
            draw.text((cx + 2, ry + 3), val, fill=color, font=font)
            cx += COL_WIDTHS[ci]

        col_centers[col_name] = ry + ROW_H // 2

    # 外枠
    draw.rectangle([x, y, x+tw, y+th], outline=COLOR_BORDER, width=1)
    # 行の区切り線
    for ri in range(len(TABLES[tname]["columns"]) + 1):
        ry = sub_y + ri * ROW_H
        draw.line([x, ry, x+tw, ry], fill=COLOR_BORDER, width=1)

    return col_centers


def _midpoint(p1, p2):
    return ((p1[0]+p2[0])//2, (p1[1]+p2[1])//2)


def _draw_arrow(draw: ImageDraw.Draw, x1, y1, x2, y2, color, font):
    """直角折れ線の矢印を描画する。"""
    mx = (x1 + x2) // 2
    pts = [(x1, y1), (mx, y1), (mx, y2), (x2, y2)]
    draw.line(pts, fill=color, width=2)
    # 矢印の先端
    dx = x2 - mx
    arrow_size = 7
    if dx >= 0:
        draw.polygon([(x2, y2), (x2-arrow_size, y2-4), (x2-arrow_size, y2+4)], fill=color)
    else:
        draw.polygon([(x2, y2), (x2+arrow_size, y2-4), (x2+arrow_size, y2+4)], fill=color)


def gen_er_image(out_path: Path) -> None:
    img = Image.new("RGB", (IMG_W, IMG_H), COLOR_BG)
    draw = ImageDraw.Draw(img)

    font      = _load_font(FONT_SIZE)
    font_bold = _load_font(FONT_SIZE_HDR)

    # タイトル
    draw.text((IMG_W//2, 24), "autoTRD — ER図", fill=(30, 30, 80),
              font=_load_font(28), anchor="mt")

    # テーブルを描画し、カラムY座標を記録
    col_y_map = {}  # tname -> {col_name: center_y}
    for tname, (tx, ty) in TABLE_POSITIONS.items():
        col_y_map[tname] = _draw_table(draw, tx, ty, tname, font, font_bold)

    # FK矢印を描画
    for from_t, to_t, fk_col in FK_RELATIONS:
        fx, fy = TABLE_POSITIONS[from_t]
        tx, ty = TABLE_POSITIONS[to_t]
        tw = _table_width()

        from_col_y = col_y_map[from_t].get(fk_col, fy + HDR_H + 10)

        # to側はidカラムのY
        to_col_y = col_y_map[to_t].get("id", ty + HDR_H + 10)

        # 始点: fromテーブルの右端 or 左端
        if fx > tx:
            x1 = fx
            x2 = tx + tw
        else:
            x1 = fx + tw
            x2 = tx

        _draw_arrow(draw, x1, from_col_y, x2, to_col_y, COLOR_FK_LINE, font)

        # ラベル
        lx = (x1 + x2) // 2
        ly = (from_col_y + to_col_y) // 2 - 8
        draw.text((lx, ly), fk_col, fill=COLOR_FK_LINE, font=font, anchor="mt")

    # 凡例
    legend_x, legend_y = 30, IMG_H - 60
    draw.rectangle([legend_x, legend_y, legend_x+14, legend_y+14],
                   fill=COLOR_TABLE_HDR)
    draw.text((legend_x+20, legend_y), "テーブル", fill=(30,30,80), font=font)
    draw.line([legend_x+120, legend_y+7, legend_x+160, legend_y+7],
              fill=COLOR_FK_LINE, width=2)
    draw.text((legend_x+170, legend_y), "FK（外部キー）", fill=(30,30,80), font=font)
    draw.text((legend_x+320, legend_y), "赤字=PK/FK", fill=COLOR_TEXT_KEY, font=font)

    img.save(str(out_path), dpi=(150, 150))
    print(f"ER図出力: {out_path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    doc_dir = Path(__file__).parent.parent / "doc"
    doc_dir.mkdir(exist_ok=True)

    gen_tsv(doc_dir / "db_tables.tsv")
    gen_er_image(doc_dir / "er_diagram.png")
    print("完了")
