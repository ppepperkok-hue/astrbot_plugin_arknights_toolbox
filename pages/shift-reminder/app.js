/* 基建换班提醒 —— 班次时间轴。
 *
 * 设计要点（为什么这样写）：
 * 1. **三段是一条环形链**：`shift_1 → shift_2 → shift_3 → shift_1` 首尾相接、合计 24 小时。
 *    所有编辑都保持这个不变量，所以**结构上拖不出非法配置**——校验从"事后拦"变成
 *    "根本做不到"。服务端仍然是唯一裁判（前端算错了也拦得住）。
 * 2. **边界换算抽成纯函数并导出**：本机没有浏览器，只有把数学抽出来才能用 node 验证。
 * 3. `shift_N_hours` 在 `_conf_schema.json` 里是 `type: int`，`module.py` 也明确要求
 *    "整数小时数"——**所以时长必须吸附到整点小时**，半小时存不下去（见 README 说明）。
 */

const MINUTES_PER_DAY = 24 * 60;
const MIN_SHIFT_MINUTES = 60;
const HOUR = 60;

const ENDPOINT = "shift-reminder/status";
const ROSTER_ENDPOINT = "shift-reminder/roster";
const UPLOAD_ENDPOINT = "shift-reminder/upload";
const REFRESH_INTERVAL_MS = 60_000;
const COUNTDOWN_INTERVAL_MS = 1_000;

const SLOT_LABELS = ["第一班", "第二班", "第三班"];

// --- 纯换算（无 DOM，可被 node 直接调用） ----------------------------------

