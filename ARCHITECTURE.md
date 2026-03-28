# AI株式リサーチBot - アーキテクチャ設計書

> Claude Code向け実装指示書  
> 対象市場: 日本株（東証）  
> コンセプト: 「今日、何を買うべきか」を毎朝LINEで通知するAIエージェント

-----

## 1. プロジェクト概要

### 目的

初心者でも迷わず行動できるよう、**「買うべき銘柄をAIが1〜3本に絞って推薦」** するLINE通知ボットを構築する。  
完全自動売買ではなく、**AIがリサーチ→人間が最終判断して手動発注**するハイブリッド構成。

### 最終的なアウトプット（LINEメッセージイメージ）

```
🤖 AI株式リサーチ - 2026/03/28（土）

━━━━━━━━━━━━━━━━
🏆 本日の推奨銘柄 TOP3
━━━━━━━━━━━━━━━━

🥇 【強く推奨】7203 トヨタ自動車
   現在値: ¥2,850 / 目標値: ¥3,100（+8.8%）
   推奨理由: EV販売好調・アナリスト目標引き上げ
   リスク: 為替円高リスクあり（★★☆）
   → 今すぐ少額で打診買い推奨

🥈 【様子見推奨】6758 ソニーグループ
   現在値: ¥12,300 / 目標値: ¥13,500（+9.8%）
   推奨理由: PS5販売回復・半導体部門好調
   リスク: 米国景気後退リスク（★★★）
   → ¥12,000割れで買い検討

🥉 【中長期向け】4502 武田薬品工業
   現在値: ¥3,890 / 目標値: ¥4,300（+10.5%）
   推奨理由: 新薬FDA承認期待・高配当（4.2%）
   リスク: 臨床試験失敗リスク（★☆☆）
   → 積立投資向き

━━━━━━━━━━━━━━━━
⚠️ 本日の市場リスク
━━━━━━━━━━━━━━━━
・日経平均前日比: -1.2%（注意）
・VIX: 18.5（やや不安定）
・円相場: 148円台（輸出株に追い風）

❗ 本情報は投資判断の参考です。
   最終決定は自己責任でお願いします。
```

-----

## 2. システム構成

2つのサブシステムで構成される。

```
【定時レポート】 GitHub Actions（7:30 / 11:30 JST）
┌─────────────────────────────────────────────┐
│              GitHub Actions                  │
│  07:30 JST（→8:30発注向け）                  │
│  11:30 JST（→12:00発注向け）                 │
└──────────────────┬──────────────────────────┘
                   │
         ┌─────────▼──────────┐
         │   main.py           │
         │   (Orchestrator)    │
         └──┬──────┬──────┬───┘
            │      │      │
   ┌────────▼─┐ ┌──▼───┐ ┌▼──────────────┐
   │ data_    │ │news_ │ │ claude_        │
   │ fetcher  │ │fetch │ │ analyzer       │
   │ .py      │ │er.py │ │ .py            │
   └────────┬─┘ └──┬───┘ └┬──────────────┘
            │      │      │
            └──────▼──────▘
                   │  portfolio_tracker.py も呼び出す
         ┌─────────▼──────────┐
         │  line_notifier.py   │
         │  （LINE送信）        │
         └────────────────────┘

【LINEコマンド受信】 Azure Container Apps（常時稼働）
┌────────────────────────────────────────────────┐
│  Azure Container Apps（無料枠）                  │
│  webhook_server.py（FastAPI）                    │
│                                                  │
│  受信コマンド例:                                  │
│   「追加 7203 100株 2650円」→ portfolio.json更新  │
│   「削除 7203」          → portfolio.json更新     │
│   「一覧」               → 保有株一覧をLINE返信   │
└───────────────────┬────────────────────────────┘
                    │ portfolio.jsonを読み書き
              ┌─────▼────────────┐
              │ Azure Blob Storage│
              │  portfolio.json   │
              │  （永続化）        │
              └──────────────────┘
```

> **なぜBlobが必要か：** GitHub Actionsとwebhook_serverは別プロセスで動く。両者がportfolio.jsonを共有するためにAzure Blob Storageを「共有ファイルストレージ」として使う。

### 使用サービス・コスト

|コンポーネント     |サービス                                   |月額コスト         |
|------------|---------------------------------------|--------------|
|定時レポート実行    |GitHub Actions（無料枠2,000分/月）            |¥0            |
|Webhookサーバー |Azure Container Apps（無料枠180,000vCPU秒/月）|¥0            |
|portfolio永続化|Azure Blob Storage（LRS / 1GBまで）        |¥3以下          |
|株価データ       |yfinance（無料）                           |¥0            |
|ニュースデータ     |RSS（Yahoo!ファイナンス/日経無料枠）                |¥0            |
|AI分析        |Claude API（claude-haiku-4-5）           |¥150〜400      |
|通知          |LINE Messaging API（無料枠1,000通/月）        |¥0            |
|**合計**      |                                       |**¥150〜400/月**|

