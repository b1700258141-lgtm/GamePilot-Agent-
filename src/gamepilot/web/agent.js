"use strict";

const agentEl = (id) => document.getElementById(id);
const agentEnemyNames = { slime: "史莱姆", goblin: "哥布林", troll: "巨魔" };
const AGENT_API = "/api/v1/agent-test-runs";
const AGENT_STORAGE_KEY = "gamepilot:last-agent-run-id";
const defectNames = {
  normal: "关闭",
  potion_overheal: "治疗越过生命上限",
  potion_not_consumed: "喝药不消耗药水",
  retaliate_after_death: "死亡后反击",
  loot_anomaly: "掉落物品异常",
};
const goalNames = {
  "battle-end": "战斗至结束",
  "full-health": "满血使用药水",
  healing: "受伤后治疗",
  victory: "击败敌人并验证结束",
};
const actionNames = { attack: "攻击", potion: "使用药水", use_potion: "使用药水", retaliate: "反击", drop: "掉落" };
const verdictNames = {
  pass: "游戏规则通过",
  defect_detected: "发现游戏缺陷",
  rule_failure: "发现规则异常",
  agent_missed: "Agent 漏检",
  goal_incomplete: "目标未完成",
  not_triggered: "缺陷未触发",
  execution_error: "执行错误",
};
let currentAgentRun = null;
let previousAgentRun = null;
let agentPollTimer = null;
let realAvailable = false;
let agentSkills = [];

function showMode(mode) {
  const manual = mode === "manual";
  agentEl("manual-panel").hidden = !manual;
  agentEl("agent-panel").hidden = manual;
  for (const [id, active] of [["mode-manual", manual], ["mode-agent", !manual]]) {
    const button = agentEl(id);
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
}

async function agentRequest(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body?.detail === "string" ? body.detail : "请求失败";
    throw new Error(`${detail}（HTTP ${response.status}）`);
  }
  return body;
}

function agentFeedback(message, kind = "success") {
  const node = agentEl("agent-feedback");
  if (node.textContent !== message || !node.classList.contains(kind)) {
    node.textContent = message;
    node.className = `feedback ${kind}`;
  }
}

function agentFormError(message) {
  const node = agentEl("agent-form-error");
  node.hidden = !message;
  node.textContent = message;
}

function updateDefectFields() {
  const enabled = agentEl("agent-defect-enabled").checked;
  agentEl("agent-defect-fields").hidden = !enabled;
  if (enabled) agentEl("agent-defect-hint").textContent =
    "缺陷只作用于本次隔离战斗。自定义 skill 若没有执行到触发动作，结果会标为“缺陷未触发”。";
}

function updateProviderFields() {
  const real = agentEl("agent-provider").value === "real";
  agentEl("agent-paid-row").hidden = !real;
  if (!real) agentEl("agent-paid").checked = false;
  agentEl("agent-provider-hint").textContent = real
    ? "实际请求模型 deepseek-v4-flash；最多 20 步、20 次调用，每次最多请求 512 输出 Token（合计请求输出上限 10,240）。输入 Token 和金额无硬上限，费用没有可靠单价时为 unknown。"
    : realAvailable
      ? "离线替身只验证工程流程，不代表真实模型能力。"
      : "离线替身只验证工程流程，不代表真实模型能力。当前服务未配置 DeepSeek 密钥，deepseek-flash 暂不可用。";
}

function formConfig() {
  const raw = agentEl("agent-seed").value.trim();
  if (raw && (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)))) {
    throw new Error("随机种子必须是 -9007199254740991 至 9007199254740991 之间的整数，或留空随机生成。");
  }
  const providerMode = agentEl("agent-provider").value;
  if (providerMode === "real" && !agentEl("agent-paid").checked) {
    throw new Error("请先确认本次真实模型调用的预算和费用边界。");
  }
  const skillId = agentEl("agent-skill").value;
  if (providerMode === "offline" && skillId !== "injured-potion-then-attack") {
    throw new Error("自定义 skill 需要 deepseek-flash；离线替身只支持默认策略。");
  }
  return {
    goal_id: "battle-end",
    skill_id: skillId,
    ...(raw ? { seed: Number(raw) } : {}),
    profile: agentEl("agent-defect-enabled").checked ? agentEl("agent-defect").value : "normal",
    provider_mode: providerMode,
    paid_ack: providerMode === "real",
  };
}

