const panels = {
  ready: document.getElementById("ready-panel"),
  prepared: document.getElementById("prepared-panel"),
  task: document.getElementById("task-panel"),
  paused: document.getElementById("paused-panel"),
  frozen: document.getElementById("frozen-panel"),
  recorded: document.getElementById("recorded-panel"),
  complete: document.getElementById("complete-panel"),
};

const heading = document.getElementById("session-heading");
const progress = document.getElementById("progress-count");
const studyLabel = document.getElementById("study-label");
const participantId = document.getElementById("participant-id");
const protocolSummary = document.getElementById("protocol-summary");
const timer = document.querySelector(".timer");
const timerValue = document.getElementById("timer-value");
const notice = document.getElementById("notice");
const error = document.getElementById("error");
const startConfirmation = document.getElementById("start-confirmation");
const startButton = document.getElementById("start-button");
const attestation = document.getElementById("attestation-select");
const finishButton = document.getElementById("finish-button");
const frozenAttestation = document.getElementById("frozen-attestation-select");
const frozenFinishButton = document.getElementById("frozen-finish-button");
let current = null;
let receivedAt = Date.now();
let activePanel = null;
let focusedTaskOrder = null;
let deadlineRefreshPending = false;

function setPanel(name) {
  const changed =
    activePanel !== name ||
    (name === "task" && focusedTaskOrder !== current?.order);
  Object.entries(panels).forEach(([key, panel]) => {
    panel.hidden = key !== name;
  });
  activePanel = name;
  if (changed) {
    const panel = panels[name];
    const target =
      name === "task"
        ? document.getElementById("task-subject")
        : panel.querySelector("h2");
    if (target) {
      target.setAttribute("tabindex", "-1");
      target.focus();
    }
    if (name === "task") focusedTaskOrder = current?.order ?? null;
  }
}

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function modeLabel(arm) {
  return arm === "manual" ? "手工排查" : "GameForge 辅助";
}

