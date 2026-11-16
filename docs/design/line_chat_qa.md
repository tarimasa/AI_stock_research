# LINE 対話型 銘柄Q&A 機能 設計書

> ステータス: 設計のみ（未実装・設定変更なし）
> 作成日: 2026-11-16
> 対象: `src/webhook_server.py` を拡張し、LINE の自然言語での質問に、API データと既存の判定ロジックに基づいて答える

-----

## 1. 目的とゴール

### 現状
- 毎朝・昼のレポートが LINE に **一方的に Push** されるだけ。
- `/webhook` は既に稼働中だが、`追加` `削除` `一覧` `更新` `ヘルプ` の**定型コマンドだけ**を受け付ける。
  それ以外の入力には「コマンドを認識できませんでした」を返す（`webhook_server.py:322`）。

### ゴール
```
[ユーザー] キオクシアの株価は？

[Bot]
📊 キオクシアHD（285A）
現在値 ¥X,XXX（前日比 -2.3%）  11/16 10:42時点

■ データ
・RSI(5) 42 / RSI(14) 48：中立
・出来高 0.8倍（20日平均比）：薄商い
・DVS -12：売り越し優勢
・25日線比 +6.1%／52週レンジの78%地点
・Stage1スコア 10点（基準60点に届かず）
・決算発表まで 4日
・地合い：注意（VIX 24、S&P500 -1.2%）

■ 考察
売り越し（DVS≤-10）が出ており、当Botの短期ルールでは「短期推奨不可」の条件に当たります。
決算直前で値動きが荒れやすく、エントリーの根拠になるシグナルも出ていません。

■ 結論：🔴 買いは見送り推奨
（再検討の目安：DVSがプラスに戻り、RSI(5)が20以下に下がったとき）

※情報提供であり投資勧誘ではありません
```

### 要件
| # | 要件 |
|---|------|
| R1 | 入力は**自然言語**。銘柄コードに限らない（社名・略称・愛称・ひらがな表記など） |
| R2 | 回答の数値は **API で取った実データ**だけを使う（LLM に数値を作らせない） |
| R3 | 「買い／見送り」の判断は **レポートと同じロジック**（Stage1スコア・DVS・RR比・マクロ）で決める。LLM は説明役 |
| R4 | 既存コマンド（追加・削除など）は今と同じように動き続ける |
| R5 | LINE の無料メッセージ枠をできるだけ使わない（Push ではなく Reply を優先） |
| R6 | 本人以外からの問い合わせには答えない（API コストの保護） |

-----

## 2. 全体アーキテクチャ

```
LINE アプリ
   │ 「キオクシアの株価は？」
   ▼
LINE Platform ──POST /webhook──▶ webhook_server.py (Azure Container Apps)
                                    │ ① 署名検証（既存）
                                    │ ② user_id が LINE_USER_ID か確認 [新規]
                                    │ ③ parse_command() 既存コマンド？
                                    │      ├─ Yes → execute_command()（既存のまま）
                                    │      └─ No  → chat_handler へ [新規]
                                    │ ④ ローディング表示を開始し、すぐ 200 を返す
                                    ▼
                         ┌──── chat_handler.py（別スレッド） ────┐
                         │ A. 意図・銘柄の解決  intent_router     │
                         │ B. データ収集       stock_snapshot     │──▶ yfinance / J-Quants / RSS
                         │ C. 判定（ルール）    stock_judge        │──▶ screener / price_calculator /
                         │                                         │    macro_preprocessor（既存）
                         │ D. 文章化（LLM）     answer_composer    │──▶ Claude API
                         └─────────────────────────────────────────┘
                                    │
                                    ▼
                         Reply API（トークン有効時）／Push API（期限切れ時のみ）
```

**設計の原則：判断はルール、説明は LLM**
LLM に「買いか？」を自由に判断させると、毎朝のレポートと食い違ったり、同じ質問で毎回答えが変わったりする。
判定（C）は既存の Python ロジックで決め、LLM（D）は「その判定と数値を自然な日本語で説明する」ことだけを担当する。

-----

## 3. 処理フロー詳細

### 3.1 Webhook の受け方（非同期化）

今の `handle_text_message` は処理が終わってから返信する作りになっている。
Q&A はデータ取得と LLM 呼び出しで **5〜20 秒**かかるため、次のように変える。