-----

## 3. ディレクトリ構成

```
stock-ai-bot/
├── .github/
│   └── workflows/
│       └── daily_report.yml      # GitHub Actions定義（7:30 & 11:30）
├── src/
│   ├── main.py                   # エントリポイント・オーケストレーター
│   ├── data_fetcher.py           # 株価データ取得（yfinance）
│   ├── news_fetcher.py           # ニュース取得（RSS）
│   ├── claude_analyzer.py        # Claude APIで分析・推奨生成
│   ├── screener.py               # 銘柄スクリーニング（pandas_ta使用）
│   ├── portfolio_tracker.py      # 保有株管理・売却アラート
│   ├── portfolio_store.py        # portfolio.jsonのBlobStorage読み書き ★追加
│   ├── webhook_server.py         # LINEコマンド受信サーバー（FastAPI）★追加
│   └── line_notifier.py          # LINE送信
├── config/
│   └── watchlist.json            # 監視銘柄リスト
│   # portfolio.jsonはAzure Blob Storageで管理（ローカルにはサンプルのみ）
├── tests/
│   └── test_dry_run.py           # 通知なしで動作確認するテスト
├── Dockerfile                    # webhook_server用コンテナ定義 ★追加
├── requirements.txt
├── .env.example
└── README.md
```

-----

## 4. 各モジュールの仕様

### 4-1. `config/watchlist.json`

監視対象銘柄を管理するJSONファイル。

```json
{
  "indices": [
    "^N225",
    "^DJI"
  ],
  "stocks": [
    { "code": "7203.T", "name": "トヨタ自動車", "sector": "自動車" },
    { "code": "6758.T", "name": "ソニーグループ", "sector": "電機" },
    { "code": "9984.T", "name": "ソフトバンクグループ", "sector": "通信" },
    { "code": "4502.T", "name": "武田薬品工業", "sector": "医薬品" },
    { "code": "8306.T", "name": "三菱UFJ銀行", "sector": "金融" },
    { "code": "6861.T", "name": "キーエンス", "sector": "精密機器" },
    { "code": "7974.T", "name": "任天堂", "sector": "ゲーム" },
    { "code": "9432.T", "name": "NTT", "sector": "通信" },
    { "code": "6367.T", "name": "ダイキン工業", "sector": "機械" },
    { "code": "4063.T", "name": "信越化学工業", "sector": "化学" }
  ]
}
```

-----

### 4-2. `src/data_fetcher.py`

yfinanceを使い、各銘柄の株価・テクニカル指標を取得する。

**取得する指標：**

- 現在株価・前日比（%）
- 52週高値・安値
- PER・PBR・配当利回り
- RSI（14日）
- 移動平均（25日・75日）との乖離率
- 出来高の5日平均比

**インターフェース：**

```python
def fetch_stock_data(ticker: str) -> dict:
    """
    Returns:
    {
        "code": "7203.T",
        "name": "トヨタ自動車",
        "price": 2850,
        "change_pct": 1.2,
        "per": 8.2,
        "pbr": 1.1,
        "dividend_yield": 2.8,
        "rsi_14": 38.5,
        "ma25_diff_pct": -2.1,
        "ma75_diff_pct": 3.4,
        "volume_ratio": 1.35
    }
    """
```

-----

### 4-3. `src/news_fetcher.py`

RSSフィードからニュースを取得し、銘柄コードと突合する。

**取得元RSS：**

- Yahoo!ファイナンス: `https://finance.yahoo.co.jp/rss/news`
- 日本経済新聞（無料）: `https://www.nikkei.com/rss/`

**インターフェース：**

```python
def fetch_news_for_stock(stock_code: str, stock_name: str, hours: int = 24) -> list[dict]:
    """
    Returns: [
        {
            "title": "トヨタ、EV販売が過去最高を更新",
            "summary": "...",
            "published": "2026-03-28T06:00:00",
            "url": "https://..."
        }
    ]
    """
```

-----

### 4-4. `src/screener.py`

スコアリングにより、Claude APIに渡す銘柄を上位5〜10本に絞る（APIコスト削減のため）。

**採用根拠：** Qiitaで公開されたバックテスト研究（2022〜2024年の日本株検証）において「RSI25〜50 & MA乖離4〜8%」の組み合わせが地合い悪化時でもシャープレシオ0.8以上を記録した条件を参考に設計。指標計算は `pandas_ta` ライブラリで統一する。

