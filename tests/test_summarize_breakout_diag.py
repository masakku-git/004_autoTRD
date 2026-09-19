"""summarize_breakout_diag: 診断ログの解析と集計"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from summarize_breakout_diag import parse_log_lines, summarize  # noqa: E402

LOG = [
    "2026-09-14 22:00:30 [DEBUG] autotrd: BREAKOUT-DIAG ticker=AAA result=skip reason=volume_low price=100.5 high20=100.0 gap_pct=-0.5 vol_ratio=1.2 vol_need=1.5",
    "2026-09-14 22:00:30 [DEBUG] autotrd: BREAKOUT-DIAG ticker=BBB result=skip reason=below_20d_high price=99.5 high20=100.0 gap_pct=0.5 vol_ratio=1.8 vol_need=1.5",
    "2026-09-14 22:00:31 [DEBUG] autotrd: BREAKOUT-DIAG ticker=CCC result=skip reason=no_breakout price=90 high20=100.0 gap_pct=10.0 vol_ratio=0.9 vol_need=1.5",
    "2026-09-14 22:00:31 [INFO] autotrd: Account: equity=1",  # 無関係な行
    "2026-09-15 22:00:30 [DEBUG] autotrd: BREAKOUT-DIAG ticker=AAA result=signal reason=buy price=101 high20=100.0 gap_pct=-1.0 vol_ratio=2.0 vol_need=1.5",
    "2026-09-15 22:00:30 [DEBUG] autotrd: BREAKOUT-DIAG ticker=AAA result=signal reason=buy price=101 high20=100.0 gap_pct=-1.0 vol_ratio=2.0 vol_need=1.5",
]


def test_parse_extracts_fields_and_ignores_other_lines():
    recs = parse_log_lines(LOG)
    assert len(recs) == 5
    assert recs[0]["ticker"] == "AAA" and recs[0]["reason"] == "volume_low"
    assert recs[0]["vol_ratio"] == 1.2 and recs[0]["gap_pct"] == -0.5


def test_summarize_counts_dedupes_and_finds_near_misses():
    s = summarize(parse_log_lines(LOG))
    assert s["total"] == 4  # 同日・同銘柄の重複(AAA 9/15)は1件
    assert s["by_reason"]["buy"] == 1 and s["by_reason"]["no_breakout"] == 1
    assert [r["ticker"] for r in s["near"]] == ["AAA", "BBB"]  # volume_low 優先、次に高値まで1%以内
    assert "2026-W38" in s["by_week"]


def test_empty_log():
    assert summarize([])["total"] == 0
