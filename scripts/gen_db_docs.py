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
#   左列: orders / trade_log（FK関係あり）
#   中列: portfolio_snapshots / market_conditions（独立）
#   右列: screening_results / strategy_metadata / backtest_results（FK関係あり）
TABLE_POSITIONS = {
    "orders":              (80,   160),
    "trade_log":           (80,  1020),
    "portfolio_snapshots": (700,  160),
    "market_conditions":   (700,  900),
    "screening_results":   (1320, 160),
    "strategy_metadata":   (2100, 160),
    "backtest_results":    (2100, 980),
}

# 色設定
COLOR_BG         = (245, 247, 252)
COLOR_TABLE_HDR  = (26,  70,  140)   # ヘッダー背景（ダークブルー）
COLOR_TABLE_HDR2 = (55, 110, 200)    # サブヘッダー（ミディアムブルー）
COLOR_TABLE_BODY = (255, 255, 255)
COLOR_TABLE_ALT  = (240, 244, 255)   # 交互行（薄青）
COLOR_BORDER     = (160, 180, 220)
COLOR_TEXT_HDR   = (255, 255, 255)
COLOR_TEXT_BODY  = (25,  30,  55)
COLOR_TEXT_PK    = (180,  30,  30)   # PK強調（赤）
COLOR_TEXT_FK    = (180,  80,   0)   # FK強調（オレンジ）
COLOR_FK_LINE    = (40,  150,  40)   # FK矢印（緑）
COLOR_SHADOW     = (200, 205, 220)
COLOR_GRID       = (210, 218, 235)

COL_WIDTHS  = [230, 155, 65, 65]   # カラム名, 型, KEY, NULL
ROW_H       = 36
HDR_H       = 70
FONT_SIZE   = 20
FONT_SIZE_HDR = 22
FONT_SIZE_TITLE = 40

TABLE_W = sum(COL_WIDTHS) + 12     # 約 527px

IMG_W = 3000
IMG_H = 2100


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
    # ヘッダー + サブヘッダー行 + データ行
    return HDR_H + ROW_H + len(TABLES[tname]["columns"]) * ROW_H + 6


def _rounded_rect(draw: ImageDraw.Draw, x0, y0, x1, y1, radius, fill=None, outline=None, width=1):
    """角丸矩形を描画する。"""
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.ellipse([x0, y0, x0 + radius*2, y0 + radius*2], fill=fill)
    draw.ellipse([x1 - radius*2, y0, x1, y0 + radius*2], fill=fill)
    draw.ellipse([x0, y1 - radius*2, x0 + radius*2, y1], fill=fill)
    draw.ellipse([x1 - radius*2, y1 - radius*2, x1, y1], fill=fill)
    if outline:
        draw.arc([x0, y0, x0+radius*2, y0+radius*2], 180, 270, fill=outline, width=width)
        draw.arc([x1-radius*2, y0, x1, y0+radius*2], 270, 360, fill=outline, width=width)
        draw.arc([x0, y1-radius*2, x0+radius*2, y1], 90, 180, fill=outline, width=width)
        draw.arc([x1-radius*2, y1-radius*2, x1, y1], 0, 90, fill=outline, width=width)
        draw.line([x0+radius, y0, x1-radius, y0], fill=outline, width=width)
        draw.line([x0+radius, y1, x1-radius, y1], fill=outline, width=width)
        draw.line([x0, y0+radius, x0, y1-radius], fill=outline, width=width)
        draw.line([x1, y0+radius, x1, y1-radius], fill=outline, width=width)