**使用ライブラリ：** `pandas_ta`（TA-Libの代替。pipでエラーなくインストール可能）

**スコアリングロジック（100点満点）：**

```python
import pandas_ta as ta

def score_stock(df: pd.DataFrame, info: dict) -> float:
    """
    df: yfinanceから取得した日足OHLCVデータ（直近90日分）
    info: yfinance ticker.info（PER, PBR等）
    """
    score = 0.0

    # ① テクニカル指標の計算（pandas_taで統一）
    df.ta.rsi(length=14, append=True)          # RSI_14
    df.ta.macd(append=True)                    # MACD_12_26_9
    df.ta.bbands(length=20, append=True)       # ボリンジャーバンド
    df.ta.sma(length=25, append=True)          # SMA_25
    df.ta.sma(length=75, append=True)          # SMA_75

    latest = df.iloc[-1]
    close = latest["Close"]

    rsi = latest.get("RSI_14", 50)
    macd = latest.get("MACD_12_26_9", 0)
    macd_signal = latest.get("MACDs_12_26_9", 0)
    bb_lower = latest.get("BBL_20_2.0", 0)
    sma25 = latest.get("SMA_25", close)
    sma75 = latest.get("SMA_75", close)

    ma25_diff_pct = (close - sma25) / sma25 * 100  # 25日線乖離率

    # ② RSIスコア（40点）
    # バックテスト研究より: RSI25〜50で買いシグナルが有効
    if 25 <= rsi <= 35:   score += 40   # 強い売られすぎ（最高評価）
    elif 35 < rsi <= 50:  score += 25   # 軽度売られすぎ
    elif 50 < rsi <= 65:  score += 10   # 中立
    # RSI>70は除外（過熱圏）

    # ③ MA乖離スコア（25点）
    # バックテスト研究より: 乖離4〜8%が押し目買いの好機
    if -8 <= ma25_diff_pct <= -4:  score += 25  # 理想的な押し目
    elif -4 < ma25_diff_pct <= -2: score += 15  # 軽度押し目
    elif ma25_diff_pct > 0:        score += 5   # 上昇トレンド継続

    # ④ MACDシグナル（20点）
    if macd > macd_signal:         score += 20  # ゴールデンクロス状態
    elif macd > 0:                 score += 10  # MACDプラス圏

    # ⑤ ボリンジャーバンド（15点）
    if close <= bb_lower:          score += 15  # 下限タッチ（反発期待）

    # ⑥ ファンダメンタルズボーナス（最大10点）
    per = info.get("trailingPE", 999)
    pbr = info.get("priceToBook", 999)
    if per < 15:   score += 5   # 割安PER
    if pbr < 1.5:  score += 5   # 割安PBR

    return score

def screen(stocks: list) -> list:
    """watchlistの全銘柄をスコアリングし上位MAX_STOCKS件を返す"""
    scored = []
    for stock in stocks:
        df = fetch_ohlcv(stock["code"])   # data_fetcherから90日OHLCVを取得
        info = fetch_info(stock["code"])  # data_fetcherからticker.infoを取得
        s = score_stock(df, info)
        if s > 0:
            scored.append({**stock, "score": s})
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:MAX_STOCKS]
```

-----

### 4-5. `src/claude_analyzer.py`

スクリーニングで選ばれた上位銘柄について、Claude APIで総合分析・推奨を生成する。

**システムプロンプト：**

```python
SYSTEM_PROMPT = """
あなたは日本株の個人投資家向けアドバイザーです。
以下のルールを必ず守って分析・推奨を行ってください。

【出力ルール】
1. 推奨銘柄を1〜3本に必ず絞ること（多すぎると初心者が迷うため）
2. 各銘柄について「今すぐ買う」「押し目待ち」「見送り」の3択で明確に分類すること
3. 推奨理由は小学生でもわかる平易な言葉で2〜3文に収めること
4. リスクを★1〜3で示すこと（★1:低リスク、★3:高リスク）
5. 目標株価を必ず提示すること（根拠も1文で）
6. 相場全体が悪い場合は「本日は買い見送り推奨」と明示すること

【出力フォーマット】
JSON形式で出力すること。
{
  "market_condition": "良好|注意|悪化",
  "market_comment": "相場全体の一言コメント",
  "recommendations": [
    {
      "rank": 1,
      "code": "7203.T",
      "name": "トヨタ自動車",
      "action": "今すぐ買う|押し目待ち|見送り",
      "current_price": 2850,
      "target_price": 3100,
      "upside_pct": 8.8,
      "reason": "推奨理由（2〜3文）",
      "risk_level": 2,
      "risk_comment": "リスクの内容"
    }
  ],
  "caution": "本日の注意事項（なければnull）"
}
"""
```

