"""バックアップ・移行手順書 PDF を生成するスクリプト。

使い方:
    python scripts/gen_backup_guide.py
出力:
    doc/backup_guide.pdf
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

# ── カラー定義 ───────────────────────────────────────────────────────────────
C_HEADER_BG = colors.HexColor("#1a3a5c")
C_HEADER_FG = colors.white
C_SUB_BG    = colors.HexColor("#2d6a9f")
C_ACCENT    = colors.HexColor("#e8711a")
C_WARN_BG   = colors.HexColor("#fff3cd")
C_WARN_FG   = colors.HexColor("#856404")
C_ROW_ODD   = colors.HexColor("#f0f4f8")
C_ROW_EVEN  = colors.white
C_BORDER    = colors.HexColor("#cccccc")
C_RED_FG    = colors.HexColor("#721c24")
C_GREEN_BG  = colors.HexColor("#d4edda")
C_GREEN_FG  = colors.HexColor("#155724")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


def _style(name, **kw) -> ParagraphStyle:
    base = getSampleStyleSheet()["Normal"]
    defaults = dict(fontName=FONT_SANS, fontSize=10, leading=16,
                    textColor=colors.black)
    defaults.update(kw)
    return ParagraphStyle(name, parent=base, **defaults)


S_TITLE    = _style("title",    fontSize=22, leading=30, textColor=C_HEADER_BG,
                    fontName=FONT_SANS, spaceAfter=2)
S_SUBTITLE = _style("subtitle", fontSize=11, textColor=C_SUB_BG, spaceAfter=6)
S_FOOTER   = _style("footer",   fontSize=8,  textColor=colors.grey)
S_SECTION  = _style("section",  fontSize=12, textColor=C_HEADER_FG,
                    fontName=FONT_SANS, leading=18)
S_BODY     = _style("body",     fontSize=10, leading=16, spaceAfter=4)
S_WARN     = _style("warn",     fontSize=9,  textColor=C_RED_FG, leading=14)
S_CODE     = _style("code",     fontSize=9,  fontName="Courier",
                    textColor=colors.HexColor("#333333"), leading=14,
                    backColor=colors.HexColor("#f4f4f4"), leftIndent=6)
S_NOTE     = _style("note",     fontSize=9,  textColor=C_WARN_FG, leading=14,
                    leftIndent=4)
S_GREEN    = _style("green",    fontSize=9,  textColor=C_GREEN_FG, leading=14,
                    leftIndent=4)


def section_header(text: str):
    data = [[Paragraph(f"■ {text}", S_SECTION)]]
    t = Table(data, colWidths=[PAGE_W - MARGIN * 2])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_HEADER_BG),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    return t


def priority_table(rows):
    """優先度テーブル（3列）"""
    header = ["優先度", "対象", "理由"]
    data = [[Paragraph(h, _style(f"th_{i}", fontSize=9, textColor=C_HEADER_FG,
                                  fontName=FONT_SANS)) for i, h in enumerate(header)]]
    for row in rows:
        data.append([Paragraph(str(c), S_BODY) for c in row])

    col_w = [22 * mm, 55 * mm, PAGE_W - MARGIN * 2 - 77 * mm]
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), C_HEADER_BG),
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


def two_col_table(rows, col_widths=None, header=None):
    col_widths = col_widths or [55 * mm, PAGE_W - MARGIN * 2 - 55 * mm]
    data = []
    if header:
        data.append([Paragraph(h, _style(f"th2_{i}", fontSize=9,
                                          textColor=C_HEADER_FG,
                                          fontName=FONT_SANS))
                     for i, h in enumerate(header)])
    for row in rows:
        data.append([Paragraph(str(c), S_BODY) for c in row])

    style = [
        ("GRID",       (0, 0), (-1, -1), 0.5, C_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), C_SUB_BG),
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


def green_box(text: str):
    data = [[Paragraph(text, S_GREEN)]]
    t = Table(data, colWidths=[PAGE_W - MARGIN * 2])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_GREEN_BG),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#c3e6cb")),
    ]))
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
    story.append(Paragraph("バックアップ・サーバ移行手順書", S_TITLE))
    story.append(Paragraph("障害発生時の別サーバへの移行に必要な資産と手順", S_SUBTITLE))
    story.append(HRFlowable(width="100%", thickness=2, color=C_HEADER_BG))
    story.append(Spacer(1, 6 * mm))

    # ── 概要 ──────────────────────────────────────────────────────────────
    story.append(green_box(
        "コード資産は GitHub で管理されているため、新サーバでは git clone するだけで復元できます。"
        "それ以外に別途バックアップが必要な資産を本書でまとめます。"
    ))
    story.append(Spacer(1, 5 * mm))

    # ── 1. 優先度一覧 ──────────────────────────────────────────────────────
    story.append(section_header("バックアップ対象と優先度"))
    story.append(Spacer(1, 3 * mm))
    story.append(priority_table([
        ("🔴 最高", ".env ファイル",      "APIキー・Slack URL・リスク設定など秘密情報。Gitに含まれず再作成が困難"),
        ("🔴 最高", "PostgreSQL DB",      "トレード履歴・ポジション記録・バックテスト結果。一度失うと再現不可。Google Drive へ自動バックアップ済み"),
        ("🟡 中",   "ログファイル",       "実行ログ・エラーログ。Google Drive へ自動バックアップ済み"),
        ("🟡 中",   "OpenD 設定メモ",     "インストール手順・ポート設定・moomooログイン情報の記録"),
        ("🟢 低",   "コード（GitHub）",   "git clone で完全復元可能"),
    ]))
    story.append(Spacer(1, 5 * mm))

    # ── 2. .env ファイルのバックアップ ────────────────────────────────────
    story.append(section_header(".env ファイルのバックアップ"))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        ".env には以下の秘密情報が含まれています。定期的にローカルPCへコピーしてください。",
        S_BODY))
    story.append(Spacer(1, 2 * mm))
    story.append(two_col_table(
        rows=[
            ("SLACK_WEBHOOK_URL",      "Slack 通知先 URL"),
            ("MOOMOO_HOST / PORT",     "OpenD 接続先ホスト・ポート"),
            ("MOOMOO_TRADE_ENV",       "SIMULATE / REAL の切り替え"),
            ("DRY_RUN",                "true / false の切り替え"),
            ("RISK_PER_TRADE_PCT",     "1トレードあたりリスク割合"),
            ("MAX_POSITIONS",          "最大保有ポジション数"),
            ("DB_URL",                 "PostgreSQL 接続文字列"),
        ],
        col_widths=[60 * mm, PAGE_W - MARGIN * 2 - 60 * mm],
        header=["パラメータ", "説明"],
    ))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("ローカルPCへのコピーコマンド（ローカルPCのターミナルで実行）:", S_BODY))
    story.append(Paragraph(
        "scp trader@157.180.91.249:/home/trader/autoTRD/.env "
        "~/backup/autoTRD_env_$(date +%Y%m%d).txt",
        S_CODE))
    story.append(Spacer(1, 3 * mm))
    story.append(warn_box(
        "⚠ .env はパスワードや API キーを含む機密ファイルです。"
        "バックアップ先のローカルフォルダのアクセス権限に注意してください。"
        "Google Drive には機密情報がそのまま見える形でアップロードしないこと。"
    ))
    story.append(Spacer(1, 5 * mm))

    # ── 3. PostgreSQL DB バックアップ ─────────────────────────────────────
    story.append(section_header("PostgreSQL データベースのバックアップ（Google Drive 自動保存）"))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "以下のテーブルにトレード記録が蓄積されます。"
        "backup_db_gdrive.sh が毎日 cron で自動実行され、Google Drive へ保存します。",
        S_BODY))
    story.append(Spacer(1, 2 * mm))
    story.append(two_col_table(
        rows=[
            ("orders",              "全注文履歴（DRY_RUN・REAL 両方）"),
            ("trade_log",           "エントリー/エグジット・損益記録"),
            ("portfolio_snapshots", "日次資産スナップショット"),
            ("market_conditions",   "市場環境の記録"),
            ("screening_results",   "スクリーニング候補銘柄の記録"),
            ("strategy_metadata",   "戦略の定義・バージョン情報"),
            ("backtest_results",    "バックテスト結果"),
        ],
        col_widths=[60 * mm, PAGE_W - MARGIN * 2 - 60 * mm],
        header=["テーブル名", "内容"],
    ))
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("自動バックアップ設定（rclone インストール済み前提）:", S_BODY))
    story.append(Spacer(1, 2 * mm))
    story.append(two_col_table(
        rows=[
            ("バックアップスクリプト", "scripts/backup_db_gdrive.sh"),
            ("Google Drive 保存先",    "autoTRD/db_backups/"),
            ("ファイル形式",           "autoTRD_db_YYYYMMDD_HHMMSS.sql.gz（gzip圧縮）"),
            ("実行タイミング",         "毎日 午前2時（cron）"),
            ("Google Drive 保持数",    "最新30ファイル（古いものは自動削除）"),
            ("ローカル保持日数",       "3日分（古いものは自動削除）"),
            ("実行ログ",               "/home/trader/logs/backup.log"),
        ],
        col_widths=[55 * mm, PAGE_W - MARGIN * 2 - 55 * mm],
        header=["項目", "設定値"],
    ))
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("crontab への登録（crontab -e で追加）:", S_BODY))
    story.append(Paragraph(
        "0 2 * * * /home/trader/autoTRD/scripts/backup_db_gdrive.sh"
        " >> /home/trader/logs/backup.log 2>&1",
        S_CODE))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("手動実行（テスト・確認用）:", S_BODY))
    story.append(Paragraph(
        "bash /home/trader/autoTRD/scripts/backup_db_gdrive.sh",
        S_CODE))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("Google Drive の保存状況確認:", S_BODY))
    story.append(Paragraph(
        "rclone ls gdrive:autoTRD/db_backups/",
        S_CODE))
    story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("【初回セットアップ手順】rclone の Google Drive 認証:", S_BODY))
    story.append(Spacer(1, 1 * mm))
    story.append(two_col_table(
        rows=[
            ("Step 1",
             "ローカルPC に rclone をインストール: brew install rclone（Mac の場合）"),
            ("Step 2",
             "ローカルPC で rclone config を実行し、Google Drive（gdrive）を設定・ブラウザ認証"),
            ("Step 3",
             "ローカルPCの設定をVPSにコピー:\n"
             "scp ~/.config/rclone/rclone.conf trader@157.180.91.249:~/.config/rclone/rclone.conf"),
            ("Step 4",
             "VPSで接続確認: rclone lsd gdrive:"),
            ("Step 5",
             "保存先フォルダ作成: rclone mkdir gdrive:autoTRD/db_backups"),
        ],
        col_widths=[18 * mm, PAGE_W - MARGIN * 2 - 18 * mm],
        header=["手順", "内容"],
    ))
    story.append(Spacer(1, 5 * mm))

    # ── 4. ログファイルのバックアップ ──────────────────────────────────────
    story.append(section_header("ログファイルのバックアップ（Google Drive 自動保存）"))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "実行ログ・エラーログも Google Drive へ自動バックアップされます。"
        "障害発生後の原因調査に使用します。",
        S_BODY))
    story.append(Spacer(1, 2 * mm))
    story.append(two_col_table(
        rows=[
            ("/home/trader/logs/backup.log",   "DB バックアップの実行履歴・エラーログ"),
            ("/home/trader/logs/trader.log",    "autoTRD メイン実行ログ（取引・シグナル等）"),
            ("Google Drive 保存先",             "autoTRD/logs/"),
        ],
        col_widths=[70 * mm, PAGE_W - MARGIN * 2 - 70 * mm],
        header=["対象", "内容"],
    ))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("ログバックアップ確認:", S_BODY))
    story.append(Paragraph(
        "rclone ls gdrive:autoTRD/logs/",
        S_CODE))
    story.append(Spacer(1, 5 * mm))

    # ── 5. OpenD の設定 ────────────────────────────────────────────────────
    story.append(section_header("OpenD（moomoo ゲートウェイ）の設定"))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        "新サーバ構築時は OpenD を再インストールし、以下を設定する必要があります。",
        S_BODY))
    story.append(Spacer(1, 2 * mm))
    story.append(two_col_table(
        rows=[
            ("インストール",      "moomoo 公式サイトから Linux 版 OpenD をダウンロード・インストール"),
            ("ログイン情報",      "moomoo アカウントの ID / パスワード（自分で管理）"),
            ("接続ポート",        ".env の MOOMOO_PORT に合わせる（デフォルト: 11111）"),
            ("自動起動設定",      "systemd または cron で OS 起動時に OpenD が自動起動するよう設定"),
            ("セッション維持",    "長時間起動時のセッション切れに注意（定期ログイン確認推奨）"),
        ],
        col_widths=[45 * mm, PAGE_W - MARGIN * 2 - 45 * mm],
        header=["項目", "内容"],
    ))
    story.append(Spacer(1, 5 * mm))

    # ── 5. 新サーバ構築チェックリスト ─────────────────────────────────────
    story.append(section_header("新サーバ構築チェックリスト"))
    story.append(Spacer(1, 3 * mm))

    checklist_items = [
        ("OS・Python 環境",
         "Ubuntu 22.04 LTS / Python 3.10 以上をインストール"),
        ("システムユーザ作成",
         "useradd -m trader  でトレード専用ユーザを作成"),
        ("git clone",
         "git clone https://github.com/<repo>/autoTRD /home/trader/autoTRD"),
        ("依存パッケージ",
         "pip install -r requirements.txt  でPythonライブラリを一括インストール"),
        ("PostgreSQL セットアップ",
         "PostgreSQL をインストール・DB作成・スキーマ適用（alembic upgrade head）"),
        ("DB リストア",
         "psql -U trader autoTRD < autoTRD_db_YYYYMMDD.sql  でデータを復元"),
        (".env 配置",
         "バックアップした .env を /home/trader/autoTRD/.env に配置"),
        ("OpenD インストール・起動",
         "公式サイトからダウンロード・インストール・moomoo アカウントでログイン"),
        ("動作確認",
         "bash scripts/trader.sh check  で全モジュールの import を確認"),
        ("ping 確認",
         "bash scripts/trader.sh ping  で OpenD 接続状態を確認"),
        ("cron 設定",
         "crontab -e  で main.py の定期実行スケジュールを再設定"),
        ("バックアップ cron 設定",
         "crontab -e  で backup_db_gdrive.sh を毎日午前2時に登録"),
        ("rclone 設定コピー",
         "ローカルPCから rclone.conf を転送: "
         "scp ~/.config/rclone/rclone.conf trader@<new-server>:~/.config/rclone/rclone.conf"),
        ("sshd_config 設定",
         "UseDNS no / GSSAPIAuthentication no を設定してSSH応答を高速化"),
    ]

    # チェックリストをテーブルで表示
    data = [[
        Paragraph("", S_BODY),
        Paragraph("作業内容", _style("th_cl", fontSize=9, textColor=C_HEADER_FG, fontName=FONT_SANS)),
        Paragraph("詳細", _style("th_cl2", fontSize=9, textColor=C_HEADER_FG, fontName=FONT_SANS)),
    ]]
    for item, desc in checklist_items:
        data.append([
            Paragraph("☐", _style("cb", fontSize=12, textColor=C_ACCENT)),
            Paragraph(item, S_BODY),
            Paragraph(desc, _style("desc", fontSize=9,
                                    textColor=colors.HexColor("#555555"), leading=13)),
        ])

    col_w = [10 * mm, 55 * mm, PAGE_W - MARGIN * 2 - 65 * mm]
    cl_style = [
        ("BACKGROUND", (0, 0), (-1, 0), C_HEADER_BG),
        ("GRID",       (0, 0), (-1, -1), 0.5, C_BORDER),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("VALIGN",     (0, 0), (-1, -1), "TOP"),
    ]
    for i in range(1, len(data)):
        bg = C_ROW_ODD if i % 2 == 1 else C_ROW_EVEN
        cl_style.append(("BACKGROUND", (0, i), (-1, i), bg))

    cl_table = Table(data, colWidths=col_w)
    cl_table.setStyle(TableStyle(cl_style))
    story.append(cl_table)
    story.append(Spacer(1, 5 * mm))

    # ── 警告 ──────────────────────────────────────────────────────────────
    story.append(note_box(
        "DB バックアップとログは rclone により毎日 Google Drive へ自動保存されます。"
        "新サーバ構築時は Google Drive からダウンロードして復元できます。"
        "rclone.conf には Google Drive の認証情報が含まれるため、"
        "ローカルPCでの保管・アクセス権限の管理に注意してください。"
    ))

    # ── フッター ──────────────────────────────────────────────────────────
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("autoTRD  バックアップ・サーバ移行手順書  2026-03-30", S_FOOTER))

    doc.build(story)
    print(f"PDF 生成完了: {out_path}")


if __name__ == "__main__":
    out = Path(__file__).parent.parent / "doc" / "backup_guide.pdf"
    build_pdf(out)