function fillForm(config) {
  agentEl("agent-skill").value = config.skill_id || "injured-potion-then-attack";
  agentEl("agent-seed").value = String(config.seed);
  agentEl("agent-defect-enabled").checked = config.profile !== "normal";
  if (config.profile !== "normal") agentEl("agent-defect").value = config.profile;
  agentEl("agent-provider").value = config.provider_mode;
  agentEl("agent-paid").checked = false;
  updateDefectFields();
  updateProviderFields();
}

function renderSkillChoices(selectedId = agentEl("agent-skill").value) {
  const select = agentEl("agent-skill");
  select.replaceChildren();
  for (const skill of agentSkills) {
    const option = document.createElement("option");
    option.value = skill.skill_id;
    option.textContent = skill.name + (skill.built_in ? "（默认）" : "（已上传）");
    select.append(option);
  }
  select.value = agentSkills.some((skill) => skill.skill_id === selectedId)
    ? selectedId : "injured-potion-then-attack";
  updateSkillHint();
}

function updateSkillHint() {
  const skill = agentSkills.find((item) => item.skill_id === agentEl("agent-skill").value);
  agentEl("agent-skill-hint").textContent = skill?.built_in
    ? "默认策略：开局满血喝药，受伤后补药，药水耗尽持续攻击；终态后再验证一次行动限制。"
    : "已选自定义文本策略。只有真实模型会根据上传内容选择动作；所有动作仍受工具、规则和预算约束。";
}