**ユーザープロンプト構築：**

```python
def build_user_prompt(screened_stocks: list, market_data: dict) -> str:
    return f"""
## 本日の市場状況
- 日経平均: {market_data['nikkei']}円（前日比{market_data['nikkei_change']}%）
- ドル円: {market_data['usdjpy']}円
- 分析日: {today}

## スクリーニング通過銘柄（上位10本）
{json.dumps(screened_stocks, ensure_ascii=False, indent=2)}

上記データをもとに、初心者投資家向けの推奨レポートをJSON形式で生成してください。
"""
```

-----

### 4-6. `config/portfolio.json` / `src/portfolio_store.py`

**portfolio.jsonはAzure Blob Storageで管理する。** GitHub ActionsとAzure Container Apps（Webhookサーバー）の両方から読み書きできるよう共有ストレージを使用。

**portfolio_store.pyのインターフェース：**

```python
# Managed Identity or 接続文字列でBlobにアクセス
BLOB_CONTAINER = "stock-bot"
BLOB_NAME = "portfolio.json"

def load_portfolio() -> dict:
    """BlobからJSONを読み込む。存在しない場合は空のデフォルトを返す"""

def save_portfolio(portfolio: dict) -> None:
    """dictをJSONにシリアライズしてBlobに保存"""
```

**portfolio.jsonのスキーマ（Blob上のデータ）：**

```json
{
  "default_alerts": {
    "profit_pct": 15,
    "loss_pct": -8,
    "rsi_overbought": 70,
    "rsi_oversold": 30
  },
  "holdings": [
    {
      "code": "7203.T",
      "name": "トヨタ自動車",
      "shares": 100,
      "buy_price": 2650,
      "buy_date": "2026-01-15",
      "target_price": 3100,
      "stop_loss_pct": -8,
      "memo": "EV展開期待で打診買い"
    }
  ]
}
```

-----

### 4-7. `src/webhook_server.py` ★追加

LINEからのテキストコマンドを受信してportfolioを操作するFastAPIサーバー。Azure Container Appsにデプロイし常時稼働させる。

**対応コマンド一覧：**

|LINEで送るテキスト         |動作                  |
|--------------------|--------------------|
|`追加 7203 100株 2650円`|トヨタ100株を¥2,650で追加   |
|`追加 7203 100 2650`  |上記の省略形（株・円不要）       |
|`削除 7203`           |トヨタを削除（確定損益をLINEで返信）|
|`一覧`                |保有株と含み損益を一覧表示       |
|`ヘルプ`               |コマンド一覧を表示           |

**LINEメッセージのやり取りイメージ：**

```
【追加】
あなた: 「追加 7203 100 2650」
Bot:   「✅ トヨタ自動車を追加しました
        100株 × ¥2,650 = 取得額 ¥265,000
        目標株価未設定（デフォルト+15%で利確アラート）」

【削除】
あなた: 「削除 7203」
Bot:   「🗑️ トヨタ自動車を削除しました
        売却損益: +¥41,000（+15.5%）
        保有期間: 73日間」

【一覧】
あなた: 「一覧」
Bot:   「📦 保有株一覧（2銘柄）
        7203 トヨタ     100株 ¥3,061 +15.5% 🟢
        6758 ソニーG     50株 ¥12,450 +2.9% ⚪
        ━━━━━━━━━━━━
        合計評価損益: +¥58,500」
```

**コマンドパースロジック：**

```python
def parse_command(text: str) -> dict | None:
    """
    「追加 7203 100 2650」→ {"action": "add", "code": "7203.T", "shares": 100, "price": 2650}
    「削除 7203」         → {"action": "remove", "code": "7203.T"}
    「一覧」              → {"action": "list"}
    解析失敗              → None（ヘルプを返す）
    """
    text = text.strip().replace("　", " ")  # 全角スペース対応
    parts = text.split()

    if parts[0] in ["追加", "add"]:
        code = parts[1] + ".T" if not parts[1].endswith(".T") else parts[1]
        shares = int(re.sub(r"[^\d]", "", parts[2]))
        price = int(re.sub(r"[^\d]", "", parts[3]))
        return {"action": "add", "code": code, "shares": shares, "price": price}

    elif parts[0] in ["削除", "remove", "売却"]:
        code = parts[1] + ".T" if not parts[1].endswith(".T") else parts[1]
        return {"action": "remove", "code": code}

    elif parts[0] in ["一覧", "list", "ポートフォリオ"]:
        return {"action": "list"}

    return None
```

**FastAPIエンドポイント：**

