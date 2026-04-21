# バックテスト 手順書

## 概要

JQuantsの2025/4/1〜2026/3/31のデータを使い、  
「どのシグナル条件で、どの利確・損切・保有日数が最も利益が出るか」を検証します。

---

## 前提条件の確認

```bash
# プロジェクトフォルダに移動
cd ~/AI_stock_research

# 仮想環境を有効化
source venv/bin/activate

# データが存在することを確認（ファイルが出ればOK）
ls data/backtest/daily_*.parquet
```

データがない場合は先にダウンロード：

```bash
python3 backtest/run_backtest.py --download
# → 約1〜2時間かかります（545日分 × 全銘柄）
# → 途中で止めても、次回は続きから再開されます
```

---

## STEP 1: 基本バックテスト（翌日1日のみ）

```bash
python3 backtest/run_backtest.py --analyze
```

**出力ファイル:**
- `data/backtest/results.csv` — Stage1通過銘柄の全シグナル＋結果

---

## STEP 2: 閾値最適化（単一シグナル分析）

```bash
python3 backtest/optimize_thresholds.py
```

**出力ファイル:**
- `data/backtest/optimization_report.txt` — テキストレポート
- `data/backtest/optimal_thresholds.json` — 最適閾値のJSON

---

## STEP 3: 複数パターン バックテスト（★メイン★）

**1〜5日保有 × TP/SL × シグナル条件の3,000通りを一括検証**

```bash
python3 backtest/multi_day_backtest.py
```

所要時間の目安：**5〜15分**

**出力ファイル:**

| ファイル | 内容 |
|---|---|
| `data/backtest/strategy_results.csv` | 全3,000戦略の成績 |
| `data/backtest/best_strategies.csv` | 期待値TOP20の戦略 |
| `data/backtest/top_totalreturn.csv` | 総利益TOP20の戦略 |

---

## 結果の読み方

```
フィルタ                       TP%    SL%  日数   件数    TP率   SL率  期待値     総EV
⑤ RSI5 < 20（超売られすぎ）  +3.0  -2.0%    3  2,134   9.8%  17.2%  +0.082%  174.8
```

| 列名 | 説明 |
|---|---|
| フィルタ | シグナルの絞り込み条件 |
| TP% | 利確ライン（翌日始値からこの%に達したら利確） |
| SL% | 損切ライン（翌日始値からこの%に達したら損切） |
| 日数 | 最大保有日数（到達しなければ最終日終値で決済） |
| 件数 | 対象シグナル数（多いほど再現性が高い） |
| TP率 | 利確ラインに達した割合 |
| SL率 | 損切ラインに達した割合 |
| **期待値** | **1トレードあたりの平均リターン（これが最重要）** |
| 総EV | 期待値 × 件数（全シグナルを全部取った場合の累積利益） |

**★ 期待値がプラス、かつ件数が多い戦略が「使える戦略」です**

---

## 検証するシグナルフィルタ一覧

| 番号 | フィルタ名 | 説明 |
|---|---|---|
| ① | 全シグナル | Stage1通過の全銘柄 |
| ② | RSI5 < 40 | 現行の条件 |
| ③〜⑦ | RSI5 < 30/25/20/15/10 | 段階的に絞り込み |
| ⑧〜⑩ | DVS > 0/10/20 | 買い越し出来高の強さ |
| ⑪ | ブレイクアウト | 5日高値を上抜け |
| ⑫⑬ | 出来高1.5/2倍 | 出来高急増 |
| ⑭⑮ | 52週安値圏20/40% | 底値圏 |
| ⑯〜㉑ | 複合条件 | RSI5 + DVS / 出来高など |
| ㉒㉓ | 曜日フィルタ | 月曜除外 / 水木のみ |
| ㉔㉕ | 複合+曜日 | RSI5 + 曜日の組み合わせ |

---

## 検証する利確・損切・保有日数の組み合わせ

| パラメータ | 選択肢 |
|---|---|
| 利確ライン | +1.5%, +2%, +3%, +5%, +7%, +10% |
| 損切ライン | -1%, -1.5%, -2%, -3%, -5% |
| 最大保有日数 | 1日, 2日, 3日, 5日 |

---

## よくある質問

**Q: 期待値がマイナスの戦略ばかり出た**  
A: SLをもう少し広げる（例: -3%）か、保有日数を増やす（例: 5日）と改善される傾向があります。

**Q: 件数が少ない戦略（n < 100）は信頼できる?**  
A: n < 100 は過去の偶然に左右される可能性が高いです。n ≥ 500 以上の戦略を優先してください。

**Q: データを再ダウンロードしたい**  
```bash
python3 backtest/run_backtest.py --download --force
```

**Q: 特定の銘柄だけ確認したい**  
`data/backtest/results.csv` をExcelで開き、Code列でフィルタしてください。

---

## 全ステップまとめ（コピペ用）

```bash
cd ~/AI_stock_research
source venv/bin/activate

# データダウンロード（初回のみ）
python3 backtest/run_backtest.py --download

# STEP 1: 基本バックテスト
python3 backtest/run_backtest.py --analyze

# STEP 2: 閾値分析
python3 backtest/optimize_thresholds.py

# STEP 3: 複数パターン検証（★一番重要）
python3 backtest/multi_day_backtest.py
```
