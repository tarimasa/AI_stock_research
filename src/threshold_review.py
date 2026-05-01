"""
threshold_review.py
クラウドに記録された推奨履歴 (backtest_log.json) をもとに
現行閾値の成績を振り返り、推奨閾値を提案する。

ローカルの backtest/optimize_thresholds.py は parquet データを必要とするため
GitHub Actions では実行できない。本モジュールはクラウドストレージ上の
記録のみで完結するため、Actions / ローカル双方で動く。

データソース:
  - Azure Blob: stock-bot/backtest_log.json (本番)
  - Local fallback: config/backtest_log_local.json (DRY_RUN / オフライン)

入力レコード (backtest_logger.log_recommendations が書く):
  signal_date, code, action, holding_days,
  signals: { breakout_5d, directional_vol_score, rsi5, rsi14,
             vol_ratio, week52_pos_pct, candle_pattern,
             ma25_diff_pct, screener_score },
  entry_price, take_profit_price, stop_loss_price,
  outcome: "win"|"loss"|"expired"|None, return_pct
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from backtest_logger import _load_log

JST = ZoneInfo("Asia/Tokyo")

# 現行スクリーナーの閾値（src/screener.py と同期）。
# レポート上で「現行 vs 提案」を示すために使用。
CURRENT_THRESHOLDS: dict[str, dict] = {
    "stage1_min_score": {
        "value": 60,
        "desc": "Stage1 通過の最小スコア (環境変数 STAGE1_MIN_SCORE / src/screener.py:40)",
    },
    "rsi5_oversold": {
        "value": 30,
        "desc": "RSI5 売られすぎ加点条件 (src/screener.py:919)",
    },
    "rsi14_oversold": {
        "value": 40,
        "desc": "RSI14 売られすぎ加点条件 (src/screener.py:908)",
    },
    "vol_ratio_surge": {
        "value": 1.3,
        "desc": "出来高急増 加点開始ライン (src/screener.py:889)",
    },
    "w52_pos_low": {
        "value": 40,
        "desc": "52週安値圏 加点条件 (src/screener.py:897)",
    },
    "dvs_positive": {
        "value": 0,
        "desc": "DVS 買い越し下限 (src/screener.py:878)",
    },
}

# 信頼性の最低サンプル数。これ未満は提案候補から除外する。
MIN_SAMPLES = 10
# 「強い」推奨を出すサンプル数（増えるほど信頼性アップ）。
STRONG_MIN_SAMPLES = 30


# ── データロード ─────────────────────────────────────────────────────────────


def load_decided_entries() -> list[dict]:
    """outcome が確定 (win/loss) しているレコードのみ返す。"""
    log = _load_log()
    return [e for e in log if e.get("outcome") in ("win", "loss")]


# ── 統計ユーティリティ ────────────────────────────────────────────────────────


@dataclass
class BinStats:
    """サンプル群に対する集計統計。"""
    n: int
    wins: int
    losses: int
    win_rate: float | None         # 0..1
    avg_return: float | None       # %
    median_return: float | None    # %
    expectancy: float | None       # win_rate × avg_win − loss_rate × avg_loss
    ev_score: float | None         # expectancy × log1p(n)（信頼性で重み付け）

    def to_dict(self) -> dict:
        return asdict(self)


def _stats(entries: Iterable[dict]) -> BinStats:
    entries = list(entries)
    n = len(entries)
    if n == 0:
        return BinStats(0, 0, 0, None, None, None, None, None)

    wins = [e for e in entries if e["outcome"] == "win"]
    losses = [e for e in entries if e["outcome"] == "loss"]
    n_w, n_l = len(wins), len(losses)
    win_rate = n_w / (n_w + n_l) if (n_w + n_l) > 0 else None

    returns = [e.get("return_pct") for e in entries if e.get("return_pct") is not None]
    avg_ret = sum(returns) / len(returns) if returns else None
    median_ret = None
    if returns:
        s = sorted(returns)
        m = len(s) // 2
        median_ret = (s[m] if len(s) % 2 == 1 else (s[m - 1] + s[m]) / 2)

    avg_win = (sum(e["return_pct"] for e in wins if e.get("return_pct") is not None)
               / n_w) if n_w else 0.0
    avg_loss = (sum(e["return_pct"] for e in losses if e.get("return_pct") is not None)
                / n_l) if n_l else 0.0
    expectancy = None
    if win_rate is not None:
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    ev_score = expectancy * math.log1p(n) if expectancy is not None else None

    return BinStats(
        n=n,
        wins=n_w,
        losses=n_l,
        win_rate=round(win_rate, 4) if win_rate is not None else None,
        avg_return=round(avg_ret, 4) if avg_ret is not None else None,
        median_return=round(median_ret, 4) if median_ret is not None else None,
        expectancy=round(expectancy, 4) if expectancy is not None else None,
        ev_score=round(ev_score, 4) if ev_score is not None else None,
    )


def _signal_value(entry: dict, key: str):
    """signals の値を安全に取り出す。"""
    return (entry.get("signals") or {}).get(key)


# ── 単一閾値スキャン ──────────────────────────────────────────────────────────


@dataclass
class ThresholdScan:
    """ある指標について、複数の候補閾値を評価した結果。"""
    signal: str
    direction: str  # "below" or "above"
    current: float | None
    rows: list[dict]            # [{threshold, ...stats}]
    best: dict | None           # ev_score 最大の row
    current_stats: BinStats | None  # 現行閾値でのサブセット統計

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "direction": self.direction,
            "current": self.current,
            "rows": self.rows,
            "best": self.best,
            "current_stats": self.current_stats.to_dict() if self.current_stats else None,
        }


def _scan_threshold(
    decided: list[dict],
    signal: str,
    extractor: Callable[[dict], float | None],
    candidates: list[float],
    direction: str,
    current: float | None,
    min_n: int = MIN_SAMPLES,
) -> ThresholdScan:
    """
    direction="below": value <= threshold（売られすぎ系）
    direction="above": value >= threshold（強さ系）
    """
    def _matches(value: float | None, thr: float) -> bool:
        if value is None:
            return False
        return value <= thr if direction == "below" else value >= thr

    rows: list[dict] = []
    for thr in candidates:
        subset = [e for e in decided if _matches(extractor(e), thr)]
        st = _stats(subset)
        if st.n >= min_n:
            rows.append({"threshold": thr, **st.to_dict()})

    best = None
    if rows:
        # 期待値が None の行は除外し、ev_score 最大を選ぶ
        ranked = [r for r in rows if r.get("ev_score") is not None]
        if ranked:
            best = max(ranked, key=lambda r: r["ev_score"])

    current_stats = None
    if current is not None:
        current_subset = [e for e in decided if _matches(extractor(e), current)]
        current_stats = _stats(current_subset)

    return ThresholdScan(
        signal=signal,
        direction=direction,
        current=current,
        rows=rows,
        best=best,
        current_stats=current_stats,
    )


# ── Stage1 スコアカットオフ ───────────────────────────────────────────────────


def scan_stage1_cutoff(decided: list[dict], min_n: int = MIN_SAMPLES) -> ThresholdScan:
    return _scan_threshold(
        decided=decided,
        signal="stage1_min_score",
        extractor=lambda e: _signal_value(e, "screener_score"),
        candidates=[0, 30, 45, 60, 75, 90, 105, 120, 150, 200],
        direction="above",
        current=CURRENT_THRESHOLDS["stage1_min_score"]["value"],
        min_n=min_n,
    )


def scan_signal_thresholds(decided: list[dict], min_n: int = MIN_SAMPLES) -> dict[str, ThresholdScan]:
    """RSI5, RSI14, vol_ratio, w52_pos, DVS のそれぞれで閾値スキャン。"""
    scans = {}

    scans["rsi5_oversold"] = _scan_threshold(
        decided, "rsi5_oversold",
        extractor=lambda e: _signal_value(e, "rsi5"),
        candidates=[10, 15, 20, 25, 30, 35, 40, 50],
        direction="below",
        current=CURRENT_THRESHOLDS["rsi5_oversold"]["value"],
        min_n=min_n,
    )
    scans["rsi14_oversold"] = _scan_threshold(
        decided, "rsi14_oversold",
        extractor=lambda e: _signal_value(e, "rsi14"),
        candidates=[20, 25, 30, 35, 40, 45, 50],
        direction="below",
        current=CURRENT_THRESHOLDS["rsi14_oversold"]["value"],
        min_n=min_n,
    )
    scans["vol_ratio_surge"] = _scan_threshold(
        decided, "vol_ratio_surge",
        extractor=lambda e: _signal_value(e, "vol_ratio"),
        candidates=[1.0, 1.2, 1.3, 1.5, 1.8, 2.0, 2.5],
        direction="above",
        current=CURRENT_THRESHOLDS["vol_ratio_surge"]["value"],
        min_n=min_n,
    )
    scans["w52_pos_low"] = _scan_threshold(
        decided, "w52_pos_low",
        extractor=lambda e: _signal_value(e, "week52_pos_pct"),
        candidates=[15, 20, 30, 40, 50, 60, 75],
        direction="below",
        current=CURRENT_THRESHOLDS["w52_pos_low"]["value"],
        min_n=min_n,
    )
    scans["dvs_positive"] = _scan_threshold(
        decided, "dvs_positive",
        extractor=lambda e: _signal_value(e, "directional_vol_score"),
        candidates=[-10, 0, 5, 10, 20, 30, 40],
        direction="above",
        current=CURRENT_THRESHOLDS["dvs_positive"]["value"],
        min_n=min_n,
    )
    return scans


# ── 提案ロジック ─────────────────────────────────────────────────────────────


def _propose_change(
    scan: ThresholdScan,
    require_strong: bool = False,
) -> dict | None:
    """
    現行とベストを比較し、改善が見込めれば提案を返す。
    require_strong=True の場合、サンプル数が少ない時は提案しない。
    """
    if scan.best is None:
        return None
    best = scan.best
    if require_strong and best["n"] < STRONG_MIN_SAMPLES:
        return None
    if scan.current_stats is None or scan.current_stats.ev_score is None:
        # 現行統計が出せない場合でも best が有意なら提案
        return {
            "current": scan.current,
            "proposed": best["threshold"],
            "current_ev_score": None,
            "proposed_ev_score": best["ev_score"],
            "proposed_n": best["n"],
            "proposed_win_rate": best["win_rate"],
            "proposed_expectancy": best["expectancy"],
            "rationale": "現行閾値ではサンプルが不足しているため、推奨閾値を提示",
        }

    if best["ev_score"] is None or best["ev_score"] <= scan.current_stats.ev_score:
        return None
    if best["threshold"] == scan.current:
        return None

    return {
        "current": scan.current,
        "proposed": best["threshold"],
        "current_ev_score": scan.current_stats.ev_score,
        "proposed_ev_score": best["ev_score"],
        "proposed_n": best["n"],
        "proposed_win_rate": best["win_rate"],
        "proposed_expectancy": best["expectancy"],
        "rationale": (
            f"ev_score 改善: {scan.current_stats.ev_score:+.3f} → {best['ev_score']:+.3f}"
        ),
    }


def build_proposals(stage1: ThresholdScan, signals: dict[str, ThresholdScan]) -> dict:
    proposals: dict = {}
    p = _propose_change(stage1, require_strong=True)
    if p:
        proposals["stage1_min_score"] = p
    for key, scan in signals.items():
        p = _propose_change(scan, require_strong=False)
        if p:
            proposals[key] = p
    return proposals


# ── レポート生成 ─────────────────────────────────────────────────────────────


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "  -  "
    return f"{v:+.{digits}f}%"


def _fmt_rate(v: float | None) -> str:
    if v is None:
        return "  -  "
    return f"{v * 100:5.1f}%"


def _fmt_scan_table(scan: ThresholdScan) -> str:
    if not scan.rows:
        return f"  (サンプル < {MIN_SAMPLES} のためスキャン結果なし)\n"

    sym = "≤" if scan.direction == "below" else "≥"
    header = (
        f"  {'閾値':>8}  {'件数':>4} {'勝':>3}/{'負':>3}  "
        f"{'勝率':>6}  {'平均R':>7}  {'期待値':>8}  {'ev_score':>9}"
    )
    sep = "  " + "-" * (len(header) - 2)
    lines = [header, sep]
    best_thr = scan.best["threshold"] if scan.best else None
    for r in scan.rows:
        mark = "★" if (best_thr is not None and r["threshold"] == best_thr) else " "
        cur_mark = "(現)" if (scan.current is not None and r["threshold"] == scan.current) else "    "
        lines.append(
            f"  {sym}{r['threshold']:>6}  {r['n']:>4} {r['wins']:>3}/{r['losses']:>3}  "
            f"{_fmt_rate(r['win_rate'])}  {_fmt_pct(r['avg_return'])}  "
            f"{_fmt_pct(r['expectancy'])}  {r['ev_score']:>9.3f}{mark}{cur_mark}"
        )
    return "\n".join(lines) + "\n"


def generate_threshold_report() -> str:
    """全文テキストレポートを返す（GitHub Actions ログ・LINE 共用）。"""
    decided = load_decided_entries()

    if not decided:
        return (
            "📊 推奨閾値 振り返りレポート\n"
            "確定済み (win/loss) 推奨がまだありません。"
            "もう数週間データを蓄積してから再実行してください。"
        )

    if len(decided) < MIN_SAMPLES:
        return (
            "📊 推奨閾値 振り返りレポート\n"
            f"確定済み推奨が {len(decided)} 件と少ないため、信頼できる閾値提案ができません "
            f"(必要: 最低 {MIN_SAMPLES} 件)。"
        )

    overall = _stats(decided)
    stage1_scan = scan_stage1_cutoff(decided)
    signal_scans = scan_signal_thresholds(decided)
    proposals = build_proposals(stage1_scan, signal_scans)

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("📊 推奨閾値 振り返りレポート")
    lines.append(f"生成日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}")
    lines.append(f"分析対象: 確定済み推奨 {overall.n} 件 (勝 {overall.wins} / 負 {overall.losses})")
    if overall.win_rate is not None:
        lines.append(f"全体勝率: {overall.win_rate * 100:.1f}%  / "
                     f"平均リターン: {_fmt_pct(overall.avg_return)} / "
                     f"期待値: {_fmt_pct(overall.expectancy)}")
    lines.append("=" * 72)

    # 現行閾値の成績
    lines.append("\n■ 現行閾値の成績（条件を満たした推奨だけを集計）")
    rows = [
        ("Stage1 score ≥ {:.0f}".format(stage1_scan.current or 0), stage1_scan.current_stats),
        ("RSI5 ≤ {:.0f}".format(signal_scans["rsi5_oversold"].current or 0),
         signal_scans["rsi5_oversold"].current_stats),
        ("RSI14 ≤ {:.0f}".format(signal_scans["rsi14_oversold"].current or 0),
         signal_scans["rsi14_oversold"].current_stats),
        ("vol_ratio ≥ {:.1f}".format(signal_scans["vol_ratio_surge"].current or 0),
         signal_scans["vol_ratio_surge"].current_stats),
        ("week52_pos ≤ {:.0f}".format(signal_scans["w52_pos_low"].current or 0),
         signal_scans["w52_pos_low"].current_stats),
        ("DVS ≥ {:.0f}".format(signal_scans["dvs_positive"].current or 0),
         signal_scans["dvs_positive"].current_stats),
    ]
    lines.append(f"  {'条件':<28} {'件数':>4}  {'勝率':>6}  {'平均R':>7}  {'期待値':>8}")
    lines.append("  " + "-" * 70)
    for cond, st in rows:
        if st is None or st.n == 0:
            lines.append(f"  {cond:<28} {'-':>4}  {'-':>6}  {'-':>7}  {'-':>8}")
            continue
        lines.append(
            f"  {cond:<28} {st.n:>4}  {_fmt_rate(st.win_rate)}  "
            f"{_fmt_pct(st.avg_return)}  {_fmt_pct(st.expectancy)}"
        )

    # 各閾値スキャン
    lines.append("\n■ Stage1 score カットオフ スキャン")
    lines.append(_fmt_scan_table(stage1_scan))
    for key, label in [
        ("rsi5_oversold", "RSI5 売られすぎ閾値"),
        ("rsi14_oversold", "RSI14 売られすぎ閾値"),
        ("vol_ratio_surge", "出来高急増ライン"),
        ("w52_pos_low", "52週安値圏 上限"),
        ("dvs_positive", "DVS 下限"),
    ]:
        lines.append(f"■ {label} スキャン")
        lines.append(_fmt_scan_table(signal_scans[key]))

    # 提案サマリー
    lines.append("■ 推奨閾値サマリー（現行 → 提案）")
    if not proposals:
        lines.append("  現行が既にほぼ最適、または提案するに足る差が見つかりませんでした。")
    else:
        for key, p in proposals.items():
            desc = CURRENT_THRESHOLDS.get(key, {}).get("desc", "")
            cur = p["current"]
            new = p["proposed"]
            wr = (p["proposed_win_rate"] or 0) * 100 if p["proposed_win_rate"] is not None else 0
            lines.append(
                f"  - {key}: {cur} → {new}  "
                f"(n={p['proposed_n']}, 勝率={wr:.1f}%, 期待値={_fmt_pct(p['proposed_expectancy'])})"
            )
            if desc:
                lines.append(f"      {desc}")
            lines.append(f"      根拠: {p['rationale']}")

    lines.append("\n" + "=" * 72)
    lines.append("【注意】")
    lines.append(f"  - サンプル < {MIN_SAMPLES} 件の閾値はノイズが大きいため除外しています。")
    lines.append(f"  - Stage1 カットオフ提案はさらに厳しく n ≥ {STRONG_MIN_SAMPLES} を要求します。")
    lines.append("  - 振り返りは過去実績であり、将来のリターンを保証しません。")
    lines.append("  - 反映する場合は src/screener.py の閾値 / 環境変数 STAGE1_MIN_SCORE を更新してください。")
    lines.append("=" * 72)

    return "\n".join(lines)


# ── 機械可読サマリー（CIアーティファクト用） ────────────────────────────────


def build_summary() -> dict:
    """CI のアーティファクトとして保存する JSON サマリー。"""
    decided = load_decided_entries()
    if not decided:
        return {
            "generated_at": datetime.now(JST).isoformat(),
            "n_decided": 0,
            "overall": None,
            "current_thresholds": {k: v["value"] for k, v in CURRENT_THRESHOLDS.items()},
            "scans": {},
            "proposals": {},
        }

    overall = _stats(decided)
    stage1_scan = scan_stage1_cutoff(decided)
    signal_scans = scan_signal_thresholds(decided)
    proposals = build_proposals(stage1_scan, signal_scans)

    return {
        "generated_at": datetime.now(JST).isoformat(),
        "n_decided": overall.n,
        "overall": overall.to_dict(),
        "current_thresholds": {k: v["value"] for k, v in CURRENT_THRESHOLDS.items()},
        "scans": {
            "stage1_min_score": stage1_scan.to_dict(),
            **{k: v.to_dict() for k, v in signal_scans.items()},
        },
        "proposals": proposals,
    }


def write_summary(out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(build_summary(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


if __name__ == "__main__":
    print(generate_threshold_report())