function formatTime(ns) {
  const seconds = Math.max(0, Math.ceil(ns / 1_000_000_000));
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function activeRemaining() {
  if (!current?.timer) return 480_000_000_000;
  const elapsedSinceFetch = current.timer.running
    ? (Date.now() - receivedAt) * 1_000_000
    : 0;
  return current.timer.remaining_ns - elapsedSinceFetch;
}

function updateTimer() {
  const remaining = activeRemaining();
  timerValue.textContent = formatTime(remaining);
  timer.classList.toggle("is-expired", remaining <= 0);
  if (
    remaining <= 0 &&
    current?.phase === "running" &&
    !deadlineRefreshPending
  ) {
    deadlineRefreshPending = true;
    refreshCurrent();
  }
}

function renderAssistance(value) {
  const panel = document.getElementById("assistance-panel");
  document.getElementById("finding-message").textContent = "";
  document.getElementById("finding-repro").textContent = "";
  document.getElementById("agent-patch").textContent = "";
  document.getElementById("assistance-status").textContent = "";
  panel.hidden = !value;
  if (!value) return;
  document.getElementById("finding-message").textContent =
    value.finding?.message ?? "未提供说明";
  document.getElementById("finding-repro").textContent = JSON.stringify(
    value.finding?.minimal_repro ?? {},
    null,
    2,
  );
  document.getElementById("agent-patch").textContent = JSON.stringify(
    value.agent_patch,
    null,
    2,
  );
  document.getElementById("assistance-status").textContent =
    `disposition=${value.disposition} · verified=${String(value.passed_verification)}`;
}

function resetTransientContent() {
  showNotice("");
  showError("");
  document.getElementById("task-subject").textContent = "";
  document.getElementById("task-mode").textContent = "";
  document.getElementById("work-path").textContent = "";
  document.getElementById("changed-paths").replaceChildren();
  document.getElementById("prepared-mode").textContent = "";
  document.getElementById("prepared-order").textContent = "";
  document.getElementById("prepared-guidance").textContent = "";
  document.getElementById("frozen-label").textContent = "";
  document.getElementById("frozen-heading").textContent = "";
  document.getElementById("frozen-guidance").textContent = "";
  startConfirmation.checked = false;
  startButton.disabled = true;
  renderAssistance(null);
  document.getElementById("syntax-output").textContent = "";
  document.getElementById("syntax-output").hidden = true;
  document.getElementById("recorded-protocol").textContent = "";
  document.getElementById("recorded-protocol").hidden = true;
  document.getElementById("complete-protocol").textContent = "";
  document.getElementById("complete-protocol").hidden = true;
  attestation.value = "";
  attestation.disabled = false;
  finishButton.disabled = true;
  frozenAttestation.value = "";
  frozenAttestation.disabled = false;
  frozenFinishButton.disabled = true;
}

function renderProtocolStatus(view, elementId) {
  const output = document.getElementById(elementId);
  if (view.protocol_status === "failure") {
    output.textContent =
      "协议失败：至少一场污染声明失败或无法确认。正确性结果仍保持隐藏。";
    output.hidden = false;
  }
}

function renderTask(view) {
  document.getElementById("task-mode").textContent = modeLabel(view.arm);
  document.getElementById("task-subject").textContent = view.task.subject;
  document.getElementById("work-path").textContent = view.task.work_path;
  const paths = document.getElementById("changed-paths");
  paths.replaceChildren(
    ...view.task.changed_paths.map((path) => {
      const item = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = path;
      item.append(code);
      return item;
    }),
  );
  renderAssistance(view.assistance);
  finishButton.disabled = !attestation.value;
}

function render(view) {
  resetTransientContent();
  current = view;
  receivedAt = Date.now();
  deadlineRefreshPending = view.phase !== "running";
  studyLabel.textContent = view.study_label;
  participantId.textContent = view.participant_id;
  protocolSummary.textContent = view.protocol_summary;
  progress.textContent = `${view.completed} / ${view.total}`;
  timer.hidden = ["recorded", "complete"].includes(view.phase);

  if (view.phase === "ready") {
    heading.textContent = "尚未准备第一场";
    setPanel("ready");
  } else if (view.phase === "recorded") {
    heading.textContent = `第 ${view.completed} 场已记录`;
    renderProtocolStatus(view, "recorded-protocol");
    setPanel("recorded");
  } else if (view.phase === "complete") {
    heading.textContent = "全部八场已记录";
    progress.textContent = `${view.total} / ${view.total}`;
    renderProtocolStatus(view, "complete-protocol");
    setPanel("complete");
  } else if (view.phase === "frozen") {
    heading.textContent = `第 ${view.order} 场 · 提交已冻结`;
    document.getElementById("frozen-label").textContent = view.timer.timed_out
      ? "ACTIVE CAP REACHED"
      : "SUBMISSION FROZEN";
    document.getElementById("frozen-heading").textContent = view.timer.timed_out
      ? "08:00 已到，提交已冻结"
      : "本场提交已冻结，记录尚未完成";
    document.getElementById("frozen-guidance").textContent = view.timer
      .timed_out
      ? "编辑器后续变化不会计入结果。任务内容与辅助信息已经移除，但仍需完成污染声明。"
      : "提交内容已固定。任务内容与辅助信息已经移除，请保持原污染声明并重试记录。";
    setPanel("frozen");
  } else if (view.phase === "paused") {
    heading.textContent = `第 ${view.order} 场 · 已暂停`;
    setPanel("paused");
  } else if (view.phase === "prepared") {
    heading.textContent = `第 ${view.order} 场等待开始`;
    document.getElementById("prepared-mode").textContent = modeLabel(view.arm);
    document.getElementById("prepared-order").textContent =
      `第 ${view.order} / ${view.total} 场`;
    document.getElementById("prepared-guidance").textContent =
      view.arm === "manual"
        ? "本场允许使用隔离编辑器和原生语法检查。"
        : "本场允许使用隔离编辑器、原生语法检查，以及本页显示的 GameForge 建议。";
    startConfirmation.checked = false;
    startButton.disabled = true;
    setPanel("prepared");
  } else {
    heading.textContent = `第 ${view.order} 场 · 计时中`;
    renderTask(view);
    setPanel("task");
  }
  updateTimer();
}

async function request(path, body) {
  showError("");
  const options = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok)
    throw new Error(value.detail ?? `请求失败 (${response.status})`);
  return value;
}

async function action(path, body) {
  try {
    const view = await request(path, body);
    render(view);
    return true;
  } catch (reason) {
    const failureMessage =
      reason instanceof Error ? reason.message : String(reason);
    await refreshCurrent();
    showError(failureMessage);
    return false;
  }
}

