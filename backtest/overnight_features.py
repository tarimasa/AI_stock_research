#!/usr/bin/env python3
"""
backtest/overnight_features.py

「T 終値 → T+1 寄付」の overnight gap-up を予測する特徴量を 1 つずつ評価する。

評価指標:
  - Spearman 相関   : 特徴量と overnight_return の単調関係の強さ
  - 上位10%平均(%) : その特徴量で上位 10% を選んだ時の平均 overnight return
  - 下位10%平均(%) : 下位 10% (リバース予測用)
  - top-bot差(%)   : effect size。大きいほど予測力あり
  - 上位10%勝率(%) : 勝率 50% を超えるか

候補特徴量 (日足のみから計算):
  1. close_in_range   : 引け位置 (C-L)/(H-L) ∈ [0,1]
  2. day_return       : (C-O)/O
  3. body_pct         : 陽線実体率 (C-O)/C
  4. upper_shadow_pct : 上ヒゲ
  5. lower_shadow_pct : 下ヒゲ
  6. vol_ratio        : V / MA(V,20)
  7. gap_today        : (O - prev_C)/prev_C
  8. overnight_bias_5 : 過去5日の overnight return 平均
  9. overnight_bias_20: 過去20日
 10. up_streak        : 連続陽線本数
 11. relative_atr     : ATR14/C
 12. ma25_diff_pct    : (C-MA25)/MA25
 13. rsi5             : 既存指標
 14. rsi14            : 既存指標
 15. dvs              : directional volume score
 16. range_pct        : (H-L)/C
 17. vol_x_body       : 複合 (vol_ratio × body_pct)
 18. close_in_range_x_vol : 複合 (close_in_range × vol_ratio)

最後に上位特徴量を組み合わせた合成スコアの閾値設計を試みる。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

from run_backtest import (
    TRAIN_START, TRAIN_END, VAL_START, VAL_END,
    load_all_data, apply_basic_filter, calc_all_signals,
)

DATA_DIR = PROJECT_ROOT / "backtest" / "data"


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """T 終値時点で観測可能な候補特徴量を全部足す。"""
    df = df.sort_values(["Code", "Date"]).reset_index(drop=True)
    g = df.groupby("Code", group_keys=False)

    # ── 当日 OHLCV から計算 ───────────────────────────────────────────
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    df["close_in_range"] = ((df["Close"] - df["Low"]) / rng).clip(0, 1)
    df["day_return"]     = (df["Close"] - df["Open"]) / df["Open"] * 100
    df["body_pct"]       = (df["Close"] - df["Open"]) / df["Close"] * 100
    df["upper_shadow"]   = (df["High"] - df[["Open", "Close"]].max(axis=1)) / df["Close"] * 100
    df["lower_shadow"]   = (df[["Open", "Close"]].min(axis=1) - df["Low"]) / df["Close"] * 100
    df["range_pct"]      = (df["High"] - df["Low"]) / df["Close"] * 100

    # ── 出来高 ──────────────────────────────────────────────────────────
    df["vol_ma20"] = g["Volume"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    df["vol_ratio"] = df["Volume"] / df["vol_ma20"].replace(0, np.nan)

    # ── 前日終値からのギャップ (今日の朝寄り) ─────────────────────────
    df["prev_close"] = g["Close"].shift(1)
    df["gap_today"] = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100

    # ── 過去の overnight bias ─────────────────────────────────────────
    overnight_t = (df["Open"] - g["Close"].shift(1)) / g["Close"].shift(1) * 100
    df["overnight_bias_5"]  = g["Open"].transform(
        lambda x: overnight_t.loc[x.index].rolling(5, min_periods=3).mean()
    )
    df["overnight_bias_20"] = g["Open"].transform(
        lambda x: overnight_t.loc[x.index].rolling(20, min_periods=10).mean()
    )

    # ── 連続陽線本数 ────────────────────────────────────────────────
    is_up = (df["Close"] > df["Open"]).astype(int)
    def _streak(s):
        out = []
        cnt = 0
        for v in s:
            cnt = cnt + 1 if v == 1 else 0
            out.append(cnt)
        return pd.Series(out, index=s.index)
    df["up_streak"] = g.apply(lambda d: _streak(is_up.loc[d.index])).reset_index(level=0, drop=True)

    # ── ATR14 ───────────────────────────────────────────────────────────
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - g["Close"].shift(1)).abs(),
        (df["Low"]  - g["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = g["Date"].transform(lambda x: tr.loc[x.index].rolling(14, min_periods=7).mean())
    df["relative_atr"] = df["atr14"] / df["Close"] * 100

    # ── 複合特徴量 ──────────────────────────────────────────────────────
    df["vol_x_body"]     = df["vol_ratio"] * df["body_pct"]
    df["close_pos_x_vol"] = df["close_in_range"] * df["vol_ratio"]

    return df


def eval_feature(df: pd.DataFrame, feat: str, label: str = "overnight_ret") -> dict:
    """1 特徴量を評価。
    extreme outlier の影響を排除するため Winsorize 平均 (±5% で頭打ち)
    と 中央値、勝率も計算する。"""
    sub = df[[feat, label]].dropna()
    if len(sub) < 1000:
        return {"feature": feat, "n": len(sub)}

    corr_s = sub[feat].rank().corr(sub[label].rank(), method="pearson")

    q10 = sub[feat].quantile(0.10)
    q90 = sub[feat].quantile(0.90)
    top = sub[sub[feat] >= q90][label]
    bot = sub[sub[feat] <= q10][label]

    def w_mean(s, cap=5.0):
        return float(s.clip(-cap, cap).mean())

    return {
        "feature":   feat,
        "n":         len(sub),
        "spearman":  round(corr_s, 4),
        "top_W平均":  round(w_mean(top), 3),
        "top_中央値": round(float(top.median()), 3),
        "top_勝率":   round(float((top > 0).mean() * 100), 1),
        "bot_W平均":  round(w_mean(bot), 3),
        "bot_中央値": round(float(bot.median()), 3),
        "bot_勝率":   round(float((bot > 0).mean() * 100), 1),
        "top-bot_W差": round(w_mean(top) - w_mean(bot), 3),
        "top_n":     len(top),
    }


def main():
    print("[features] データ準備中...")
    all_data = load_all_data()
    latest = all_data["Date"].max()
    valid_codes = set(apply_basic_filter(all_data[all_data["Date"] == latest])["Code"].unique())
    all_data = all_data[all_data["Code"].isin(valid_codes)].copy()

    df = calc_all_signals(all_data)
    df = add_features(df)

    # ── overnight ラベル ──────────────────────────────────────────────
    g = df.groupby("Code", group_keys=False)
    df["next_open"] = g["Open"].shift(-1)
    df["overnight_ret"] = (df["next_open"] - df["Close"]) / df["Close"] * 100
    df = df[df["overnight_ret"].notna()].copy()

    # ── 実運用フィルタ: 終値 500円 以上 & 出来高 10万株以上 ─────────
    # (低位株の outlier を排除して、実取引可能な銘柄だけ評価)
    before = len(df)
    df = df[(df["Close"] >= 500) & (df["Volume"] >= 100_000)].copy()
    print(f"[features] 実運用フィルタ後: {len(df):,} / {before:,} 行 "
          f"(Close>=500 & Vol>=100k)")
    # 手数料 0.2% を差し引いた純リターン
    df["overnight_ret_net"] = df["overnight_ret"] - 0.20
    print(f"[features] 評価対象: {len(df):,} 行")

    features = [
        "close_in_range", "day_return", "body_pct", "upper_shadow", "lower_shadow",
        "range_pct", "vol_ratio", "gap_today",
        "overnight_bias_5", "overnight_bias_20", "up_streak",
        "relative_atr", "ma25_diff_pct", "rsi5", "rsi14", "dvs",
        "vol_x_body", "close_pos_x_vol",
        "stage1_score",  # 比較用ベースライン
    ]

    # ── TRAIN / VAL で別々に評価 ──────────────────────────────────────
    train = df[(df["Date"] >= pd.Timestamp(TRAIN_START)) & (df["Date"] <= pd.Timestamp(TRAIN_END))]
    val   = df[(df["Date"] >= pd.Timestamp(VAL_START))   & (df["Date"] <= pd.Timestamp(VAL_END))]

    print(f"  TRAIN: {len(train):,} 行")
    print(f"  VAL:   {len(val):,} 行")

    all_results = []
    for period_name, period_df in [("TRAIN", train), ("VAL", val)]:
        print(f"\n{'='*92}")
        print(f"【{period_name}】 ラベル = overnight_ret_net (手数料-0.2%引き)")
        print(f"{'='*92}")
        rows = []
        for f in features:
            r = eval_feature(period_df, f, label="overnight_ret_net")
            r["period"] = period_name
            rows.append(r)
            all_results.append(r)
        rows_sorted = sorted(rows, key=lambda x: abs(x.get("top-bot_W差", 0)), reverse=True)
        print(f"  {'feature':<22} {'n':>9} {'corr':>7} "
              f"{'top_W平均':>9} {'top_中央':>9} {'top_勝率':>8} "
              f"{'bot_W平均':>10} {'bot_中央':>9} {'top-bot差':>10}")
        print("  " + "-" * 105)
        for r in rows_sorted:
            print(f"  {r['feature']:<22} {r.get('n',0):>9,} "
                  f"{r.get('spearman',0):>+7.4f} "
                  f"{r.get('top_W平均',0):>+8.3f}% "
                  f"{r.get('top_中央値',0):>+8.3f}% "
                  f"{r.get('top_勝率',0):>7.1f}% "
                  f"{r.get('bot_W平均',0):>+9.3f}% "
                  f"{r.get('bot_中央値',0):>+8.3f}% "
                  f"{r.get('top-bot_W差',0):>+9.3f}%")

    pd.DataFrame(all_results).to_csv(
        DATA_DIR / "overnight_features.csv", index=False, encoding="utf-8-sig",
    )
    print(f"\nCSV: {DATA_DIR / 'overnight_features.csv'}")


if __name__ == "__main__":
    main()