async function uploadSkill() {
  const input = agentEl("agent-skill-upload");
  const file = input.files?.[0];
  if (!file) return;
  try {
    if (!/\.(md|txt)$/i.test(file.name) || file.size > 16000) {
      throw new Error("请选择不超过 16 KB 的 .md 或 .txt 文件（内容最多 4,000 字符）。");
    }
    const instructions = (await file.text()).trim();
    if (!instructions || instructions.length > 4000) {
      throw new Error("skill 内容须为 1 至 4,000 字符。");
    }
    const heading = instructions.match(/^#\s+(.+)$/m)?.[1]?.trim();
    const name = (heading || file.name.replace(/\.(md|txt)$/i, "")).slice(0, 80);
    const skill = await agentRequest(`${AGENT_API}/skills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, instructions }),
    });
    agentSkills = await agentRequest(`${AGENT_API}/skills`);
    renderSkillChoices(skill.skill_id);
    agentFeedback(`已添加 skill“${skill.name}”。选择 deepseek-flash 后可运行。`, "success");
    agentFormError("");
  } catch (error) {
    agentFormError(error.message || "上传 skill 失败。");
  } finally {
    input.value = "";
  }
}

function timelineText(item) {
  if (item.kind === "initial") {
    const s = item.snapshot;
    return ["读取初始战斗状态", `玩家 ${s.player.hp}/${s.player.max_hp} HP · ${agentEnemyNames[s.enemy_type || "slime"]} ${s.slime.hp}/${s.slime.max_hp} HP`, s];
  }
  if (item.kind === "model") {
    const action = item.tool_calls?.map((call) => {
      const name = call.arguments?.action || call.name;
      return actionNames[name] || name;
    }).join("、") || "未选择工具";
    return [`第 ${item.index} 次模型调用 · ${action}`, item.response_text || "模型未提供文字说明。", item];
  }
  if (item.kind === "decision") {
    const action = item.action || item.tool;
    const label = actionNames[action] || action || "未识别工具";
    return [
      `工具校验：${label} ${item.accepted ? "已接受" : "被拒绝"}`,
      item.accepted ? "可执行的工具请求。" : `${item.detail}；未向游戏发出动作。`,
      item,
    ];
  }
  if (item.kind === "action") {
    const before = item.before;
    const after = item.after || before;
    const failedRules = [...new Set((item.checks || []).filter((check) => check.status === "fail").map((check) => check.rule_id))];
    const potionEvent = (item.new_events || []).find((event) => event.actor === "player" && event.kind === "potion");
    const events = (item.new_events || []).map((event) => `${actionNames[event.kind] || event.kind} ${event.value}${event.item ? ` ${event.item === "gold" ? "金币" : event.item}` : ""}`).join("、");
    const potionAmount = before.ruleset === "v2"
      ? `药水效力 ${potionEvent?.value}，实际回血 ${potionEvent?.player_hp - before.player.hp}`
      : `实际回血 ${potionEvent?.player_hp - before.player.hp}`;
    const detail = potionEvent
      ? `HTTP ${item.observed_status ?? "未知"} · ${potionAmount}；生命 ${before.player.hp}→${potionEvent.player_hp}/${before.player.max_hp}，药水 ${before.player.potions}→${potionEvent.potions} · 回合结束生命 ${after.player.hp}（可能已受反击）`
      : `HTTP ${item.observed_status ?? "未知"} · 玩家 ${before.player.hp}→${after.player.hp} HP · ${agentEnemyNames[before.enemy_type] || "敌人"} ${before.slime.hp}→${after.slime.hp} HP · 金币 ${before.player.gold ?? 0}→${after.player.gold ?? 0}`;
    return [
      `第 ${item.index} 步：${item.action === "attack" ? "攻击" : "使用药水"}`,
      `${detail}${events ? ` · 事件：${events}` : ""}${failedRules.length ? ` · 规则失败：${failedRules.join("、")}` : ""}`,
      item,
    ];
  }
  const unmet = (item.conditions || []).filter((condition) => !condition.met);
  return ["目标覆盖检查", item.met ? "覆盖条件已满足。" : `尚未满足：${unmet.map((condition) => condition.detail).join("；")}`, item];
}

function renderTimeline(steps) {
  const list = agentEl("agent-timeline");
  const opened = new Set([...list.querySelectorAll("details[open]")].map((node) => node.dataset.sequence));
  list.replaceChildren();
  agentEl("agent-step-count").textContent = `${steps.length} 项`;
  agentEl("agent-no-steps").hidden = steps.length > 0;
  for (const item of steps) {
    const [title, description, evidence] = timelineText(item);
    const li = document.createElement("li");
    if ((item.kind === "decision" && !item.accepted) ||
      (item.kind === "action" && (item.checks || []).some((check) => check.status === "fail"))) {
      li.className = "problem";
    }
    const heading = document.createElement("strong");
    heading.textContent = title;
    const paragraph = document.createElement("p");
    paragraph.textContent = description;
    const details = document.createElement("details");
    details.dataset.sequence = String(item.sequence);
    details.open = opened.has(String(item.sequence));
    const summary = document.createElement("summary");
    summary.textContent = "查看记录详情";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(evidence, null, 2);
    details.append(summary, pre);
    li.append(heading, paragraph, details);
    list.append(li);
  }
}

function addFact(label, value) {
  const box = document.createElement("div");
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = value;
  box.append(term, detail);
  agentEl("agent-result-facts").append(box);
}

function resultReason(run) {
  const result = run.result;
  const stop = result.stop_detail ? ` ${result.stop_detail}` : "";
  const potionStep = run.steps.find((item) => item.kind === "action" && item.action === "use_potion" && item.observed_status === 200);
  const potionEvent = potionStep?.new_events?.find((event) => event.actor === "player" && event.kind === "potion");
  const earlyStop = result.goal_met ? "" : "检出缺陷后测试已停止，因此完整目标覆盖显示未满足。";
  switch (result.verdict) {
    case "defect_detected":
      if (run.config.profile === "potion_overheal" && potionEvent) {
        return `喝药事件生命值为 ${potionEvent.player_hp}/${potionStep.before.player.max_hp}；回合结束后的生命值可能已被敌方反击降低。独立规则 ${result.target_rules_hit.join("、")} 检出缺陷。${earlyStop}`;
      }
      if (run.config.profile === "potion_not_consumed" && potionEvent) {
        return `喝药事件药水数量 ${potionStep.before.player.potions}→${potionEvent.potions}，没有减少。独立规则 ${result.target_rules_hit.join("、")} 检出缺陷。${earlyStop}`;
      }
      return `独立规则 ${result.target_rules_hit.join("、")} 失败。Agent 成功检出，游戏规则未通过。${earlyStop}`;
    case "rule_failure":
      return `独立规则 ${result.failing_rules.join("、")} 失败；请查看动作详情中的期望与实际。${stop}`;
    case "agent_missed":
      return `缺陷已触发，但未检出目标规则 ${result.target_rules.join("、")}。${stop}`;
    case "goal_incomplete":
      return `测试目标尚未覆盖。${stop}`;
    case "not_triggered":
      if (["potion_overheal", "potion_not_consumed"].includes(run.config.profile) && !potionStep) {
        return `本次没有成功喝药，所选缺陷因此未触发。可换用会在受伤后喝药的 skill 再测。${stop}`;
      }
      return `本次动作未触发所选缺陷；不能据此判断 Agent 漏检。${stop}`;
    case "execution_error":
      return result.replay_outcome !== "match"
        ? `无模型重放结果为 ${result.replay_outcome}，本次测试不能判定通过。${stop}`
        : `执行过程中发生错误，本次测试不能判定通过。${stop}`;
    default:
      return result.stop_detail || "覆盖条件已满足，所观察规则通过。";
  }
}

function renderResult(run) {
  const card = agentEl("agent-result-card");
  card.hidden = !["completed", "error"].includes(run.status);
  if (card.hidden) return;
  agentEl("agent-result-facts").replaceChildren();
  if (run.status === "error") {
    agentEl("agent-result-heading").textContent = "执行错误";
    agentEl("agent-result-reason").textContent = run.message;
    agentEl("agent-report-link").hidden = true;
    return;
  }
  const result = run.result;
  const name = verdictNames[result.verdict] || result.verdict;
  agentEl("agent-result-heading").textContent = result.verdict === "defect_detected"
    ? `${name}：${defectNames[run.config.profile]}` : name;
  agentEl("agent-result-reason").textContent = resultReason(run);
  addFact("目标覆盖", result.goal_met ? "已满足" : "未满足");
  addFact("缺陷触发", result.triggered ? "已触发" : "未触发");
  addFact("规则失败", result.failing_rules.length ? result.failing_rules.join("、") : "无");
  addFact("无模型重放", result.replay_outcome === "match" ? "一致" : result.replay_outcome);
  const usage = result.usage;
  addFact("模型 Token", usage.available
    ? `输入 ${usage.prompt_tokens} / 输出 ${usage.completion_tokens} / 合计 ${usage.total_tokens}`
    : "unknown");
  addFact("费用", result.cost.amount == null ? "unknown" : `${result.cost.amount} ${result.cost.currency || ""}`);
  addFact("执行方式", result.provider_is_test_double ? "离线测试替身" : "真实模型");
  agentEl("agent-report-link").hidden = false;
  agentEl("agent-report-link").href = `${AGENT_API}/${encodeURIComponent(run.run_id)}/report`;
}

function renderComparison(run) {
  const comparable = previousAgentRun?.status === "completed" && run.status === "completed" &&
    previousAgentRun.config.seed === run.config.seed &&
    previousAgentRun.config.goal_id === run.config.goal_id &&
    previousAgentRun.config.skill_id === run.config.skill_id &&
    previousAgentRun.config.provider_mode === run.config.provider_mode &&
    previousAgentRun.config.profile !== run.config.profile;
  agentEl("agent-compare-card").hidden = !comparable;
  if (comparable) {
    agentEl("agent-compare-text").textContent =
      `${defectNames[previousAgentRun.config.profile]}：${verdictNames[previousAgentRun.result.verdict]}；` +
      `${defectNames[run.config.profile]}：${verdictNames[run.result.verdict]}。` +
      "两次运行使用相同种子和 skill，Agent 选择的动作可能不同。";
  }
}

function renderAgentRun(run) {
  currentAgentRun = run;
  agentEl("agent-run-card").hidden = false;
  agentEl("agent-process-card").hidden = false;
  const status = agentEl("agent-run-status");
  status.textContent = { queued: "等待中", running: "运行中", completed: "已结束", error: "执行错误" }[run.status];
  status.className = `status-chip ${run.status === "error" ? "error" : run.status === "running" ? "running" : run.result?.verdict === "defect_detected" ? "detected" : ""}`;
  agentEl("agent-model-count").textContent = `${run.model_calls} / ${run.budget.max_model_calls}`;
  agentEl("agent-action-count").textContent = `${run.actions_attempted} / ${run.budget.max_action_attempts}`;
  agentEl("agent-run-config").textContent = `${run.skill?.name || goalNames[run.config.goal_id]} · seed ${run.config.seed} · ${defectNames[run.config.profile]}`;
  agentEl("agent-run-id").textContent = run.run_id;
  agentFeedback(run.message, run.status === "error" ? "error" : run.status === "running" ? "busy" : "success");
  agentEl("agent-start").disabled = run.status === "queued" || run.status === "running";
  renderTimeline(run.steps);
  renderResult(run);
  renderComparison(run);
}

async function pollAgentRun(runId) {
  if (agentPollTimer) clearTimeout(agentPollTimer);
  try {
    const run = await agentRequest(`${AGENT_API}/${encodeURIComponent(runId)}`);
    renderAgentRun(run);
    if (run.status === "queued" || run.status === "running") {
      agentPollTimer = setTimeout(() => void pollAgentRun(runId), document.hidden ? 4000 : 1000);
    }
  } catch (error) {
    agentFeedback(error.message || "无法读取测试状态。", "error");
    agentEl("agent-start").disabled = false;
  }
}

async function startAgentRun(config) {
  agentFormError("");
  agentEl("agent-start").disabled = true;
  agentFeedback("正在创建自动测试…", "busy");
  try {
    if (currentAgentRun?.status === "completed") previousAgentRun = currentAgentRun;
    const run = await agentRequest(AGENT_API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    currentAgentRun = run;
    try { localStorage.setItem(AGENT_STORAGE_KEY, run.run_id); } catch { /* 存储可能被禁用 */ }
    showMode("agent");
    renderAgentRun(run);
    await pollAgentRun(run.run_id);
  } catch (error) {
    agentFormError(error.message || "无法启动自动测试。");
    agentFeedback("自动测试未启动。", "error");
    agentEl("agent-start").disabled = false;
  }
}

agentEl("mode-manual").addEventListener("click", () => showMode("manual"));
agentEl("mode-agent").addEventListener("click", () => showMode("agent"));
for (const id of ["agent-defect-enabled", "agent-defect"]) {
  agentEl(id).addEventListener("change", updateDefectFields);
}
agentEl("agent-skill").addEventListener("change", updateSkillHint);
agentEl("agent-skill-upload").addEventListener("change", () => void uploadSkill());
agentEl("agent-provider").addEventListener("change", updateProviderFields);
agentEl("agent-form").addEventListener("submit", (event) => {
  event.preventDefault();
  try { void startAgentRun(formConfig()); } catch (error) { agentFormError(error.message); }
});
agentEl("agent-rerun").addEventListener("click", () => {
  if (!currentAgentRun) return;
  fillForm(currentAgentRun.config);
  if (currentAgentRun.config.provider_mode === "real") {
    agentFeedback("请再次确认本次真实模型测试，再点击开始。", "busy");
    agentEl("agent-paid").focus();
  } else {
    void startAgentRun({ ...currentAgentRun.config, paid_ack: false });
  }
});
agentEl("agent-switch-defect").addEventListener("click", () => {
  if (!currentAgentRun) return;
  fillForm(currentAgentRun.config);
  const enabled = currentAgentRun.config.profile === "normal";
  agentEl("agent-defect-enabled").checked = enabled;
  if (enabled) agentEl("agent-defect").value = "potion_overheal";
  updateDefectFields();
  agentFeedback("已保留 skill 和种子。选择缺陷后开始新测试；旧报告仍保留。", "busy");
  agentEl(enabled ? "agent-defect" : "agent-defect-enabled").focus();
});

async function initializeAgentPage() {
  updateDefectFields();
  updateProviderFields();
  try {
    const config = await agentRequest(`${AGENT_API}/config`);
    realAvailable = config.real_available;
    agentEl("agent-real-option").disabled = !realAvailable;
    updateProviderFields();
  } catch {
    agentEl("agent-real-option").disabled = true;
  }
  try {
    agentSkills = await agentRequest(`${AGENT_API}/skills`);
    renderSkillChoices();
  } catch {
    agentFormError("无法加载 skill 列表；请检查服务状态。");
  }
  let lastId = null;
  try { lastId = localStorage.getItem(AGENT_STORAGE_KEY); } catch { /* 存储可能被禁用 */ }
  if (lastId) {
    showMode("agent");
    await pollAgentRun(lastId);
    if (currentAgentRun) fillForm(currentAgentRun.config);
  }
}

void initializeAgentPage();
