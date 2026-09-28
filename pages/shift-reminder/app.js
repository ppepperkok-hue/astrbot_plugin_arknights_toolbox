/* 基建换班提醒 —— 只读状态页。
 *
 * 数据全部来自后端 `GET /<plugin_name>/shift-reminder/status`；
 * 这个页面**不算任何班次**，只负责显示和倒计时。
 */

const ENDPOINT = "shift-reminder/status";
const REFRESH_INTERVAL_MS = 60_000;
const COUNTDOWN_INTERVAL_MS = 1_000;

let changeAtMs = null;
let countdownTimer = null;

function byId(id) {
  return document.getElementById(id);
}

function setText(id, text) {
  const node = byId(id);
  if (node) {
    node.textContent = text;
  }
}

/** bridge 有时把响应给成 JSON 字符串，统一成对象。 */
function normalize(payload) {
  if (typeof payload !== "string") {
    return payload || null;
  }
  try {
    return JSON.parse(payload);
  } catch (error) {
    console.error("状态响应不是合法 JSON", error);
    return null;
  }
}

function showError(message) {
  const box = byId("error");
  if (box) {
    box.textContent = message;
    box.hidden = false;
  }
}

function hideError() {
  const box = byId("error");
  if (box) {
    box.hidden = true;
  }
}

function renderCountdown() {
  if (changeAtMs === null) {
    setText("countdown", "—");
    return;
  }
  const diff = changeAtMs - Date.now();
  if (diff <= 0) {
    setText("countdown", "马上就要换班了");
    return;
  }
  const totalMinutes = Math.floor(diff / 60_000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  setText("countdown", hours > 0 ? `还剩 ${hours} 小时 ${minutes} 分` : `还剩 ${minutes} 分`);
}

function renderRecent(list) {
  const box = byId("recent");
  if (!box) {
    return;
  }
  box.textContent = "";
  if (!Array.isArray(list) || list.length === 0) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "还没有发送记录。";
    box.appendChild(empty);
    return;
  }
  list.forEach((item) => {
    const li = document.createElement("li");
    const when = String(item.at || "").replace("T", " ").slice(0, 16);
    const mark = item.ok ? "成功" : "失败";
    const detail = item.detail ? `（${item.detail}）` : "";
    li.textContent = `${when}　${item.shift || "?"}　${mark}${detail}`;
    if (!item.ok) {
      li.className = "bad";
    }
    box.appendChild(li);
  });
}

function render(payload) {
  const data = normalize(payload);
  if (!data) {
    showError("后端返回了无法解析的数据，请点刷新重试。");
    return;
  }
  hideError();

  const current = data.current || {};
  const upcoming = data.upcoming || {};

  setText("current", current.name ? `${current.name}　${current.start} – ${current.end}` : "—");

  setText(
    "upcoming",
    upcoming.name ? `${upcoming.name}　${upcoming.start} – ${upcoming.end}` : "—",
  );
  setText(
    "upcoming-change",
    upcoming.change_at ? `换班时刻 ${String(upcoming.change_at).slice(11, 16)}` : "",
  );

  changeAtMs = upcoming.change_at ? Date.parse(upcoming.change_at) : null;
  if (changeAtMs !== null && Number.isNaN(changeAtMs)) {
    changeAtMs = null;
  }
  renderCountdown();

  const binding = data.binding || {};
  setText(
    "binding",
    binding.bound ? "已绑定 —— 提醒会发到绑定的那个会话" : "未绑定 —— 请在 QQ 里发 /ak bind",
  );

  setText("lead", `提前 ${data.lead_minutes ?? "?"} 分钟`);

  const breaker = data.breaker || {};
  setText(
    "breaker",
    breaker.open
      ? `已暂停（连续失败 ${breaker.consecutive_failures} 次，成功一次即恢复）`
      : "正常",
  );

  const roster = data.roster || {};
  setText("roster", roster.imported ? "已导入" : "未导入（提醒里暂不含干员名单）");

  renderRecent(data.recent);

  setText("status-line", `读取时间 ${String(data.now || "").replace("T", " ").slice(0, 19)}`);
}

async function refresh() {
  const button = byId("refresh");
  if (button) {
    button.disabled = true;
  }
  try {
    const payload = await window.AstrBotPluginPage.apiGet(ENDPOINT, { limit: 5 });
    render(payload);
  } catch (error) {
    console.error("读取状态失败", error);
    showError(`读取失败：${error && error.message ? error.message : error}`);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

async function main() {
  try {
    await window.AstrBotPluginPage.ready();
  } catch (error) {
    console.error("bridge 初始化失败", error);
    showError("无法与 AstrBot 面板通信，请刷新页面重试。");
    return;
  }

  const button = byId("refresh");
  if (button) {
    button.addEventListener("click", refresh);
  }

  await refresh();

  countdownTimer = window.setInterval(renderCountdown, COUNTDOWN_INTERVAL_MS);
  window.setInterval(refresh, REFRESH_INTERVAL_MS);
}

main();
