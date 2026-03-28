// app.js - LIFF SDK 初期化とフォーム送信処理

// webhook_server の /portfolio エンドポイント URL
// Azure Container Apps デプロイ後に実際の URL に変更してください
const WEBHOOK_BASE_URL = "https://YOUR-CONTAINER-APP.japaneast.azurecontainerapps.io";

let liffUserId = null;

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
  // index.html の URL パラメータ liffId から取得するか、ハードコード
  const params = new URLSearchParams(window.location.search);
  return params.get("liffId") || "YOUR_LIFF_ID";
}

function showError(msg) {
  const el = document.getElementById("errorMsg");
  el.textContent = msg;
  el.style.display = "block";
}

function hideError() {
  document.getElementById("errorMsg").style.display = "none";
}

function validateForm(action, code, shares, price) {
  if (action === "list") return true;

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
  const resp = await fetch(`${WEBHOOK_BASE_URL}/portfolio`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Line-User-Id": liffUserId || "",
    },
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`サーバーエラー (${resp.status}): ${body}`);
  }
  return resp.json();
}

// フォームの表示切り替え
document.getElementById("action").addEventListener("change", function () {
  const addFields = document.getElementById("addFields");
  const codeGroup = document.getElementById("codeGroup");
  const submitBtn = document.getElementById("submitBtn");

  if (this.value === "list") {
    addFields.style.display = "none";
    codeGroup.style.display = "none";
    submitBtn.textContent = "📋 一覧を表示";
  } else if (this.value === "remove") {
    addFields.style.display = "none";
    codeGroup.style.display = "block";
    submitBtn.textContent = "🗑️ 削除する";
  } else {
    addFields.style.display = "block";
    codeGroup.style.display = "block";
    submitBtn.textContent = "✅ 登録する";
  }
});

document.getElementById("portfolioForm").addEventListener("submit", async function (e) {
  e.preventDefault();
  hideError();

  const action = document.getElementById("action").value;
  const code = document.getElementById("code").value.trim();
  const shares = document.getElementById("shares").value;
  const price = document.getElementById("price").value;

  if (!validateForm(action, code, shares, price)) return;

  const payload = { action, user_id: liffUserId };
  if (action !== "list") {
    payload.code = code;
  }
  if (action === "add") {
    payload.shares = parseInt(shares);
    payload.price = parseInt(price);
  }

  const submitBtn = document.getElementById("submitBtn");
  submitBtn.disabled = true;
  submitBtn.textContent = "送信中...";

  try {
    await submitPortfolio(payload);
    document.getElementById("portfolioForm").style.display = "none";
    document.getElementById("successMsg").style.display = "block";
    // 2 秒後に LIFF を閉じて LINE トークに戻る
    setTimeout(() => {
      liff.closeWindow();
    }, 2000);
  } catch (err) {
    showError(err.message || "送信に失敗しました。もう一度お試しください。");
    submitBtn.disabled = false;
    submitBtn.textContent = "✅ 登録する";
  }
});

// 初期化
initLiff();
