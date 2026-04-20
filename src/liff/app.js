// app.js - LIFF SDK 初期化とフォーム送信処理

const WEBHOOK_BASE_URL = "https://YOUR-CONTAINER-APP.japaneast.azurecontainerapps.io";

let liffUserId = null;
let autoCloseTimer = null;  // 自動クローズタイマーID

/**
 * liff.init() はwindow.locationを書き換えるため、
 * mode パラメータは init() を呼ぶ前に読み取って保存する。
 * liff.state 経由のパラメータ（LINE内ブラウザでよく起きる）にも対応。
 */
const _initialMode = (() => {
  const params = new URLSearchParams(window.location.search);
  const direct = params.get("mode");
  if (direct) return direct;
  try {
    const liffState = params.get("liff.state");
    if (liffState) {
      const stateParams = new URLSearchParams(decodeURIComponent(liffState));
      return stateParams.get("mode");
    }
  } catch (_) {}
  return null;
})();

async function initLiff() {
  try {
    await liff.init({ liffId: getLiffId() });
    if (!liff.isLoggedIn()) {
      liff.login();
      return;
    }
    const profile = await liff.getProfile();
    liffUserId = profile.userId;
  } catch (err) {
    console.error("LIFF 初期化エラー:", err);
    showError("LINE 連携の初期化に失敗しました。アプリを再起動してください。");
  }
}

function getLiffId() {
  const params = new URLSearchParams(window.location.search);
  return params.get("liffId") || "YOUR_LIFF_ID";
}

/** XSS対策: HTMLエスケープ */
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function showError(msg) {
  const el = document.getElementById("errorMsg");
  el.textContent = msg;
  el.style.display = "block";
}

function hideError() {
  document.getElementById("errorMsg").style.display = "none";
}

function cancelAutoClose() {
  if (autoCloseTimer !== null) {
    clearTimeout(autoCloseTimer);
    autoCloseTimer = null;
  }
}

function showResult(message, type, action) {
  document.getElementById("portfolioForm").style.display = "none";
  const resultArea = document.getElementById("resultArea");
  const resultMsg = document.getElementById("resultMsg");
  const continueBtn = document.getElementById("continueBtn");
  const backBtn = document.getElementById("backBtn");

  resultMsg.textContent = message;
  resultMsg.className = `result-msg ${type}`;
  resultArea.style.display = "block";

  // 「続けて追加する」は追加成功時のみ表示、「戻る」は削除/エラー時
  const isAddSuccess = (type === "success" && action === "add");
  continueBtn.style.display = isAddSuccess ? "block" : "none";
  backBtn.style.display = isAddSuccess ? "none" : "block";

  // 追加成功時のみ5秒後に自動クローズ（続けて追加・戻るボタンで解除される）
  cancelAutoClose();
  if (isAddSuccess) {
    autoCloseTimer = setTimeout(() => liff.closeWindow(), 5000);
  }
}

function validateForm(action, code, shares, price) {
  if (action === "list" || action === "remove") return true;

  if (!code || !/^\d{4}$/.test(code)) {
    showError("銘柄コードは4桁の数字で入力してください。");
    return false;
  }

  if (action === "add") {
    if (!shares || parseInt(shares) < 1) {
      showError("株数は1以上の整数を入力してください。");
      return false;
    }
    if (!price || parseInt(price) < 1) {
      showError("取得単価は1以上の整数を入力してください。");
      return false;
    }
  }

  return true;
}