```python
@app.post("/webhook")
async def webhook(request: Request):
    # LINE署名検証
    # イベントからテキスト取得
    # parse_command() でコマンド解析
    # portfolio_store.load_portfolio() で現在状態取得
    # コマンド実行 → portfolio_store.save_portfolio()
    # line_notifier で結果をLINE返信
```

-----

### 4-8. `src/line_notifier.py`

`portfolio.json` を読み込み、各保有株の現在損益・売却アラートを計算する。

**インターフェース：**

```python
def check_portfolio() -> dict:
    """
    portfolio.jsonを読み込み、全保有株の損益とアラートを返す。

    Returns:
    {
        "total_unrealized_pnl": 85000,
        "total_unrealized_pnl_pct": 6.2,
        "holdings": [
            {
                "code": "7203.T",
                "name": "トヨタ自動車",
                "shares": 100,
                "buy_price": 2650,
                "current_price": 3050,
                "unrealized_pnl": 40000,
                "unrealized_pnl_pct": 15.1,
                "rsi_14": 72.3,
                "alert": "利確推奨",      # "利確推奨"|"損切り推奨"|"RSI過熱"|"様子見"|null
                "alert_reason": "含み益+15.1%が目標ライン到達 & RSI72（過熱圏）"
            }
        ]
    }
    """
```

**アラート判定ロジック：**

```python
def judge_alert(holding: dict, current_price: float, rsi: float) -> tuple[str | None, str]:
    buy_price = holding["buy_price"]
    pnl_pct = (current_price - buy_price) / buy_price * 100

    # 損切りチェック（最優先）
    stop_loss = holding.get("stop_loss_pct", default_alerts["loss_pct"])
    if pnl_pct <= stop_loss:
        return "損切り推奨", f"含み損{pnl_pct:.1f}%が損切りライン{stop_loss}%を超過"

    # 目標株価チェック
    target = holding.get("target_price")
    if target and current_price >= target:
        return "利確推奨", f"目標株価¥{target}に到達（含み益+{pnl_pct:.1f}%）"

    # 含み益%チェック（target_price未設定時のフォールバック）
    if not target and pnl_pct >= default_alerts["profit_pct"]:
        return "利確推奨", f"含み益+{pnl_pct:.1f}%がデフォルト利確ライン到達"

    # RSI過熱チェック
    if rsi >= default_alerts["rsi_overbought"]:
        return "RSI過熱", f"RSI{rsi:.0f}（買われすぎ圏、利確タイミング候補）"
    if rsi <= default_alerts["rsi_oversold"]:
        return "RSI底値", f"RSI{rsi:.0f}（売られすぎ圏、追加購入候補）"

    return None, ""
```

**LINEメッセージへの組み込み（出力イメージ）：**

```
📦 保有株アラート

🔴 【損切り推奨】9984 ソフトバンクG
   取得: ¥8,200 → 現在: ¥7,450（-9.1%）
   含み損: -75,000円（100株）
   → 損切りラインを超過。早めの対応を推奨

🟡 【利確推奨】7203 トヨタ自動車
   取得: ¥2,650 → 現在: ¥3,060（+15.5%）
   含み益: +41,000円（100株）
   RSI: 72（過熱圏）
   → 目標株価付近 & RSI過熱。一部利確を検討

✅ 【様子見】6758 ソニーグループ
   取得: ¥12,100 → 現在: ¥12,450（+2.9%）
   含み益: +17,500円（50株）

━━━━━━━━━━━━━━━━
💰 ポートフォリオ合計
   評価損益: +83,500円（+5.8%）
```

-----

### 4-8. `src/line_notifier.py`

毎日の通知をFlex Messageで送信する。レポート本文の末尾に **「📝 銘柄を更新する」ボタン** を常に表示し、タップするとLIFFフォームが開く。

> **設計方針：** ユーザーは「追加」などのコマンドを覚える必要がない。毎朝・昼のレポートを受け取ったタイミングでそのままボタンをタップしてポートフォリオを更新できる。

**送信メッセージ構造（Flex Message）：**

```
┌──────────────────────────────┐
│ 🤖 AI株式リサーチ 2026/03/28  │  ← header
├──────────────────────────────┤
│ 【推奨レポート本文】           │  ← body（テキスト）
│  🥇 今すぐ買う  7203 トヨタ   │
│  🥈 押し目待ち  6758 ソニー   │
│  ...                          │
│                                │
│ 【保有株アラート】             │
│  🔴 損切り推奨 9984 ソフトバンク│
│  💰 合計評価損益: +83,500円    │
├──────────────────────────────┤
│      [📝 銘柄を更新する]      │  ← footer（LIFFボタン）
└──────────────────────────────┘
```

**実装コード：**