/** 把分钟数规整到 0..1439。跨天由取模自然处理。 */
export function wrapMinutes(minutes) {
  return ((Math.round(minutes) % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
}

/** 吸附到整点小时：`shift_N_hours` 只接受整数小时。 */
export function snapToHour(minutes) {
  return Math.round(minutes / HOUR) * HOUR;
}

export function formatHhmm(minutes) {
  const value = wrapMinutes(minutes);
  const hh = String(Math.floor(value / HOUR)).padStart(2, "0");
  const mm = String(value % HOUR).padStart(2, "0");
  return `${hh}:${mm}`;
}

export function formatDuration(minutes) {
  const hours = Math.floor(minutes / HOUR);
  const rest = minutes % HOUR;
  if (hours === 0) {
    return `${rest} 分钟`;
  }
  return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分`;
}

export function totalMinutes(segments) {
  return segments.reduce((sum, seg) => sum + seg.minutes, 0);
}

/** 相邻两段之间的分界在环形链上的位置（段 i 的结束）。 */
export function boundaryMinute(segments, index) {
  return wrapMinutes(segments[index].startMin + segments[index].minutes);
}

/**
 * 把后端给的 `status.shifts` 变成内部时间轴模型。
 *
 * 后端**必须**按配置顺序给（否则「第一班」会拿到夜班，保存时写错段）——这里只做
 * 形状转换与兜底，不替后端猜顺序。
 */
export function normalizeSegments(shifts) {
  return (Array.isArray(shifts) ? shifts : []).map((shift, index) => ({
    slot: Number.isInteger(shift.slot) ? shift.slot : index + 1,
    name: String(shift.name ?? SLOT_LABELS[index] ?? `第${index + 1}班`),
    startMin: parseHhmmSafe(shift.start),
    minutes: Number.isFinite(shift.minutes)
      ? Math.round(shift.minutes)
      : Math.round((Number(shift.hours) || 0) * HOUR),
  }));
}

function parseHhmmSafe(text) {
  const parts = String(text ?? "").split(":");
  if (parts.length !== 2) {
    return 0;
  }
  const hh = Number(parts[0]);
  const mm = Number(parts[1]);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) {
    return 0;
  }
  return wrapMinutes(hh * HOUR + mm);
}

/**
 * 拖动第 `index` 个内部分界到 `newEndMinute`。
 *
 * 语义：**相邻班的边界同步跟随**——被拖那段变长，下一段起点跟着走；再下一段的时长
 * 自动补差，因此**合计恒为 24 小时、首尾恒相接**。
 *
 * @returns 新的 segments（不修改入参）。
 */
export function dragBoundary(segments, index, newEndMinute) {
  const count = segments.length;
  if (count < 2 || index < 0 || index >= count - 1) {
    return segments.map((seg) => ({ ...seg }));
  }

  const segs = segments.map((seg) => ({ ...seg }));
  const first = segs[index];
  const middle = segs[(index + 1) % count];
  const last = segs[(index + 2) % count];

  // middle 的时长不动，所以第一段的取值空间由它和"每段至少 1 小时"共同决定。
  const middleMinutes = middle.minutes;
  const maxFirst = totalMinutes(segments) - middleMinutes - MIN_SHIFT_MINUTES;
  const minFirst = MIN_SHIFT_MINUTES;

  let length = wrapMinutes(newEndMinute - first.startMin);
  length = Math.min(Math.max(length, minFirst), Math.max(minFirst, maxFirst));
  length = snapToHour(length);
  length = Math.min(Math.max(length, minFirst), Math.max(minFirst, maxFirst));

  first.minutes = length;
  middle.startMin = wrapMinutes(first.startMin + length);
  last.startMin = wrapMinutes(middle.startMin + middleMinutes);
  last.minutes = totalMinutes(segments) - length - middleMinutes;
  return segs;
}

/** 整体平移（等于改「第一班几点开始」）：时长一个不动，只转起始时刻。 */
export function rotateSegments(segments, deltaMinutes) {
  return segments.map((seg) => ({
    ...seg,
    startMin: wrapMinutes(seg.startMin + deltaMinutes),
  }));
}

/** 内部模型 → 配置片段。段序即 `shift_1/2/3` 的段序。 */
export function segmentsToPayload(segments, leadMinutes) {
  const shift = {};
  segments.forEach((seg, index) => {
    const slot = index + 1;
    shift[`shift_${slot}_name`] = seg.name;
    shift[`shift_${slot}_start`] = formatHhmm(seg.startMin);
    shift[`shift_${slot}_hours`] = seg.minutes / HOUR;
  });
  shift.lead_minutes = leadMinutes;
  return { shift_reminder: shift };
}

/**
 * 前端自查：只为省一次往返。
 *
 * **它不是防线**——服务端校验可以绕过前端，所以这里判"不行"只是不给保存按钮添乱，
 * 判"行"也不代表一定存得下去（真结果以后端回执为准）。
 */
export function checkSegments(segments) {
  if (segments.length < 1) {
    return "至少要有 1 个班次。";
  }
  for (let index = 0; index < segments.length; index += 1) {
    const seg = segments[index];
    if (!Number.isFinite(seg.minutes) || seg.minutes <= 0) {
      return `${SLOT_LABELS[index] ?? `第 ${index + 1} 班`}的时长必须大于 0。`;
    }
    if (Math.abs(seg.minutes - snapToHour(seg.minutes)) > 1e-6) {
      return `${SLOT_LABELS[index] ?? `第 ${index + 1} 班`}的时长必须是整点小时（当前 ${formatDuration(seg.minutes)}）。`;
    }
    if (!seg.name || !String(seg.name).trim()) {
      return `${SLOT_LABELS[index] ?? `第 ${index + 1} 班`}还需要一个名称。`;
    }
    const following = segments[(index + 1) % segments.length];
    if (wrapMinutes(seg.startMin + seg.minutes) !== following.startMin) {
      return "班次之间没有首尾相接，请重新调整。";
    }
  }
  if (totalMinutes(segments) !== MINUTES_PER_DAY) {
    return `三段时长之和必须正好 24 小时（当前 ${formatDuration(totalMinutes(segments))}）——拖动分界点不会破坏这一条，改名称也不会。`;
  }
  return null;
}

/** 把段切成"在 00:00–24:00 上可见的条"，跨天的段会切成两条。 */
export function segmentsToBars(segments) {
  const bars = [];
  segments.forEach((seg, index) => {
    const start = wrapMinutes(seg.startMin);
    const end = start + seg.minutes;
    if (end <= MINUTES_PER_DAY) {
      bars.push({ index, slot: seg.slot, name: seg.name, from: start, to: end, wrapped: false });
    } else {
      bars.push({ index, slot: seg.slot, name: seg.name, from: start, to: MINUTES_PER_DAY, wrapped: false });
      bars.push({ index, slot: seg.slot, name: seg.name, from: 0, to: end - MINUTES_PER_DAY, wrapped: true });
    }
  });
  return bars;
}

// --- DOM 部分 --------------------------------------------------------------

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

// --- 页面状态 --------------------------------------------------------------

let countdownTimer = null;
let changeAtMs = null;

/** 当前编辑中的段（按配置顺序）。null 表示还没读到数据。 */
let segments = [];
/** 保存成功后用于「撤销改动」。 */
let pristine = null;
let leadMinutes = 10;

function snapshotSegments() {
  return segments.map((seg) => ({ ...seg }));
}

function restore(list) {
  segments = list.map((seg) => ({ ...seg }));
  renderTimeline();
}

// --- 时间轴渲染与交互 ------------------------------------------------------

function renderTimeline() {
  const host = byId("timeline");
  if (!host) {
    return;
  }
  host.textContent = "";

  if (segments.length === 0) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没读到班次配置。";
    host.appendChild(empty);
    renderEditors();
    return;
  }

  // 分钟刻度：每 3 小时一条，够看清方位又不糊。
  for (let minute = 0; minute < MINUTES_PER_DAY; minute += 3 * HOUR) {
    const tick = document.createElement("div");
    tick.className = "tick";
    tick.style.left = `${(minute / MINUTES_PER_DAY) * 100}%`;
    const label = document.createElement("span");
    label.textContent = formatHhmm(minute);
    tick.appendChild(label);
    host.appendChild(tick);
  }

  for (const bar of segmentsToBars(segments)) {
    const node = document.createElement("div");
    node.className = `bar bar-${bar.index % 3}`;
    node.style.left = `${(bar.from / MINUTES_PER_DAY) * 100}%`;
    node.style.width = `${((bar.to - bar.from) / MINUTES_PER_DAY) * 100}%`;

    const seg = segments[bar.index];
    const title = document.createElement("span");
    title.className = "bar-title";
    if (bar.from !== seg.startMin || !bar.wrapped) {
      title.textContent = seg.name;
    }
    node.appendChild(title);

    const meta = document.createElement("span");
    meta.className = "bar-meta";
    if (!bar.wrapped) {
      meta.textContent = `${formatHhmm(seg.startMin)}–${formatHhmm(seg.startMin + seg.minutes)} · ${formatDuration(seg.minutes)}`;
    }
    node.appendChild(meta);

    // 拖色块 = 整体平移（改第一班几点开始）
    node.addEventListener("pointerdown", (event) => beginRotate(event, node));
    host.appendChild(node);
  }

  // 内部分界手柄
  for (let index = 0; index < segments.length - 1; index += 1) {
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "handle";
    handle.style.left = `${(boundaryMinute(segments, index) / MINUTES_PER_DAY) * 100}%`;
    handle.title = "拖动调整换班时刻";
    handle.setAttribute("aria-label", `调整第 ${index + 1} 段与第 ${index + 2} 段的换班时刻`);
    handle.addEventListener("pointerdown", (event) => beginDragBoundary(event, handle, index));
    handle.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        nudgeBoundary(index, event.key === "ArrowRight" ? HOUR : -HOUR);
      }
    });
    host.appendChild(handle);
  }

  renderEditors();

  const sum = totalMinutes(segments);
  const ok = sum === MINUTES_PER_DAY;
  setText(
    "timeline-hint",
    `${segments
      .map((seg) => `${seg.name} ${formatHhmm(seg.startMin)} 起 ${formatDuration(seg.minutes)}`)
      .join("　|　")}　合计 ${formatDuration(sum)}${ok ? " ✓" : "（异常）"}`,
  );
}

function segmentsHost() {
  return byId("timeline");
}

function minuteFromClientX(clientX) {
  const host = segmentsHost();
  if (!host) {
    return 0;
  }
  const rect = host.getBoundingClientRect();
  if (rect.width <= 0) {
    return 0;
  }
  const ratio = (clientX - rect.left) / rect.width;
  return Math.min(Math.max(ratio, 0), 1) * MINUTES_PER_DAY;
}

function beginDragBoundary(event, handle, index) {
  event.preventDefault();
  handle.setPointerCapture?.(event.pointerId);
  const move = (moveEvent) => {
    const target = snapToHour(minuteFromClientX(moveEvent.clientX));
    segments = dragBoundary(segments, index, target);
    renderTimeline();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function beginRotate(event, node) {
  // 从色块拖动 = 整条轴旋转。startX 用指针位置，位移换算成分钟。
  event.preventDefault();
  const host = segmentsHost();
  if (!host) {
    return;
  }
  const rect = host.getBoundingClientRect();
  const startX = event.clientX;
  const original = snapshotSegments();
  node.setPointerCapture?.(event.pointerId);

  const move = (moveEvent) => {
    if (rect.width <= 0) {
      return;
    }
    const deltaMinutes = ((moveEvent.clientX - startX) / rect.width) * MINUTES_PER_DAY;
    segments = rotateSegments(original, deltaMinutes);
    renderTimeline();
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function nudgeBoundary(index, deltaMinutes) {
  const target = wrapMinutes(boundaryMinute(segments, index) + deltaMinutes);
  segments = dragBoundary(segments, index, target);
  renderTimeline();
}

function renderEditors() {
  const host = byId("editors");
  if (!host) {
    return;
  }
  host.textContent = "";
  if (segments.length === 0) {
    return;
  }

  segments.forEach((seg, index) => {
    const row = document.createElement("div");
    row.className = "editor";

    const dot = document.createElement("span");
    dot.className = `dot dot-${index % 3}`;
    row.appendChild(dot);

    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = seg.name;
    nameInput.setAttribute("aria-label", `${SLOT_LABELS[index] ?? `第 ${index + 1} 班`}名称`);
    nameInput.addEventListener("input", () => {
      segments[index].name = nameInput.value;
      const hint = byId("timeline-hint");
      if (hint) {
        const sum = totalMinutes(segments);
        hint.textContent = `${segments
          .map((item) => `${item.name} ${formatHhmm(item.startMin)} 起 ${formatDuration(item.minutes)}`)
          .join("　|　")}　合计 ${formatDuration(sum)}${sum === MINUTES_PER_DAY ? " ✓" : "（异常）"}`;
      }
    });
    row.appendChild(nameInput);

    const startText = document.createElement("span");
    startText.className = "mono";
    startText.textContent = `${formatHhmm(seg.startMin)} 起`;
    row.appendChild(startText);

    const duration = document.createElement("span");
    duration.className = "mono";
    duration.textContent = formatDuration(seg.minutes);
    row.appendChild(duration);

    // 触屏拖不准时的兜底：±1 小时的微调按钮（时长必须是整点，所以只能是整小时）
    if (index < segments.length - 1) {
      const minus = document.createElement("button");
      minus.type = "button";
      minus.className = "ghost small";
      minus.textContent = "−1h";
      minus.title = "这个换班时刻提前一小时";
      minus.addEventListener("click", () => nudgeBoundary(index, -HOUR));
      row.appendChild(minus);

      const plus = document.createElement("button");
      plus.type = "button";
      plus.className = "ghost small";
      plus.textContent = "+1h";
      plus.title = "这个换班时刻推迟一小时";
      plus.addEventListener("click", () => nudgeBoundary(index, HOUR));
      row.appendChild(plus);
    }

    host.appendChild(row);
  });

  renderLeadHint();
}

function renderLeadHint() {
  const hint = byId("lead-hint");
  if (!hint || segments.length === 0) {
    return;
  }
  const raw = (byId("lead-input")?.value ?? "").trim();
  // 空输入（含刚清空）沿用当前值：`Number("")` 是 0，直接拿去算会给出「提前 0 分钟」的假提示。
  const parsed = Number(raw);
  const minutes = raw !== "" && Number.isInteger(parsed) ? parsed : leadMinutes;
  const first = segments[0];
  const at = wrapMinutes(first.startMin - minutes);
  setText(
    "lead-hint",
    `${formatHhmm(first.startMin)} 的班会提前 ${minutes} 分钟，也就是在 ${formatHhmm(at)} 提醒你。`,
  );
}

// --- 保存 ------------------------------------------------------------------

async function saveShifts() {
  const button = byId("save-shifts");
  const hint = byId("save-hint");

  const problem = checkSegments(segments);
  if (problem) {
    showError(problem);
    return;
  }

  const leadRaw = (byId("lead-input")?.value ?? "").trim();
  const lead = leadRaw === "" ? leadMinutes : Number(leadRaw);
  if (!Number.isInteger(lead) || lead < 0 || lead > 180) {
    showError("提前量必须是 0~180 的整数分钟。");
    return;
  }

  const payload = segmentsToPayload(segments, lead);

  if (button) {
    button.disabled = true;
  }
  if (hint) {
    hint.textContent = "正在保存…";
  }

  try {
    const result = normalize(await window.AstrBotPluginPage.apiPost("config", payload));
    // 后端校验失败会返回错误，而且**不会**留下坏配置（它会回滚）。
    if (result && result.status === "error") {
      showError(`保存失败：${result.message || "配置不合法"}`);
      if (hint) {
        hint.textContent = "未生效";
      }
      return;
    }
    hideError();
    if (hint) {
      hint.textContent = result && result.applied === false ? "已保存，但尚未生效" : "已保存并生效";
    }
    pristine = snapshotSegments();
    leadMinutes = lead;
    await refresh();
  } catch (error) {
    console.error("保存配置失败", error);
    showError(`保存失败：${error && error.message ? error.message : error}`);
    if (hint) {
      hint.textContent = "未生效";
    }
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

// --- 状态展示（沿用既有行为） ----------------------------------------------

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
  const total = Math.floor(diff / 60_000);
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
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
  setText("upcoming", upcoming.name ? `${upcoming.name}　${upcoming.start} – ${upcoming.end}` : "—");
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

  leadMinutes = Number.isInteger(data.lead_minutes) ? data.lead_minutes : leadMinutes;
  setText("lead", `提前 ${leadMinutes} 分钟`);
  const leadInput = byId("lead-input");
  if (leadInput && document.activeElement !== leadInput) {
    leadInput.value = String(leadMinutes);
  }

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

  segments = normalizeSegments(data.shifts);
  pristine = snapshotSegments();
  renderTimeline();

  setText("status-line", `读取时间 ${String(data.now || "").replace("T", " ").slice(0, 19)}`);
}

/**
 * 房型图标：取中文名的**首字**做成徽章。
 *
 * 为什么这么做：不引任何外部资源是硬要求（离线环境 + 图片版权），而图标字体
 * 同样是外部依赖。中文房型名的首字天然就是最好的缩写——「贸」「制」「电」「宿」，
 * 一眼能认，且与名称永远一致（不会像图标表那样跟数据漂移）。
 */
function roomInitial(label) {
  const text = String(label || "").trim();
  return text ? text.slice(0, 1) : "房";
}

/** 一个干员名 → 紧凑标签块。 */
function operatorChip(name) {
  const chip = document.createElement("span");
  chip.className = "op";
  chip.textContent = name;
  return chip;
}

function renderRoster(view) {
  const host = byId("roster-detail");
  const summaryHost = byId("roster-summary");
  if (!host) {
    return;
  }
  host.textContent = "";
  if (summaryHost) {
    summaryHost.textContent = "";
  }

  if (!view || view.imported !== true) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent =
      "还没有导入排班表。在上面选一个导出的 JSON 上传就行——不导入也能正常用，" +
      "只是提醒里不会列出干员。";
    host.append(p);
    return;
  }

  // --- 顶部汇总：一眼看出「这套布局有多大」 ---------------------------------
  if (summaryHost) {
    const stats = [
      { value: view.shift_count, unit: "个班次" },
      { value: view.total_rooms, unit: "间房" },
      { value: view.total_operators, unit: "位干员" },
    ];
    if (view.skipped_total) {
      stats.push({ value: view.skipped_total, unit: "间「不动」" });
    }
    for (const stat of stats) {
      const box = document.createElement("div");
      box.className = "stat";
      const value = document.createElement("span");
      value.className = "stat-value";
      value.textContent = String(stat.value ?? 0);
      const unit = document.createElement("span");
      unit.className = "stat-unit";
      unit.textContent = stat.unit;
      box.append(value, unit);
      summaryHost.append(box);
    }

    const meta = document.createElement("p");
    meta.className = "muted roster-meta";
    const parts = [];
    if (view.source) {
      parts.push(`来源 ${view.source}`);
    }
    if (view.imported_at) {
      parts.push(`导入于 ${String(view.imported_at).replace("T", " ").slice(0, 16)}`);
    }
    meta.textContent = parts.join(" · ");
    summaryHost.append(meta);
  }

  // --- 逐班：按房型成块 -----------------------------------------------------
  (view.shifts || []).forEach((shift, shiftIndex) => {
    const block = document.createElement("section");
    block.className = `roster-shift shift-tint-${shiftIndex % 3}`;

    const head = document.createElement("div");
    head.className = "shift-head";

    const badge = document.createElement("span");
    badge.className = `shift-badge shift-badge-${shiftIndex % 3}`;
    badge.textContent = `第 ${shift.plan_index} 班`;
    head.append(badge);

    const title = document.createElement("h3");
    title.textContent = shift.plan_name || `第 ${shift.plan_index} 班`;
    head.append(title);

    const count = document.createElement("span");
    count.className = "muted shift-count";
    count.textContent = `${shift.room_count} 间房 · ${shift.operator_count} 位干员`;
    head.append(count);
    block.append(head);

    const grid = document.createElement("div");
    grid.className = "room-grid";

    (shift.groups || []).forEach((group) => {
      const card = document.createElement("div");
      card.className = "room-card";

      const cardHead = document.createElement("div");
      cardHead.className = "room-card-head";

      const icon = document.createElement("span");
      icon.className = "room-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = roomInitial(group.label);
      cardHead.append(icon);

      const label = document.createElement("span");
      label.className = "room-label";
      label.textContent = group.label;
      cardHead.append(label);

      // 「×2」这种数量徽标，直接回答「这套布局有几间贸易站」。
      const qty = document.createElement("span");
      qty.className = "room-qty";
      qty.textContent = `×${group.count}`;
      cardHead.append(qty);
      card.append(cardHead);

      const list = document.createElement("div");
      list.className = "room-list";
      (group.rooms || []).forEach((room) => {
        const row = document.createElement("div");
        row.className = "room-row";
        if (room.skipped) {
          row.classList.add("skipped");
        }

        const where = document.createElement("span");
        where.className = "room-where";
        where.textContent = room.index === null || room.index === undefined ? "—" : String(room.index);
        row.append(where);

        const ops = document.createElement("div");
        ops.className = "ops";
        const names = room.operators || [];
        if (names.length === 0) {
          const none = document.createElement("span");
          none.className = "muted";
          none.textContent = "（空）";
          ops.append(none);
        } else {
          names.forEach((name) => ops.append(operatorChip(name)));
        }
        row.append(ops);

        if (room.skipped) {
          const tag = document.createElement("span");
          tag.className = "tag";
          // 页面上必须显示这些房间并标注——藏起来用户会以为我们读漏了。
          // 提醒消息里相反：那里不渲染它们，因为那是给用户的「指令」，
          // 让用户去换一间排班表标明不要动的房就是错误信息。
          tag.textContent = "不动";
          tag.title = "排班表标明这一班这间房不要动，提醒里不会出现它";
          row.append(tag);
        }
        list.append(row);
      });
      card.append(list);
      grid.append(card);
    });

    block.append(grid);
    host.append(block);
  });
}

async function loadRoster() {
  try {
    const view = await window.AstrBotPluginPage.apiGet(ROSTER_ENDPOINT);
    renderRoster(normalize(view));
  } catch (error) {
    console.error("读取排班表失败", error);
    renderRoster(null);
  }
}

// 与后端 module.py 的 MAX_UPLOAD_BYTES 保持一致。**服务端才是权威**（真正的上限判定
// 在 `webapi.decode_schedule_upload`），这里只是先拦一道：把几十 MB 的文件读成 base64
// 既会把字符串撑大 4/3，postMessage 也搬不动那么大的东西。
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

/**
 * 把选中的文件读成 base64。
 *
 * 为什么不是 FormData：bridge 用 postMessage 往父页面传数据，而 FormData 不能被
 * 结构化克隆——浏览器会直接抛「FormData object could not be cloned.」。
 * @param {File} file
 * @returns {Promise<string>} 不含 data URL 前缀的 base64
 */
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("浏览器读取文件失败"));
    reader.onload = () => {
      const result = String(reader.result || "");
      // readAsDataURL 的产物形如 "data:application/json;base64,XXXX"，只取逗号之后。
      // 后端也容忍带前缀的输入，这里切掉纯粹是为了少传那几十个字节。
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

async function uploadRoster() {
  const input = byId("roster-file");
  const button = byId("roster-upload");
  const file = input && input.files && input.files[0];
  if (!file) {
    setText("upload-hint", "先选一个 .json 文件再点上传。");
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    setText(
      "upload-hint",
      `文件太大：上限 ${MAX_UPLOAD_BYTES / 1024 / 1024} MB，排班表通常只有几十 KB。`
    );
    return;
  }

  if (button) {
    button.disabled = true;
  }
  try {
    // 两步都说话：文件大时读取也要一会儿，不说用户会以为按钮坏了。
    setText("upload-hint", "正在读取文件…");
    const contentB64 = await readFileAsBase64(file);

    setText("upload-hint", "正在上传…");
    const result = normalize(
      await window.AstrBotPluginPage.apiPost(UPLOAD_ENDPOINT, {
        filename: file.name,
        content_b64: contentB64,
      })
    );
    if (result && result.saved) {
      setText("upload-hint", `导入成功。${result.summary || ""}`.trim());
      input.value = "";
      await loadRoster();
      await refresh();
    } else {
      const reason = (result && result.error) || "后端没有返回成功标记";
      setText("upload-hint", `导入失败：${reason}`);
    }
  } catch (error) {
    console.error("上传失败", error);
    setText("upload-hint", `上传失败：${error && error.message ? error.message : error}`);
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
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

  byId("refresh")?.addEventListener("click", refresh);
  byId("save-shifts")?.addEventListener("click", saveShifts);
  byId("reset-shifts")?.addEventListener("click", () => {
    if (pristine) {
      restore(pristine);
      setText("save-hint", "已恢复到上次读取的状态");
    }
  });
  byId("lead-input")?.addEventListener("input", renderLeadHint);
  byId("roster-upload")?.addEventListener("click", uploadRoster);

  await refresh();
  await loadRoster();

  countdownTimer = window.setInterval(renderCountdown, COUNTDOWN_INTERVAL_MS);
  window.setInterval(refresh, REFRESH_INTERVAL_MS);
}

// 只有真的在浏览器里（有 document 与 bridge）才启动；node 里跑纯函数测试时跳过。
if (typeof window !== "undefined" && typeof document !== "undefined") {
  main();
}

export { countdownTimer };