```
handle_text_message(event):
    if event.source.user_id != LINE_USER_ID:           # R6
        return                                          # 無視（返信しない）

    cmd = parse_command(text)
    if cmd is not None:
        今と同じ処理（即時 Reply）
        return

    if not chat_rate_limiter.allow(user_id):            # 連投対策
        reply("少し時間をおいてから送ってください")
        return

    start_loading_animation(user_id, seconds=30)        # LINE「入力中…」表示
    received_at = now()
    threading.Thread(target=chat_handler.handle,
                     args=(text, reply_token, user_id, received_at)).start()
```

- **ローディング表示**: `POST https://api.line.me/v2/bot/chat/loading/start`（`chatId`, `loadingSeconds` 5〜60）。1対1トークで使える。無料。
- **返信方法**:
  - Reply トークンは**受信から約1分**しか使えない。`received_at` から 50 秒以内に答えが出たら Reply（**メッセージ数にカウントされない**）。
  - 50 秒を超えたら Push に切り替える（無料プランは月200通まで）。
- `line_notifier.reply_message()` が既にあるので、それを使う。

### 3.2 A. 意図と銘柄の解決（`intent_router.py`）

#### 意図の種類（Phase 1）
| intent | 例 | 主な処理 |
|--------|----|---------|
| `stock_quote` | 「キオクシアの株価は？」「トヨタどう？」「7203」 | 1銘柄スナップショット＋判定 |
| `stock_buy_judgement` | 「ソニー買い？」「任天堂今入っていい？」 | 同上（買い判定を強調） |
| `holding_advice` | 「持ってる三菱重工どうする？」 | 保有株の出口判定（`_generate_insight`） |
| `today_picks` | 「今日のおすすめは？」 | 当日レポート結果を返す（再分析しない） |
| `market` | 「今日の地合いは？」 | マクロ判定のみ |
| `compare` | 「トヨタとホンダどっちがいい？」 | 2銘柄分のスナップショット（Phase 2） |
| `unknown` | 雑談・範囲外 | 使い方の案内 |

#### 銘柄の解決（段階的に。LLM にコードを作らせない）
```
resolve_stock(text) -> Resolution
  1. コード直接指定:   正規表現 [0-9][0-9A-Z]{2}[0-9A-Z]（例 7203, 285A）
  2. 別名辞書:         config/stock_aliases.json
                       {"キオクシア": "285A", "ソフバン": "9984", "トヨタ": "7203", ...}
  3. マスタ照合:       master_manager.get_master() の社名と比べる
                       - 正規化: NFKC（全角/半角統一）→ ひらがなをカタカナに → 小文字化
                                 →「ホールディングス」「HD」「株式会社」「グループ」を除く
                       - 完全一致 → 前方一致 → 部分一致 → あいまい一致（difflib, 類似度0.8以上）
  4. LLM による補助:   3 で候補が複数／0件のときだけ、
                       「質問文」と「マスタから選んだ候補 上位10件（コード・社名）」を Claude に渡し、
                       候補の中から1つ選ばせる（候補にないコードは使わない）
  5. 決めきれない:     「どちらですか？ 1) 三菱重工(7011) 2) 三菱電機(6503)」と聞き返す
                       （LINE のクイックリプライボタンで選べるようにする）
```

意図の判定と銘柄名の取り出しは、**Claude の tool use（構造化出力）を 1 回だけ**呼んで同時に行う。
```json
{ "intent": "stock_quote", "stock_mentions": ["キオクシア"], "holding": false }
```
LLM が返すのは**「文中に出てきた銘柄名」だけ**で、コードへの変換は手順 1〜5 の決まった処理が行う。

> ⚠️ **既存バグ（実装前に直す必要あり）**
> `master_manager.get_master()` は `code4.isdigit()` でないコードを読み飛ばしている（`master_manager.py:93`）。
> **キオクシア（285A）など、2024年以降に増えた英字入りコードの銘柄はマスタに入らない**ため、例の質問に答えられない。
> 同じ前提が `parse_command`（`.T` を付けるだけなので問題なし）と `PortfolioRequest.code`（`^\d{4}$`）にもある。
> → `[0-9][0-9A-Z]{2}[0-9A-Z]` を許可するよう直す。