```python
LIFF_URL = os.environ["LIFF_URL"]  # 例: https://liff.line.me/YOUR_LIFF_ID

ACTION_EMOJI = {"今すぐ買う": "🟢", "押し目待ち": "🟡", "見送り": "🔴"}
RISK_STARS   = {1: "★☆☆", 2: "★★☆", 3: "★★★"}

def build_report_text(analysis: dict, portfolio_result: dict) -> str:
    """推奨レポート＋保有株アラートのテキストを生成"""
    lines = []

    # 相場警告
    if analysis["market_condition"] == "悪化":
        lines.append("⛔ 本日は全銘柄買い見送り推奨")
        lines.append(f"理由: {analysis['market_comment']}")
    else:
        lines.append(f"━━ 本日の推奨銘柄 ━━")
        for r in analysis["recommendations"]:
            emoji = ACTION_EMOJI.get(r["action"], "⚪")
            lines.append(
                f"{emoji} {r['action']} {r['code'].replace('.T','')} {r['name']}\n"
                f"   ¥{r['current_price']:,} → 目標¥{r['target_price']:,}（+{r['upside_pct']}%）\n"
                f"   {r['reason']}"
            )

    # 保有株アラート（holdingsがある場合のみ）
    if portfolio_result.get("holdings"):
        lines.append("\n━━ 保有株アラート ━━")
        for h in portfolio_result["holdings"]:
            if h["alert"]:
                lines.append(
                    f"{'🔴' if '損切り' in h['alert'] else '🟡'} 【{h['alert']}】{h['name']}\n"
                    f"   {h['unrealized_pnl_pct']:+.1f}%（{h['unrealized_pnl']:+,}円）"
                )
        pnl = portfolio_result["total_unrealized_pnl"]
        pnl_pct = portfolio_result["total_unrealized_pnl_pct"]
        lines.append(f"\n💰 合計評価損益: {pnl:+,}円（{pnl_pct:+.1f}%）")

    return "\n".join(lines)


def build_flex_message(report_text: str) -> dict:
    """レポートテキスト＋LIFFボタンをFlex Messageに組み立てる"""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d")
    return {
        "type": "flex",
        "altText": f"AI株式リサーチ {today}",
        "contents": {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#1E3A5F",
                "contents": [{
                    "type": "text",
                    "text": f"🤖 AI株式リサーチ {today}",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "md"
                }]
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [{
                    "type": "text",
                    "text": report_text,
                    "wrap": True,
                    "size": "sm"
                }]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [{
                    "type": "button",
                    "action": {
                        "type": "uri",
                        "label": "📝 銘柄を更新する",
                        "uri": LIFF_URL
                    },
                    "style": "primary",
                    "color": "#1E3A5F"
                }]
            }
        }
    }


def send_daily_report(analysis: dict, portfolio_result: dict) -> None:
    report_text = build_report_text(analysis, portfolio_result)
    flex_msg = build_flex_message(report_text)
    # LINE Push Message API で送信
    ...
```

-----

### 4-9. LIFF フォーム（`src/liff/`）★追加

LINEの内蔵ブラウザで開くWebフォーム。Azure Static Web Apps（tarimasa.comと同構成）にデプロイする。

**UIイメージ：**

```
┌─────────────────────────┐
│   📈 保有株フォーム      │
│ ─────────────────────── │
│ 操作      [追加    ▼]   │ ← <select>
│ 銘柄コード [    ]        │ ← type="tel" pattern="[0-9]{4}"
│ 株数      [    ]        │ ← type="number"
│ 取得単価  [    ]        │ ← type="number"
│                          │
│       [✅ 登録する]     │
└─────────────────────────┘
```

**バリデーション：**

- 銘柄コード：4桁数字のみ（`pattern="[0-9]{4}"`）
- 株数・取得単価：正の整数のみ（`type="number" min="1"`）
- 送信前にすべてのフィールドが有効か確認

**送信後の動作：**

- webhook_server.pyの `/portfolio` エンドポイントにPOST
- 成功するとLINEトークに自動返信（LIFFを閉じてトークに戻る）
- 失敗時はフォーム上にエラーメッセージを表示

**ファイル構成：**

```
src/liff/
├── index.html     # フォームUI（バニラHTMLでOK）
├── style.css
└── app.js         # LIFF SDK初期化 + フォーム送信処理
```

**コスト：** Azure Static Web Apps 無料枠に同居（追加費用ゼロ）

-----

### 4-10. `.github/workflows/daily_report.yml`