async function submitPortfolio(payload) {
  const accessToken = liff.getAccessToken();
  if (!accessToken) {
    throw new Error("LIFFアクセストークンが取得できません。再ログインしてください。");
  }
  const resp = await fetch(`${WEBHOOK_BASE_URL}/portfolio`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${accessToken}`,
    },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`サーバーエラー (${resp.status}): ${body}`);
  }
  return resp.json();
}

/** 削除チェックボックスリストをレンダリング（holdings/list 両フォーマット対応） */
function renderDeleteCheckboxList(holdings, fromListAction) {
  const container = document.getElementById("deleteList");
  if (holdings.length === 0) {
    container.innerHTML = '<div class="delete-empty">📦 保有株がありません</div>';
    return;
  }

  let html = '<div class="delete-label-title">削除する銘柄を選択（複数可）</div>';
  for (const h of holdings) {
    // list アクション応答は .T なし、holdings アクション応答は .T あり
    const code4 = fromListAction ? h.code : (h.code || "").replace(".T", "");
    const codeWithT = fromListAction ? (h.code + ".T") : (h.code || "");
    // holding_id: 新サーバーは uuid、旧サーバーフォールバックは "7203.T:2650" 形式
    const holdingId = (!fromListAction && h.id) ? h.id : `${codeWithT}:${h.buy_price}`;
    const price = Number(h.buy_price || 0).toLocaleString();
    const shares = Number(h.shares || 0).toLocaleString();
    const inputId = `sell-price-${esc(holdingId).replace(/[^a-zA-Z0-9]/g, "_")}`;

    html += `
      <div class="delete-item-wrap">
        <label class="delete-check-item">
          <input type="checkbox" class="delete-checkbox"
                 value="${esc(holdingId)}"
                 data-code="${esc(code4)}"
                 data-input-id="${inputId}" />
          <span class="delete-check-label">
            <span class="delete-code">${esc(code4)}</span>
            <span class="delete-name">${esc(h.name || "")}</span>
            <span class="delete-price">取得¥${price}（${shares}株）</span>
          </span>
        </label>
        <div class="delete-sell-price-wrap" id="${inputId}" style="display:none;">
          <label class="delete-sell-label">売却価格（円）</label>
          <input type="number" class="delete-sell-input" min="1" step="1"
                 placeholder="例: 2800（空欄で現在値を自動取得）"
                 inputmode="numeric" />
        </div>
      </div>`;
  }
  container.innerHTML = html;
  container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener("change", () => {
      // チェック状態に応じて売却価格入力欄を表示/非表示
      const wrapId = cb.dataset.inputId;
      const wrap = document.getElementById(wrapId);
      if (wrap) wrap.style.display = cb.checked ? "block" : "none";

      const anyChecked = container.querySelectorAll('input[type="checkbox"]:checked').length > 0;
      document.getElementById("submitBtn").disabled = !anyChecked;
    });
  });
}

/** 削除モード用: 保有株一覧をラジオリストとして表示 */
async function loadHoldingsForDelete() {
  const container = document.getElementById("deleteList");
  container.innerHTML = '<div class="delete-loading">📡 読み込み中...</div>';
  container.style.display = "block";
  document.getElementById("submitBtn").disabled = true;

  // 新API (holdings) を試み、失敗したら旧API (list) にフォールバック
  try {
    const data = await submitPortfolio({ action: "holdings" });
    renderDeleteCheckboxList(data.holdings || [], false);
    return;
  } catch (_) { /* fall through */ }

  try {
    const data = await submitPortfolio({ action: "list" });
    const holdings = (data.holdings_data && data.holdings_data.holdings) || [];
    renderDeleteCheckboxList(holdings, true);
  } catch (err) {
    container.innerHTML = `<div class="delete-error">⚠️ 読み込み失敗: ${esc(err.message)}</div>`;
  }
}

function updateFormLayout(action) {
  const addFields = document.getElementById("addFields");
  const codeGroup = document.getElementById("codeGroup");
  const deleteList = document.getElementById("deleteList");
  const submitBtn = document.getElementById("submitBtn");

  deleteList.style.display = "none";
  deleteList.innerHTML = "";

  if (action === "list") {
    addFields.style.display = "none";
    codeGroup.style.display = "none";
    submitBtn.textContent = "📋 一覧を表示";
    submitBtn.disabled = false;
  } else if (action === "remove") {
    addFields.style.display = "none";
    codeGroup.style.display = "none";
    submitBtn.textContent = "🗑️ 削除する";
    submitBtn.disabled = true;  // ラジオ選択後に有効化
    loadHoldingsForDelete();
  } else {
    addFields.style.display = "block";
    codeGroup.style.display = "block";
    submitBtn.textContent = "✅ 登録する";
    submitBtn.disabled = false;
  }
}

// 初期状態を一覧モードに設定
updateFormLayout("list");

// 操作選択時のフォーム表示切り替え
document.getElementById("action").addEventListener("change", function () {
  hideError();
  updateFormLayout(this.value);
});

// フォーム送信
document.getElementById("portfolioForm").addEventListener("submit", async function (e) {
  e.preventDefault();
  hideError();

  const action = document.getElementById("action").value;
  const code = document.getElementById("code").value.trim();
  const shares = document.getElementById("shares").value;
  const price = document.getElementById("price").value;

  if (!validateForm(action, code, shares, price)) return;

  const payload = { action };

  if (action === "remove") {
    const checked = [...document.querySelectorAll('.delete-checkbox:checked')];
    if (checked.length === 0) {
      showError("削除する銘柄を選択してください。");
      return;
    }

    const submitBtn = document.getElementById("submitBtn");
    submitBtn.disabled = true;
    submitBtn.textContent = "送信中...";

    try {
      for (const cb of checked) {
        const wrapId = cb.dataset.inputId;
        const wrap = document.getElementById(wrapId);
        const sellInput = wrap ? wrap.querySelector(".delete-sell-input") : null;
        const sellPriceVal = sellInput ? parseInt(sellInput.value) : NaN;
        const payload = {
          action: "remove",
          holding_id: cb.value,
          code: cb.dataset.code,
        };
        if (!isNaN(sellPriceVal) && sellPriceVal > 0) {
          payload.sell_price = sellPriceVal;
        }
        await submitPortfolio(payload);
      }
      showResult(`${checked.length}件を削除しました。`, "success", action);
    } catch (err) {
      showError(err.message || "送信に失敗しました。もう一度お試しください。");
      submitBtn.disabled = false;
      submitBtn.textContent = "🗑️ 削除する";
    }
    return;
  } else if (action !== "list") {
    payload.code = code;
    if (action === "add") {
      payload.shares = parseInt(shares);
      payload.price = parseInt(price);
    }
  }

  const submitBtn = document.getElementById("submitBtn");
  submitBtn.disabled = true;
  submitBtn.textContent = "送信中...";

  try {
    const data = await submitPortfolio(payload);
    if (action === "list" && data.holdings_data) {
      showHoldingsList(data.holdings_data);
    } else {
      showResult(data.message || "完了しました。", "success", action);
    }
  } catch (err) {
    showError(err.message || "送信に失敗しました。もう一度お試しください。");
    submitBtn.disabled = false;
    submitBtn.textContent = action === "list" ? "📋 一覧を表示" :
                            action === "remove" ? "🗑️ 削除する" : "✅ 登録する";
  }
});

function showHoldingsList(data) {
  document.getElementById("portfolioForm").style.display = "none";

  const totalColor = data.total_pnl >= 0 ? "profit" : "loss";
  const totalSign = data.total_pnl >= 0 ? "+" : "";

  let html = `
    <div class="list-header">
      <div class="list-title">📦 保有株一覧（${data.count}銘柄）</div>
      <div class="list-updated">🕐 取得: ${data.fetched_at}</div>
    </div>
    <div class="list-total ${totalColor}">
      合計評価損益<br>
      <span class="total-amount">${totalSign}${data.total_pnl.toLocaleString()}円</span>
      <span class="total-pct">（${totalSign}${data.total_pnl_pct}%）</span>
    </div>
  `;

  for (const h of data.holdings) {
    const isProfit = h.pnl_pct >= 0;
    const pnlClass = isProfit ? "profit" : "loss";
    const dot = isProfit ? "🟢" : "🔴";
    const sign = isProfit ? "+" : "";
    const rem = h.target_remaining_pct;
    const remText = rem !== null
      ? (rem >= 0 ? `あと${rem}%` : `超過${Math.abs(rem)}%`)
      : "";
    const rsiText = h.rsi ? `RSI ${h.rsi}` : "";

    html += `
      <div class="holding-card">
        <div class="holding-header">
          <span class="holding-code">${esc(h.code)}</span>
          <span class="holding-name">${esc(h.name)}</span>
          <span class="holding-rsi">${esc(rsiText)}</span>
        </div>
        <div class="price-block">
          <div class="price-row">
            <span class="price-label">現在値</span>
            <span class="price-main">¥${h.current_price.toLocaleString()}</span>
            <span class="price-pnl ${pnlClass}">${sign}${h.pnl_pct}% ${dot}</span>
          </div>
          <div class="price-sub muted">${h.shares}株 取得¥${h.buy_price.toLocaleString()} &nbsp;損益 <span class="${pnlClass}">${sign}${h.pnl.toLocaleString()}円</span></div>
        </div>
        <div class="order-guide">
          <div class="order-row take-profit">
            <span class="order-type">📈 利確売り指値</span>
            <span class="order-price take">¥${h.target_price.toLocaleString()}</span>
            <span class="order-note">${esc(remText)}</span>
          </div>
          <div class="order-row stop-loss">
            <span class="order-type">🛑 損切り逆指値</span>
            <span class="order-price stop">¥${h.stop_loss_price.toLocaleString()}</span>
            <span class="order-note">${h.stop_loss_pct}%</span>
          </div>
        </div>
        <div class="holding-insight">${esc(h.insight)}</div>
      </div>
    `;
  }

  document.getElementById("resultMsg").innerHTML = html;
  document.getElementById("resultMsg").className = "result-msg list";
  document.getElementById("resultArea").style.display = "block";

  // 一覧表示時は「続けて追加」非表示・「更新」「戻る」表示
  document.getElementById("continueBtn").style.display = "none";
  document.getElementById("refreshReportBtn").style.display = "block";
  document.getElementById("backBtn").style.display = "block";
}

// 戻るボタン
document.getElementById("backBtn").addEventListener("click", function () {
  cancelAutoClose();
  document.getElementById("resultArea").style.display = "none";
  document.getElementById("refreshReportBtn").style.display = "none";
  document.getElementById("portfolioForm").style.display = "block";
  const action = document.getElementById("action").value;
  updateFormLayout(action);
});

// 続けて追加するボタン
document.getElementById("continueBtn").addEventListener("click", function () {
  cancelAutoClose();
  document.getElementById("resultArea").style.display = "none";
  document.getElementById("portfolioForm").style.display = "block";
  // 追加フォームにリセット
  document.getElementById("action").value = "add";
  document.getElementById("code").value = "";
  document.getElementById("shares").value = "";
  document.getElementById("price").value = "";
  hideError();
  updateFormLayout("add");
  document.getElementById("code").focus();
});

// LINEに戻るボタン
document.getElementById("closeBtn").addEventListener("click", function () {
  liff.closeWindow();
});

// ─────────────────────────────────────────
// AIレポート更新（/refresh エンドポイント呼び出し）
// ─────────────────────────────────────────

async function triggerRefresh() {
  const accessToken = liff.getAccessToken();
  if (!accessToken) {
    showError("LIFFアクセストークンが取得できません。再ログインしてください。");
    return;
  }

  const btn = document.getElementById("refreshReportBtn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "更新リクエスト送信中...";
  }

  try {
    const resp = await fetch(`${WEBHOOK_BASE_URL}/refresh`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${accessToken}`,
      },
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`サーバーエラー (${resp.status}): ${body}`);
    }
    const data = await resp.json();
    if (btn) {
      btn.textContent = "✅ リクエスト送信済み";
    }
    alert(data.message || "更新リクエストを送信しました。");
  } catch (err) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🔄 AIレポートを更新";
    }
    showError(err.message || "更新リクエストの送信に失敗しました。");
  }
}