### 3.3 B. データ収集（`stock_snapshot.py`）

既存の関数を組み合わせ、1銘柄分のデータを1つの dict にまとめる。**新しいデータソースは増やさない。**

| 項目 | 取得元（既存） | 補足 |
|------|----------------|------|
| 現在値・前日比・PER・PBR・配当利回り・52週高値/安値 | `data_fetcher.fetch_stock_data_with_df()` | yfinance。場中は約20分遅れ |
| RSI5 / RSI14 / SMA25 / breakout_5d / DVS / 出来高倍率(20日) / 52週位置 / ATR14 | **`screener` の `_calc_one` 内の計算を切り出す** | → `screener.calc_technicals_from_hist(hist, close, volume)` として公開関数化。今のスクリーナーと**同じ式**を使うことで「レポートと同じ基準」になる |
| 日足履歴 | `data_fetcher.load_bulk_history()` → J-Quants → yfinance | コンテナに bulk CSV が無い場合は API に自動で切り替わる |
| Stage1 スコア・シグナル | `screener._calc_stage1_score()` | 基準 `STAGE1_MIN_SCORE`(=60) と比べる |
| 買値・利確・損切り候補 | `price_calculator.calc_price_candidates()` | ATR×1.5 指値・SL-5%・TP+7.5〜10% |
| 地合い | `data_fetcher.fetch_market_data()` → `macro_preprocessor.preprocess_macro()` | **15分キャッシュ**（質問ごとに取りに行かない） |
| 決算日 | `earnings_signal.get_upcoming_earnings()` | 既にプロセス内キャッシュあり |
| 業種 | `master_manager.get_sector_info()` | |
| 保有状況 | `portfolio_store.load_portfolio()` | 保有中なら取得単価・損益を付ける |
| 今日のレポートで推奨されたか | **新規** `latest_analysis.json`（Blob） | 下記 3.6 |
| 直近ニュース見出し（最大3件） | `news_fetcher.fetch_news_for_stock()` | 失敗しても続行 |

データ時点（`as_of`）を必ず持たせ、回答に「◯時◯分時点」と書く。
取得に失敗した項目は `None` にして、LLM には「不明」と伝える（推測で埋めさせない）。

### 3.4 C. 判定ロジック（`stock_judge.py`）— ルールで決める

毎朝のレポートと同じ考え方で、上から順に当てはめる。

```
judge(snapshot, macro, holding) -> Verdict
  ── 保有中の場合 ──
  holding があれば webhook_server._generate_insight() を流用（損切り/利確/RSI過熱/MACD）
     → verdict = "利確検討" | "損切り検討" | "保有継続" | "様子見"

  ── 未保有の場合（買い判定） ──
  1. dvs ≤ -10                         → 🔴 見送り   理由「売り越し（短期推奨不可ルール）」
  2. macro.condition == "悪化"          → 🔴 見送り   理由「地合い悪化」
  3. 決算まで ≤ 3営業日                  → 🟡 見送り寄り 理由「決算またぎリスク」（overnight イベント除外の考え方）
  4. stage1_score < STAGE1_MIN_SCORE    → 🔴 見送り   理由「短期シグナル不足（スコア xx/60）」
  5. price_calculator の RR比 < 1.5      → 🔴 見送り   理由「リスクに見合う値幅が取れない」
     （claude_analyzer.validate_recommendation と同じ基準）
  6. vol_ratio ≥ 2.0 または breakout+dvs正+rsi5≤30
                                         → 🟢 買い候補（今すぐ買う：buy_now 指値）
  7. それ以外（スコアは基準以上）          → 🟡 押し目待ち（buy_dip 指値）

  Verdict = {
    "action": "今すぐ買う|押し目待ち|見送り|利確検討|損切り検討|保有継続",
    "reasons": [...],          # 当てはまったルール（日本語・数値入り）
    "prices": {...} | None,    # 買い候補のときだけ buy/tp/sl
    "recheck_hint": "...",     # 見送り時「どうなったら再検討か」
    "in_today_report": bool,
  }
```

- 閾値は**新しく作らず**、`screener` / `price_calculator` / `claude_analyzer` の定数を import して使う。今後レポート側の閾値を変えると Q&A も自動で揃う。
- ルールの並び順は `claude_analyzer.SYSTEM_PROMPT` の「シグナル優先度」と同じにする。