```yaml
name: Daily Stock AI Report

on:
  schedule:
    - cron: '30 22 * * 0-4'  # 07:30 JST（8:30発注向け）※日〜木=月〜金
    - cron: '30 02 * * 1-5'  # 11:30 JST（12:00発注向け）※月〜金
  workflow_dispatch:           # 手動実行も可能

jobs:
  run-report:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: python src/main.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          LINE_CHANNEL_ACCESS_TOKEN: ${{ secrets.LINE_CHANNEL_ACCESS_TOKEN }}
          LINE_USER_ID: ${{ secrets.LINE_USER_ID }}
          AZURE_STORAGE_CONNECTION_STRING: ${{ secrets.AZURE_STORAGE_CONNECTION_STRING }}
```

-----

## 5. 環境変数（.env.example）

```env
# Claude API
ANTHROPIC_API_KEY=sk-ant-...

# LINE Messaging API
LINE_CHANNEL_ACCESS_TOKEN=...
LINE_USER_ID=U...             # 定時レポートの送信先LINE User ID
LINE_CHANNEL_SECRET=...       # Webhook署名検証用（webhook_server.pyで使用）
LIFF_URL=https://liff.line.me/YOUR_LIFF_ID   # LIFFフォームURL ★追加

# Azure Blob Storage（portfolio.json共有用）
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=...

# 設定
DRY_RUN=false                 # trueにするとLINE送信せずターミナル出力のみ
MAX_STOCKS_TO_ANALYZE=10      # Claudeに渡す銘柄数（多いとAPIコスト増）
```

-----

## 6. requirements.txt

```
anthropic>=0.40.0
yfinance>=0.2.40
pandas_ta>=0.3.14b          # テクニカル指標計算（RSI/MACD/BB/SMAなど）
feedparser>=6.0.11
requests>=2.32.0
python-dotenv>=1.0.0
pytz>=2024.1
fastapi>=0.115.0             # Webhookサーバー
uvicorn>=0.32.0              # ASGIサーバー
line-bot-sdk>=3.14.0         # LINE SDK v3（Webhook署名検証）
azure-storage-blob>=12.23.0  # Blob Storage（portfolio.json共有）
```

-----

## 7. 実装手順（Claude Codeへの指示順序）

### Step 1: プロジェクト初期化

```
GitHubリポジトリ「stock-ai-bot」を作成し、
上記ディレクトリ構成のファイルを空ファイルで作成してください。
requirements.txtと.env.exampleも作成してください。
```

### Step 2: Blob Storage管理モジュール

```
src/portfolio_store.pyを実装してください。
azure-storage-blobを使いAzure Blob StorageのJSONを読み書きします。
AZURE_STORAGE_CONNECTION_STRINGが未設定の場合はローカルファイル
config/portfolio_local.jsonにフォールバックしてください（開発用）。
```

### Step 3: データ取得モジュール

```
src/data_fetcher.pyを実装してください。
yfinanceを使いwatchlist.jsonの全銘柄の株価・OHLCVデータ（直近90日）を取得します。
ticker.infoからPER/PBRも取得してください。
DRY_RUN=trueで動作確認できるようにしてください。
```

### Step 4: スクリーニング

```
src/screener.pyを実装してください。
pandas_taを使いARCHITECTURE.mdのスコアリングロジックに従って実装してください。
上位MAX_STOCKS_TO_ANALYZE件を返します。
```

### Step 5: ニュース取得

```
src/news_fetcher.pyを実装してください。
RSSから過去24時間のニュースを取得し、銘柄名・コードでフィルタリングします。
```

### Step 6: Claude分析

```
src/claude_analyzer.pyを実装してください。
ARCHITECTURE.mdのシステムプロンプトをそのまま使用し、
JSON出力をパースして返します。APIエラー時はリトライを3回行ってください。
```

### Step 7: 保有株トラッカー

```
src/portfolio_tracker.pyを実装してください。
portfolio_store.load_portfolio()でデータを取得し、
ARCHITECTURE.mdのアラート判定ロジックに従って計算して返します。
holdingsが空配列の場合は空のdictを返してスキップしてください。
```

### Step 8: LINE通知

```
src/line_notifier.pyを実装してください。
analysis dictとportfolio_result dictを受け取り、
ARCHITECTURE.mdのフォーマットで整形して1通のLINEにまとめて送信します。
DRY_RUN=trueの場合はprint出力のみにしてください。
```

### Step 9: オーケストレーター＋GitHub Actions

```
src/main.pyとdaily_report.ymlを実装してください。
ARCHITECTURE.mdの実行順序（Step2〜8）に従い呼び出します。
cronは07:30 JSTと11:30 JSTの2回。エラー時はLINEにエラー通知を送ります。
```

### Step 10: LIFFフォーム ★追加