// mode=refresh でLIFFを開いた場合、自動的に更新を実行
async function handleRefreshMode() {
  if (_initialMode !== "refresh") return;

  // フォームを隠して更新中メッセージを表示
  document.getElementById("portfolioForm").style.display = "none";
  const resultArea = document.getElementById("resultArea");
  const resultMsg = document.getElementById("resultMsg");
  resultMsg.textContent = "🔄 AIレポートの更新リクエストを送信しています...";
  resultMsg.className = "result-msg";
  document.getElementById("continueBtn").style.display = "none";
  document.getElementById("backBtn").style.display = "none";
  resultArea.style.display = "block";

  const accessToken = liff.getAccessToken();
  if (!accessToken) {
    resultMsg.textContent = "⚠️ LIFFアクセストークンが取得できません。再ログインしてください。";
    document.getElementById("backBtn").style.display = "block";
    return;
  }

  try {
    const resp = await fetch(`${WEBHOOK_BASE_URL}/refresh`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${accessToken}`,
      },
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`サーバーエラー (${resp.status}): ${body}`);
    }
    const data = await resp.json();
    resultMsg.textContent = data.message || "✅ 更新リクエストを送信しました。完了後にLINEにレポートが届きます。";
    resultMsg.className = "result-msg success";
    // 5秒後に自動クローズ
    setTimeout(() => liff.closeWindow(), 5000);
  } catch (err) {
    resultMsg.textContent = `⚠️ 更新リクエストの送信に失敗しました: ${err.message}`;
    resultMsg.className = "result-msg error";
    document.getElementById("backBtn").style.display = "block";
  }
}

// ─────────────────────────────────────────
// バックテスト評価レポート（/backtest エンドポイント呼び出し）
// ─────────────────────────────────────────

async function handleBacktestMode() {
  if (_initialMode !== "backtest") return;

  document.getElementById("portfolioForm").style.display = "none";
  const resultArea = document.getElementById("resultArea");
  const resultMsg = document.getElementById("resultMsg");
  resultMsg.textContent = "📊 バックテスト評価レポートを生成しています...";
  resultMsg.className = "result-msg";
  document.getElementById("continueBtn").style.display = "none";
  document.getElementById("backBtn").style.display = "none";
  resultArea.style.display = "block";

  const accessToken = liff.getAccessToken();
  if (!accessToken) {
    resultMsg.textContent = "⚠️ LIFFアクセストークンが取得できません。再ログインしてください。";
    document.getElementById("backBtn").style.display = "block";
    return;
  }

  try {
    const resp = await fetch(`${WEBHOOK_BASE_URL}/backtest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${accessToken}`,
      },
    });
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`サーバーエラー (${resp.status}): ${body}`);
    }
    const data = await resp.json();
    resultMsg.textContent = data.message || "✅ リクエストを送信しました。まもなくLINEにレポートが届きます。";
    resultMsg.className = "result-msg success";
    setTimeout(() => liff.closeWindow(), 5000);
  } catch (err) {
    resultMsg.textContent = `⚠️ 送信に失敗しました: ${err.message}`;
    resultMsg.className = "result-msg error";
    document.getElementById("backBtn").style.display = "block";
  }
}

// 初期化
(async () => {
  await initLiff();
  handleRefreshMode();
  handleBacktestMode();
})();