### 3.5 D. 回答文の作成（`answer_composer.py`）

Claude に渡すのは「事実（snapshot）」「判定（verdict）」「質問文」だけ。

**システムプロンプト（要旨）**
```
あなたは日本株リサーチBotの解説役です。
- 渡された JSON の数値だけを使う。数値を計算・推測・追加しない。値が null の項目は「不明」と書く。
- 結論（action）は verdict.action をそのまま使う。変えてはいけない。
- 構成：「■ データ」箇条書き5〜8行 →「■ 考察」2〜3文 →「■ 結論」1行。
- 指標には短い意味を添える（例「RSI(5) 18：売られすぎ」）。
- 全体 600 文字以内。絵文字は見出しと結論のみ。
- 最後に「※情報提供であり投資勧誘ではありません」。
```

- モデル: `claude_analyzer.MODEL`（現在 haiku）と同じものを使う。temperature 0（`_EXTRA_BODY` を流用）。
- システムプロンプトにはプロンプトキャッシュを付ける。
- **答え合わせ（後処理）**: 回答文の中の「¥数字」「数字%」を抜き出して snapshot にある値か確かめる。
  ない数字があれば、LLM を使わない**テンプレート文**（データ・判定理由を並べただけのもの）で返す。
- LLM が失敗・タイムアウトしたときも同じテンプレート文で返す（Q&A が止まらないように）。

### 3.6 当日レポートとの連携

「今日のおすすめは？」で**再分析しない**ため、また「この銘柄は今朝推奨されたか」を答えるために、
`report.run_report()` / `run_noon_report()` の最後で `analysis` を Blob に保存する（`stock-bot/latest_analysis.json`、`portfolio_store` と同じ仕組み）。

```json
{ "session": "morning", "generated_at": "2026-11-16T08:05:00+09:00",
  "market_condition": "注意", "recommendations": [ ... ] }
```

-----

## 4. 新規・変更ファイル一覧

| ファイル | 種類 | 内容 |
|----------|------|------|
| `src/chat/__init__.py` | 新規 | |
| `src/chat/handler.py` | 新規 | 全体のまとめ役。A→B→C→D、Reply/Push の切り替え、エラー時の返信 |
| `src/chat/intent_router.py` | 新規 | 意図判定（Claude tool use）＋ `resolve_stock()` |
| `src/chat/stock_snapshot.py` | 新規 | 3.3 のデータ収集。地合いの15分キャッシュ |
| `src/chat/stock_judge.py` | 新規 | 3.4 の判定ルール |
| `src/chat/answer_composer.py` | 新規 | 3.5 の LLM 文章化・数値チェック・テンプレート文 |
| `config/stock_aliases.json` | 新規 | 略称・愛称の辞書 |
| `src/webhook_server.py` | 変更 | 本人確認、コマンド以外を chat へ、ローディング表示、別スレッド化。`_generate_insight` を共通モジュールへ移す |
| `src/screener.py` | 変更 | `_calc_one` の計算部分を `calc_technicals_from_hist()` として切り出す（スクリーナー自体の動きは同じ） |
| `src/master_manager.py` | 変更 | 英字入りコード（285A など）を読み込めるようにする |
| `src/line_notifier.py` | 変更 | `start_loading()`、クイックリプライ付き Reply を追加 |
| `src/report.py` | 変更 | `latest_analysis.json` の保存 |
| `tests/test_chat_*.py` | 新規 | 下記 6 章 |

**インフラ・環境変数・LINE Developers の設定は変更不要。**
既存の Container App・Webhook URL・`ANTHROPIC_API_KEY`・`LINE_*`・`JQUANTS_API_KEY`・`AZURE_STORAGE_CONNECTION_STRING` をそのまま使う。
任意で追加できる環境変数（未設定でも動く既定値あり）:
`CHAT_ENABLED`（既定 true。問題時に止めるスイッチ）、`CHAT_RATE_LIMIT_PER_MIN`（既定 6）。

-----

## 5. 運用上の考慮点