```
src/liff/index.html, style.css, app.jsを作成してください。
ARCHITECTURE.mdのUIイメージとバリデーション仕様に従い実装してください。
送信先はwebhook_server.pyの /portfolio エンドポイント（POST）です。
送信成功後はliff.closeWindow()でLINEトークに戻ってください。
Azure Static Web Appsにデプロイすることを想定したシンプルな構成にしてください。
```

### Step 11: Webhookサーバー

```
src/webhook_server.pyをFastAPIで実装してください。
以下の2つのエンドポイントを実装してください。

POST /webhook  : LINE Messaging APIからのWebhookイベント受信（署名検証必須）
POST /portfolio: LIFFフォームからの保有株追加・削除リクエスト受信

LINE SDK v3（linebot.v3）を使いWebhook署名検証を必ず行ってください。
/portfolioはportfolio_store経由でBlob Storageを更新後、
LINE Push APIで「✅ 追加完了」などの結果をユーザーに返信してください。
```

### Step 12: Dockerfile

```
Dockerfileを作成してください。
webhook_server.pyをuvicornで起動するコンテナです。
ポートは8000。Azure Container Appsにデプロイすることを想定してください。
```

### Step 13: テスト

```
tests/test_dry_run.pyを作成してください。
DRY_RUN=trueで全モジュールが正常動作することを確認するテストです。
portfolio_store.pyはローカルファイルフォールバックで動作確認してください。
```

-----

## 8. GitHub Secrets / Azure / LINE LIFF 設定手順

### GitHub Secrets（Settings > Secrets and variables > Actions）

|Secret名                          |取得方法                                    |
|---------------------------------|----------------------------------------|
|`ANTHROPIC_API_KEY`              |https://console.anthropic.com           |
|`LINE_CHANNEL_ACCESS_TOKEN`      |LINE Developers Console > Messaging API |
|`LINE_CHANNEL_SECRET`            |LINE Developers Console > Basic settings|
|`LINE_USER_ID`                   |LINE Messaging API Webhookで確認           |
|`AZURE_STORAGE_CONNECTION_STRING`|Azure Portal > Storage Account > アクセスキー |
|`LIFF_URL`                       |LINE Developers Console > LIFF（下記手順で取得） |

### LIFF 登録手順

```
1. LINE Developers Console > 対象チャンネル > LIFF タブ
2. 「追加」をクリック
3. 設定値：
   - LIFFアプリ名: 保有株フォーム
   - サイズ: Tall（フォームが見やすい高さ）
   - エンドポイントURL: https://<Static Web Apps のURL>/liff/
   - Scope: profile
   - Bot リンク機能: On (Aggressive)
4. 発行された LIFF ID を確認
   → LIFF_URL = https://liff.line.me/<LIFF_ID>
```

### Azure Container Apps デプロイ手順

```bash
# 1. Azure Container Registry作成（ACR）
az acr create --name stockbotacr --resource-group rg-stock-bot --sku Basic

# 2. DockerイメージをACRにpush
az acr build --registry stockbotacr --image stock-bot-webhook:latest .

# 3. Container Apps環境作成
az containerapp env create --name stock-bot-env --resource-group rg-stock-bot --location japaneast

# 4. Container Appデプロイ
az containerapp create \
  --name stock-bot-webhook \
  --resource-group rg-stock-bot \
  --environment stock-bot-env \
  --image stockbotacr.azurecr.io/stock-bot-webhook:latest \
  --ingress external --target-port 8000 \
  --env-vars LINE_CHANNEL_ACCESS_TOKEN=... LINE_CHANNEL_SECRET=... \
             AZURE_STORAGE_CONNECTION_STRING=...

# 5. 取得したFQDN（https://stock-bot-webhook.xxx.japaneast.azurecontainerapps.io）を
#    LINE Developers Console の Webhook URL に設定
#    例: https://stock-bot-webhook.xxx.japaneast.azurecontainerapps.io/webhook
```

-----

## 9. 拡張候補（Phase 2以降）

|機能                     |技術                             |優先度|
|-----------------------|-------------------------------|---|
|kabuステーションAPIで半自動発注    |auカブコム証券API                    |★★★|
|過去推奨の的中率トラッキング         |SQLite / Azure Cosmos DB       |★★☆|
|週次パフォーマンスレポート          |GitHub Actions（毎週日曜）           |★★☆|
|Azure Functions移行（常時稼働）|Azure Functions + Timer Trigger|★☆☆|
|ポートフォリオ管理Webダッシュボード    |Next.js（tarimasa.com統合）        |★☆☆|

-----

## 10. 免責事項（READMEに必ず記載すること）

```
本ツールの出力は情報提供を目的としており、投資勧誘ではありません。
投資判断は必ず自己責任で行ってください。
AIの推奨は過去データに基づくものであり、将来の利益を保証しません。
```
