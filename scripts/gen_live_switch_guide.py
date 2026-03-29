"""本番切り替え手順書 PDF を生成するスクリプト。

使い方:
    python scripts/gen_live_switch_guide.py
出力:
    doc/live_switch_guide.pdf
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ── フォント登録 ────────────────────────────────────────────────────────────
pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))
FONT_SANS = "HeiseiKakuGo-W5"
FONT_SERIF = "HeiseiMin-W3"

# ── カラー定義（position_sizing.pdf に合わせる） ────────────────────────────
C_HEADER_BG = colors.HexColor("#1a3a5c")   # ダークネイビー
C_HEADER_FG = colors.white
C_SUB_BG    = colors.HexColor("#2d6a9f")   # ミディアムブルー
C_ACCENT    = colors.HexColor("#e8711a")   # オレンジ（強調）
C_WARN_BG   = colors.HexColor("#fff3cd")   # 警告背景（薄黄）
C_WARN_FG   = colors.HexColor("#856404")   # 警告文字
C_ROW_ODD   = colors.HexColor("#f0f4f8")
C_ROW_EVEN  = colors.white
C_BORDER    = colors.HexColor("#cccccc")
C_GREEN     = colors.HexColor("#155724")
C_GREEN_BG  = colors.HexColor("#d4edda")
C_RED_FG    = colors.HexColor("#721c24")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


def _style(name, **kw) -> ParagraphStyle:
    base = getSampleStyleSheet()["Normal"]
    defaults = dict(fontName=FONT_SANS, fontSize=10, leading=16,
                    textColor=colors.black)
    defaults.update(kw)
    return ParagraphStyle(name, parent=base, **defaults)


S_TITLE     = _style("title",    fontSize=22, leading=30, textColor=C_HEADER_BG,
                      fontName=FONT_SANS, spaceAfter=2)
S_SUBTITLE  = _style("subtitle", fontSize=11, textColor=C_SUB_BG, spaceAfter=6)
S_FOOTER    = _style("footer",   fontSize=8,  textColor=colors.grey)
S_SECTION   = _style("section",  fontSize=12, textColor=C_HEADER_FG,
                      fontName=FONT_SANS, leading=18)
S_BODY      = _style("body",     fontSize=10, leading=16, spaceAfter=4)
S_WARN      = _style("warn",     fontSize=9,  textColor=C_RED_FG, leading=14)
S_CODE      = _style("code",     fontSize=9,  fontName="Courier",
                      textColor=colors.HexColor("#333333"), leading=14,
                      backColor=colors.HexColor("#f4f4f4"), leftIndent=6)
S_NOTE      = _style("note",     fontSize=9,  textColor=C_WARN_FG, leading=14,
                      leftIndent=4)


def section_header(text: str):
    """■ セクションヘッダーブロック"""
    data = [[Paragraph(f"■ {text}", S_SECTION)]]
    t = Table(data, colWidths=[PAGE_W - MARGIN * 2])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_HEADER_BG),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    return t


def two_col_table(rows, col_widths=None, header=None):
    """汎用2カラムテーブル"""
    col_widths = col_widths or [55 * mm, PAGE_W - MARGIN * 2 - 55 * mm]
    data = []
    if header:
        data.append([Paragraph(h, _style("th", fontSize=9, textColor=C_HEADER_FG,
                                          fontName=FONT_SANS)) for h in header])
    for row in rows:
        data.append([Paragraph(str(c), S_BODY) for c in row])

    style = [
        ("GRID",       (0, 0), (-1, -1), 0.5, C_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), C_SUB_BG),
            ("FONTNAME",   (0, 0), (-1, 0), FONT_SANS),
        ]
        row_start = 1
    else:
        row_start = 0

    for i in range(row_start, len(data)):
        bg = C_ROW_ODD if i % 2 == 0 else C_ROW_EVEN
        style.append(("BACKGROUND", (0, i), (-1, i), bg))

    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle(style))
    return t


def warn_box(text: str):
    """警告ボックス"""
    data = [[Paragraph(text, S_WARN)]]
    t = Table(data, colWidths=[PAGE_W - MARGIN * 2])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), colors.HexColor("#f8d7da")),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#f5c6cb")),
    ]))
    return t


def note_box(text: str):
    """注意ボックス（黄）"""
    data = [[Paragraph(text, S_NOTE)]]
    t = Table(data, colWidths=[PAGE_W - MARGIN * 2])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_WARN_BG),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#ffc107")),
    ]))
    return t


def checklist_table(items):
    """チェックリスト形式テーブル"""
    data = [[Paragraph("", S_BODY),
             Paragraph("作業内容", _style("th2", fontSize=9, textColor=C_HEADER_FG,
                                          fontName=FONT_SANS)),
             Paragraph("説明", _style("th2", fontSize=9, textColor=C_HEADER_FG,
                                       fontName=FONT_SANS))]]
    for item, desc in items:
        data.append([
            Paragraph("☐", _style("cb", fontSize=12, textColor=C_ACCENT)),
            Paragraph(item, S_BODY),
            Paragraph(desc, _style("desc", fontSize=9, textColor=colors.HexColor("#555555"),
                                    leading=13)),
        ])
    col_w = [10 * mm, 65 * mm, PAGE_W - MARGIN * 2 - 75 * mm]
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), C_HEADER_BG),
        ("SPAN",       (0, 0), (0, 0)),
        ("GRID",       (0, 0), (-1, -1), 0.5, C_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
    ]
    for i in range(1, len(data)):
        bg = C_ROW_ODD if i % 2 == 1 else C_ROW_EVEN
        style.append(("BACKGROUND", (0, i), (-1, i), bg))

    t = Table(data, colWidths=col_w)
    t.setStyle(TableStyle(style))
    return t


def build_pdf(out_path: Path):
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN,  bottomMargin=MARGIN,
    )

    story = []

    # ── タイトル ──────────────────────────────────────────────────────────
    story.append(Paragraph("本番環境切り替え手順書", S_TITLE))
    story.append(Paragraph("シミュレーション → リアル口座への移行チェックリスト", S_SUBTITLE))
    story.append(HRFlowable(width="100%", thickness=2, color=C_HEADER_BG))
    story.append(Spacer(1, 6 * mm))

    # ── 1. 事前確認 ──────────────────────────────────────────────────────
    story.append(section_header("事前確認"))
    story.append(Spacer(1, 3 * mm))
    story.append(checklist_table([
        ("moomoo 実口座ログイン確認",
         "moomoo アプリ（またはPC版）で実口座にログインし、米国株の取引権限が有効か確認する"),
        ("OpenD が実口座に接続",
         "OpenD のステータスで REAL 口座接続が確立されていること（SIMULATE でないこと）"),
        ("取引資金の確認",
         "実口座の入金額・現金残高を確認し、ポジションサイジングが資産規模に合っているか確認する"),
        ("リスク設定の確認",
         ".env の MAX_POSITIONS・RISK_PER_TRADE_PCT が意図した値になっているか確認する"),
    ]))
    story.append(Spacer(1, 5 * mm))

    # ── 2. .env 修正 ─────────────────────────────────────────────────────
    story.append(section_header(".env ファイルの修正"))
    story.append(Spacer(1, 3 * mm))
    story.append(two_col_table(
        rows=[
            ("DRY_RUN",           "true  →  false",  "false にすると実際の注文が発生する"),
            ("MOOMOO_TRADE_ENV",  "SIMULATE  →  REAL", "REAL にすると実口座で約定する"),
        ],
        col_widths=[50 * mm, 55 * mm, PAGE_W - MARGIN * 2 - 105 * mm],
        header=["パラメータ", "変更内容", "説明"],
    ))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "編集コマンド（VPS上で実行）:", S_BODY))
    story.append(Paragraph(
        "vi /home/trader/autoTRD/.env", S_CODE))
    story.append(Spacer(1, 5 * mm))

    # ── 3. DB リセット ───────────────────────────────────────────────────
    story.append(section_header("データベースのリセット"))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "シミュレーション中のデータと本番データが混在しないよう、以下の3テーブルをクリアします。",
        S_BODY))
    story.append(Spacer(1, 2 * mm))
    story.append(two_col_table(
        rows=[
            ("orders",               "注文履歴。DRY_RUN 中の仮注文が含まれる"),
            ("trade_log",            "エントリー/エグジット記録。シミュレーション分を除去する"),
            ("portfolio_snapshots",  "資産スナップショット。DRY_RUN 中は $3,300 固定値が入っている"),
        ],
        col_widths=[55 * mm, PAGE_W - MARGIN * 2 - 55 * mm],
        header=["テーブル名", "リセットが必要な理由"],
    ))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("実行 SQL（psql または DBeaver で実行）:", S_BODY))
    story.append(Paragraph(
        "TRUNCATE TABLE orders, trade_log, portfolio_snapshots RESTART IDENTITY;",
        S_CODE))
    story.append(Spacer(1, 2 * mm))
    story.append(note_box(
        "market_conditions・screening_results・strategy_metadata・backtest_results は"
        "そのまま残して問題ありません。"))
    story.append(Spacer(1, 5 * mm))

    # ── 4. 初回稼働確認 ──────────────────────────────────────────────────
    story.append(section_header("初回稼働確認"))
    story.append(Spacer(1, 3 * mm))
    story.append(checklist_table([
        ("市場が閉まっている時間に起動",
         "土日・米国夜間（日本時間）に起動し、エラーなく動作するか確認する"),
        ("ログを目視確認",
         "python3 src/main.py 2>&1 | tee /tmp/live_test.log を実行し、"
         "REAL モードで起動していることをログで確認する"),
        ("Slack 通知の確認",
         "通知内容に DRY_RUN の記載がなく、実口座の資産額が反映されているか確認する"),
        ("小額での初回注文確認",
         "最初の取引日は注文が moomoo の実口座履歴に反映されることを手動で確認する"),
    ]))
    story.append(Spacer(1, 5 * mm))

    # ── 5. ロールバック手順 ──────────────────────────────────────────────
    story.append(section_header("問題発生時のロールバック手順"))
    story.append(Spacer(1, 3 * mm))
    story.append(two_col_table(
        rows=[
            ("Step 1", ".env を元に戻す",
             "DRY_RUN=true / MOOMOO_TRADE_ENV=SIMULATE に変更してプロセスを再起動"),
            ("Step 2", "未約定注文をキャンセル",
             "moomoo アプリで当日の未約定注文を手動キャンセルする"),
            ("Step 3", "ポジションを確認",
             "既に約定したポジションは手動で管理するか、moomoo アプリで手動決済する"),
        ],
        col_widths=[15 * mm, 45 * mm, PAGE_W - MARGIN * 2 - 60 * mm],
        header=["", "作業", "詳細"],
    ))
    story.append(Spacer(1, 5 * mm))

    # ── 警告 ─────────────────────────────────────────────────────────────
    story.append(warn_box(
        "⚠ 注意: DRY_RUN=false に変更した瞬間から実際のお金で注文が発生します。"
        "切り替え前にリスク設定・ポジションサイズを必ず再確認してください。"
        "本番稼働初日は相場が閉まっている時間帯に設定変更し、翌営業日の動作を観察することを強く推奨します。"
    ))

    # ── フッター ─────────────────────────────────────────────────────────
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("autoTRD  本番環境切り替え手順書  2026-03-29", S_FOOTER))

    doc.build(story)
    print(f"PDF 生成完了: {out_path}")


if __name__ == "__main__":
    out = Path(__file__).parent.parent / "doc" / "live_switch_guide.pdf"
    build_pdf(out)