async function refreshCurrent() {
  try {
    const response = await fetch("/api/current", { cache: "no-store" });
    const view = await response.json();
    if (!response.ok) throw new Error(view.detail ?? "无法读取当前场次");
    render(view);
    return true;
  } catch (reason) {
    deadlineRefreshPending = true;
    showError(reason instanceof Error ? reason.message : String(reason));
    return false;
  }
}

document
  .getElementById("next-button")
  .addEventListener("click", () => action("/api/next"));
document
  .getElementById("recorded-next-button")
  .addEventListener("click", () => action("/api/next"));
startConfirmation.addEventListener("change", () => {
  startButton.disabled = !startConfirmation.checked;
});
startButton.addEventListener("click", () => action("/api/start"));
document
  .getElementById("pause-button")
  .addEventListener("click", () => action("/api/pause"));
document
  .getElementById("resume-button")
  .addEventListener("click", () => action("/api/resume"));

document
  .getElementById("open-editor-button")
  .addEventListener("click", async () => {
    try {
      await request("/api/open-editor");
      showNotice(
        "已打开独立的无扩展 Visual Studio Code 窗口。请只编辑其中显示的文件。",
      );
    } catch (reason) {
      showError(reason instanceof Error ? reason.message : String(reason));
    }
  });

document.getElementById("syntax-button").addEventListener("click", async () => {
  const output = document.getElementById("syntax-output");
  try {
    const result = await request("/api/syntax-check");
    output.hidden = false;
    output.textContent = [
      `exit code: ${result.exit_code}`,
      result.stdout,
      result.stderr,
    ]
      .filter(Boolean)
      .join("\n");
  } catch (reason) {
    showError(reason instanceof Error ? reason.message : String(reason));
  }
});

attestation.addEventListener("change", () => {
  finishButton.disabled = !attestation.value;
});

frozenAttestation.addEventListener("change", () => {
  frozenFinishButton.disabled = !frozenAttestation.value;
});

async function submitFinish(selection, button) {
  if (!selection.value) return;
  const chosenAttestation = selection.value;
  button.disabled = true;
  const confirmed = window.confirm(
    "确认冻结并记录本场？提交后不能修改或重做；不会立即显示正确性。",
  );
  if (!confirmed) {
    button.disabled = false;
    return;
  }
  resetTransientContent();
  heading.textContent = `第 ${current.order} 场 · 正在记录`;
  document.getElementById("frozen-label").textContent = "FINAL SUBMISSION";
  document.getElementById("frozen-heading").textContent =
    "正在冻结并记录本场提交";
  document.getElementById("frozen-guidance").textContent =
    "任务内容与辅助信息已经移除。请等待服务器确认最终状态。";
  frozenAttestation.disabled = true;
  setPanel("frozen");

  try {
    const view = await request("/api/finish", {
      participant_attested_no_contamination: chosenAttestation === "clear",
    });
    render(view);
  } catch (reason) {
    const failureMessage =
      reason instanceof Error ? reason.message : String(reason);
    const refreshed = await refreshCurrent();
    if (refreshed && ["recorded", "complete"].includes(current.phase)) return;

    const retryOnRunningTask = refreshed && current?.phase === "running";
    if (!refreshed) {
      document.getElementById("frozen-label").textContent = "STATUS UNKNOWN";
      document.getElementById("frozen-heading").textContent =
        "无法确认最终状态";
      document.getElementById("frozen-guidance").textContent =
        "任务内容与辅助信息保持隐藏。请保留同一污染声明并重试记录。";
    }
    const retrySelection = retryOnRunningTask ? attestation : frozenAttestation;
    const retryButton = retryOnRunningTask ? finishButton : frozenFinishButton;
    retrySelection.disabled = false;
    retrySelection.value = chosenAttestation;
    retryButton.disabled = false;
    showError(failureMessage);
  }
}

finishButton.addEventListener("click", () =>
  submitFinish(attestation, finishButton),
);
frozenFinishButton.addEventListener("click", () =>
  submitFinish(frozenAttestation, frozenFinishButton),
);

async function initialize() {
  await refreshCurrent();
}

setInterval(updateTimer, 250);
initialize();