### 5.1 コスト（1質問あたりの目安）
| 項目 | 回数 | 備考 |
|------|------|------|
| Claude（意図判定） | 1回（入力 約500 / 出力 約50 トークン） | |
| Claude（銘柄の補助解決） | 0〜1回 | 名前が辞書・マスタで決まらないときだけ |
| Claude（回答文） | 1回（入力 約1,500 / 出力 約400 トークン） | |
| yfinance / J-Quants | 2〜4回 | 地合いはキャッシュ |
| LINE | Reply 1通（無料枠を使わない） | 50秒超のときだけ Push |

haiku なら 1質問 1円未満の見込み。1日数十回なら月数百円程度。

### 5.2 応答時間
- 目標: 10 秒以内（ローディング表示があるので体感は待てる）
- データ取得（現在値・日足・ニュース）は `ThreadPoolExecutor` で同時に取りに行く。
- Container Apps が**ゼロ台まで縮小する設定**の場合、最初の質問は起動待ちで遅くなる。気になる場合は最小レプリカ 1（有料。今回は設定変更しない）。

### 5.3 安全面
- 本人以外の `user_id` には返信しない（グループやほかの友だち追加者から API を使われないため）。
- 質問文は LLM の**ユーザーメッセージ**としてだけ渡し、システムプロンプトには入れない。
  LLM には道具（ツール）を持たせず、保有株の追加・削除など**書き込み操作は Q&A からは行わない**（それは既存コマンドの役目）。
- 回答の最後に免責文を必ず付ける（ARCHITECTURE.md 10章）。

### 5.4 データの限界（回答で正直に伝える）
- 場中の株価は約20分遅れ。テクニカル指標は前日終値ベース（場中は「前日終値時点」と書く）。
- Stage1 ルールは**短期（1〜3日）売買用**。「長期で持つならどう？」には「当Botの判定は短期向け」と添える。

-----

## 6. テスト計画

| テスト | 内容 |
|--------|------|
| `test_chat_resolver.py` | 「キオクシア」「きおくしあ」「ｷｵｸｼｱ」「285A」→ 285A、「トヨタ」→ 7203、「三菱」→ 聞き返し、存在しない名前 → 見つからない |
| `test_chat_judge.py` | DVS≤-10 → 見送り、スコア不足 → 見送り、出来高2倍＋基準以上 → 今すぐ買う、RR<1.5 → 見送り、保有中 → 出口判定 |
| `test_chat_composer.py` | snapshot にない数字を含む LLM 回答 → テンプレート文に切り替わる／LLM 例外 → テンプレート文 |
| `test_chat_webhook.py` | 既存コマンドは今どおり動く／本人以外は無視／50秒超で Push に切り替わる |
| `test_dry_run.py` 拡張 | `DRY_RUN=true` で外部 API なしで最後まで流れる |
| 既存 `test_determinism.py` | `calc_technicals_from_hist` 切り出し後も Stage1 の結果が変わらないこと |

-----

## 7. 段階的な実装計画

| Phase | 内容 | 目安 |
|-------|------|------|
| 0 | 英字コードのマスタ対応、`calc_technicals_from_hist` 切り出し（動きは変えない） | 0.5日 |
| 1 | `stock_quote` / `stock_buy_judgement`：銘柄解決（辞書＋マスタ）→ snapshot → judge → テンプレート文で返信（**LLM なしで動く最小版**） | 1日 |
| 2 | 意図判定・銘柄補助・回答文に Claude を使う。数値チェック。ローディング表示 | 1日 |
| 3 | `holding_advice` / `today_picks` / `market`、`latest_analysis.json` | 1日 |
| 4 | `compare`、クイックリプライ、会話の続き（「じゃあソニーは？」を直前の文脈で解釈。user_id ごとに直近3往復を10分保持） | 任意 |

Phase 1 だけでも「キオクシアの株価は？ → データ＋判定」が LLM 費用なしで動くので、先に出して挙動を確かめられる。

-----

## 8. 決めておきたいこと（オーナー確認事項）

1. **判定の時間軸**: 既存ルール（1〜3日の短期）に合わせる前提で設計した。中長期の視点（PER・PBR・業績）も結論に入れたいか？
2. **Push への切り替え**: 50秒を超えたら Push（無料枠を消費）でよいか。それとも「時間切れ」とだけ Reply するか。
3. **ニュース**: 回答に入れるか（入れると1〜3秒遅くなる）。