def _draw_table(draw: ImageDraw.Draw, x: int, y: int, tname: str,
                font, font_bold, font_small) -> dict:
    """テーブルボックスを描画し、各カラムの中心Y座標を返す。"""
    tw = TABLE_W
    th = _table_height(tname)

    # ドロップシャドウ
    shadow_offset = 6
    draw.rectangle(
        [x + shadow_offset, y + shadow_offset, x + tw + shadow_offset, y + th + shadow_offset],
        fill=COLOR_SHADOW,
    )

    # テーブル本体背景
    draw.rectangle([x, y, x + tw, y + th], fill=COLOR_TABLE_BODY)

    # ヘッダー（角丸）
    _rounded_rect(draw, x, y, x + tw, y + HDR_H, radius=8, fill=COLOR_TABLE_HDR)
    # 下半分を四角で上書き（角丸は上だけ）
    draw.rectangle([x, y + HDR_H // 2, x + tw, y + HDR_H], fill=COLOR_TABLE_HDR)

    label_lines = TABLES[tname]["label"].split("\n")
    # テーブル名（1行目）
    draw.text(
        (x + tw // 2, y + 8),
        label_lines[0],
        fill=COLOR_TEXT_HDR,
        font=font_bold,
        anchor="mt",
    )
    # サブタイトル（2行目）
    if len(label_lines) > 1:
        draw.text(
            (x + tw // 2, y + 8 + FONT_SIZE_HDR + 4),
            label_lines[1],
            fill=(200, 220, 255),
            font=font_small,
            anchor="mt",
        )

    # カラムヘッダー行
    sub_y = y + HDR_H
    draw.rectangle([x, sub_y, x + tw, sub_y + ROW_H], fill=COLOR_TABLE_HDR2)
    headers = ["カラム名", "型", "KEY", "NULL"]
    cx = x + 6
    for i, h in enumerate(headers):
        draw.text((cx + 3, sub_y + 6), h, fill=COLOR_TEXT_HDR, font=font_small)
        cx += COL_WIDTHS[i]

    # データ行
    col_centers = {}
    for ri, col in enumerate(TABLES[tname]["columns"]):
        col_name, col_type, key, nullable, _ = col
        ry = sub_y + ROW_H + ri * ROW_H
        bg = COLOR_TABLE_BODY if ri % 2 == 0 else COLOR_TABLE_ALT
        draw.rectangle([x, ry, x + tw, ry + ROW_H], fill=bg)

        # グリッド線（列区切り）
        cx = x + 6
        for ci, val in enumerate([col_name, col_type, key, nullable]):
            if ci == 2 and val == "PK":
                color = COLOR_TEXT_PK
            elif ci == 2 and val == "FK":
                color = COLOR_TEXT_FK
            else:
                color = COLOR_TEXT_BODY
            draw.text((cx + 3, ry + 6), val, fill=color, font=font)
            cx += COL_WIDTHS[ci]

        col_centers[col_name] = ry + ROW_H // 2

    # 外枠
    draw.rectangle([x, y, x + tw, y + th], outline=COLOR_BORDER, width=2)
    # 行の区切り線
    for ri in range(len(TABLES[tname]["columns"]) + 2):
        ry = sub_y + ri * ROW_H
        draw.line([x + 1, ry, x + tw - 1, ry], fill=COLOR_GRID, width=1)
    # 列の区切り線
    cx = x + 6
    for w in COL_WIDTHS[:-1]:
        cx += w
        draw.line([cx, sub_y, cx, y + th], fill=COLOR_GRID, width=1)

    return col_centers


def _draw_fk_arrow(draw: ImageDraw.Draw, x1, y1, x2, y2, label: str,
                   color, font, offset_x: int = 0):
    """L字型の FK 矢印を描画する。offset_x で複数矢印の重なりを避ける。"""
    arrow_size = 9
    line_w = 2

    # 折れ点のX座標
    mid_x = (x1 + x2) // 2 + offset_x

    pts = [(x1, y1), (mid_x, y1), (mid_x, y2), (x2, y2)]
    draw.line(pts, fill=color, width=line_w)

    # 矢印頭
    if x2 >= mid_x:
        draw.polygon(
            [(x2, y2), (x2 - arrow_size, y2 - 4), (x2 - arrow_size, y2 + 4)],
            fill=color,
        )
    else:
        draw.polygon(
            [(x2, y2), (x2 + arrow_size, y2 - 4), (x2 + arrow_size, y2 + 4)],
            fill=color,
        )

    # ラベル（中点付近）
    lx = mid_x + 4
    ly = (y1 + y2) // 2 - FONT_SIZE // 2 - 2
    # ラベル背景
    bbox_w = len(label) * (FONT_SIZE - 4)
    draw.rectangle(
        [lx - 2, ly - 2, lx + bbox_w + 4, ly + FONT_SIZE + 2],
        fill=(255, 255, 240),
        outline=color,
        width=1,
    )
    draw.text((lx, ly), label, fill=color, font=font)


def gen_er_image(out_path: Path) -> None:
    img = Image.new("RGB", (IMG_W, IMG_H), COLOR_BG)
    draw = ImageDraw.Draw(img)

    font_title = _load_font(FONT_SIZE_TITLE)
    font_hdr   = _load_font(FONT_SIZE_HDR)
    font_body  = _load_font(FONT_SIZE)
    font_small = _load_font(FONT_SIZE - 2)

    # ── タイトル ────────────────────────────────────────────────────────
    draw.text(
        (IMG_W // 2, 30),
        "autoTRD  —  ER 図",
        fill=COLOR_TABLE_HDR,
        font=font_title,
        anchor="mt",
    )
    draw.text(
        (IMG_W // 2, 30 + FONT_SIZE_TITLE + 6),
        "Database Entity-Relationship Diagram",
        fill=(100, 120, 170),
        font=font_small,
        anchor="mt",
    )
    # タイトル下線
    draw.line(
        [60, 30 + FONT_SIZE_TITLE + 28, IMG_W - 60, 30 + FONT_SIZE_TITLE + 28],
        fill=COLOR_TABLE_HDR,
        width=2,
    )

    # ── テーブル描画 ────────────────────────────────────────────────────
    col_y_map = {}
    for tname, (tx, ty) in TABLE_POSITIONS.items():
        col_y_map[tname] = _draw_table(draw, tx, ty, tname, font_body, font_hdr, font_small)

    # ── FK 矢印 ─────────────────────────────────────────────────────────
    for i, (from_t, to_t, fk_col) in enumerate(FK_RELATIONS):
        fx, fy = TABLE_POSITIONS[from_t]
        tx, ty = TABLE_POSITIONS[to_t]

        from_col_y = col_y_map[from_t].get(fk_col, fy + HDR_H + 10)
        to_col_y   = col_y_map[to_t].get("id", ty + HDR_H + 10)

        # 始点・終点（テーブルの左右端）
        if fx >= tx:          # fromが右側 → 左端 → 右端
            x1 = fx
            x2 = tx + TABLE_W
        else:                  # fromが左側 → 右端 → 左端
            x1 = fx + TABLE_W
            x2 = tx

        # 複数矢印の重なり防止（同じ from→to ペアにoffsetを付ける）
        offset = (i % 2) * 20 - 10

        _draw_fk_arrow(
            draw, x1, from_col_y, x2, to_col_y,
            label=fk_col,
            color=COLOR_FK_LINE,
            font=font_small,
            offset_x=offset,
        )

    # ── 凡例 ────────────────────────────────────────────────────────────
    lx, ly = 60, IMG_H - 70
    # 背景
    draw.rectangle([lx - 10, ly - 10, lx + 520, ly + 40], fill=(235, 238, 248),
                   outline=COLOR_BORDER, width=1)
    # テーブルアイコン
    draw.rectangle([lx, ly + 4, lx + 18, ly + 22], fill=COLOR_TABLE_HDR)
    draw.text((lx + 26, ly + 4), "テーブル", fill=COLOR_TEXT_BODY, font=font_small)
    # FK線
    draw.line([lx + 120, ly + 13, lx + 160, ly + 13], fill=COLOR_FK_LINE, width=2)
    draw.polygon(
        [(lx+160, ly+13), (lx+153, ly+9), (lx+153, ly+17)],
        fill=COLOR_FK_LINE,
    )
    draw.text((lx + 168, ly + 4), "外部キー (FK)", fill=COLOR_TEXT_BODY, font=font_small)
    # PK
    draw.text((lx + 290, ly + 4), "PK = 主キー", fill=COLOR_TEXT_PK, font=font_small)
    # FK列
    draw.text((lx + 400, ly + 4), "FK = 外部キー列", fill=COLOR_TEXT_FK, font=font_small)

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
