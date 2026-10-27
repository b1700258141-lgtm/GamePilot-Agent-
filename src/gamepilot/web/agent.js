"use strict";

const agentEl = (id) => document.getElementById(id);
const agentEnemyNames = {
  slime: "史莱姆", goblin: "哥布林", skeleton: "骷髅", wolf: "野狼",
  orc: "兽人", troll: "巨魔", wraith: "幽灵", dragon: "巨龙",
};
const AGENT_API = "/api/v1/agent-test-runs";
const AGENT_STORAGE_KEY = "gamepilot:last-agent-run-id";
const defectNames = {
  normal: "无缺陷",
  potion_overheal: "治疗越过生命上限",
  potion_not_consumed: "喝药不消耗药水",
  retaliate_after_death: "死亡后反击",
  loot_anomaly: "掉落物品异常",
  level_anomaly: "玩家升级异常",
  skill_power_anomaly: "技能威力异常",
};
const goalNames = {
  "battle-end": "战斗至结束",
  "full-health": "满血使用药水",
  healing: "受伤后治疗",
  victory: "击败敌人并验证结束",
};
const goalGuidance = {
  "battle-end": {
    hint: "打到最后一场获胜或玩家阵亡，并验证终态后不能继续行动。",
    criteria: ["观察到最后一场获胜或玩家阵亡", "终态后操作被拒，且状态未变"],
  },
  "full-health": {
    hint: "在满血时使用药水，检查实际回血、生命上限和药水消耗。",
    criteria: ["满血时成功使用药水", "治疗量、生命上限和药水消耗检查通过"],
  },
  healing: {
    hint: "先进入受伤状态再使用药水，检查治疗边界与药水消耗。",
    criteria: ["受伤后成功使用药水", "治疗量、生命上限和药水消耗检查通过"],
  },
  victory: {
    hint: "赢得最后一场战斗，并验证获胜后不能继续行动。",
    criteria: ["最后一场获胜且该步规则通过", "获胜后操作被拒，且状态未变"],
  },
};
const actionNames = {
  attack: "攻击", power_strike: "重击", cast_spark: "魔法飞弹",
  cast_flame: "火球", cast_meteor: "陨石", cast_heal: "治疗魔法",
  potion: "使用药水", use_potion: "使用药水", next_battle: "进入下一场",
  retaliate: "反击", drop: "掉落", level_up: "升级",
};
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
let realAccessRequestOnly = false;
const previewAccessMessage = "当前版本无权限，请联系平台制作者申请权限。";
let randomFaultPercent = 30;
let rerunLegacyProfile = null;
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
  const response = await window.gamepilotVisitor.fetch(path, { cache: "no-store", ...options });
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

function checkedFaults() {
  return [...document.querySelectorAll(".agent-force-fault:checked")].map((item) => item.value);
}

function selectedFaults(config) {
  if (Array.isArray(config.selected_faults)) return config.selected_faults;
  return config.profile && config.profile !== "normal" ? [config.profile] : [];
}

function detectedFaults(run) {
  return run.result.detected_faults || (run.result.verdict === "defect_detected" ? selectedFaults(run.config) : []);
}

function faultLabel(config) {
  const faults = selectedFaults(config);
  if (!faults.length) return config.fault_selection_mode === "random" ? "随机未注入（正常）" : "无缺陷";
  return `${config.fault_selection_mode === "random" ? "随机：" : "指定："}${faults.map((fault) => defectNames[fault] || fault).join("、")}`;
}

function faultScheduleLabel(config) {
  const faults = selectedFaults(config);
  if (!faults.length) return "无";
  if (!config.fault_schedule) return "旧报告未记录";
  return faults.map((fault) => `${defectNames[fault] || fault}：第 ${config.fault_schedule[fault]} 个候选节点`).join("；");
}

function updateDefectHint() {
  const faults = checkedFaults();
  agentEl("agent-defect-hint").textContent = faults.length
    ? `已指定 ${faults.length} 种缺陷：每种按 seed 安排生效节点，不固定在首次机会；未走到该节点则记为未触发。`
    : `不勾选时：${100 - randomFaultPercent}% 不注入缺陷，${randomFaultPercent}% 随机注入一种；出现时也按 seed 安排生效节点。`;
}

function updateProviderFields() {
  const real = agentEl("agent-provider").value === "real";
  agentEl("agent-paid-row").hidden = !real || realAccessRequestOnly;
  if (!real) agentEl("agent-paid").checked = false;
  agentEl("agent-provider-hint").textContent = real && realAccessRequestOnly
    ? previewAccessMessage
    : real
    ? "实际请求模型 deepseek-v4-flash；最多 50 步、50 次调用，每次最多请求 512 输出 Token（合计请求输出上限 25,600）。输入 Token 和金额无硬上限，费用没有可靠单价时为 unknown。"
    : realAccessRequestOnly
      ? "离线演练可直接运行；测试替身只验证工程流程，不代表真实模型能力。"
    : realAvailable
      ? "离线替身只验证工程流程，不代表真实模型能力。"
      : "离线替身只验证工程流程，不代表真实模型能力。当前服务未配置 DeepSeek 密钥，deepseek-flash 暂不可用。";
  agentFormError("");
}

function updateGoalHint() {
  const goalId = agentEl("agent-goal").value;
  agentEl("agent-goal-hint").textContent = goalGuidance[goalId]?.hint || "请选择测试目标。";
}

function formConfig() {
  const raw = agentEl("agent-seed").value.trim();
  if (raw && (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)))) {
    throw new Error("随机种子必须是 -9007199254740991 至 9007199254740991 之间的整数，或留空随机生成。");
  }
  const providerMode = agentEl("agent-provider").value;
  if (providerMode === "real" && realAccessRequestOnly) {
    throw new Error(previewAccessMessage);
  }
  if (providerMode === "real" && !agentEl("agent-paid").checked) {
    throw new Error("请先确认本次真实模型调用的预算和费用边界。");
  }
  const skillId = agentEl("agent-skill").value;
  const goalId = agentEl("agent-goal").value;
  if (!goalGuidance[goalId]) throw new Error("请选择有效的测试目标。");
  if (providerMode === "offline" && skillId !== "injured-potion-then-attack") {
    throw new Error(realAccessRequestOnly
      ? "在线预览可上传和查看自定义 skill；运行需申请真实模型权限。"
      : "自定义 skill 需要 deepseek-flash；离线替身只支持默认策略。");
  }
  return {
    goal_id: goalId,
    skill_id: skillId,
    ...(raw ? { seed: Number(raw) } : {}),
    battle_count: Number(agentEl("agent-battle-count").value),
    forced_faults: checkedFaults(),
    provider_mode: providerMode,
    paid_ack: providerMode === "real",
  };
}

function fillForm(config) {
  agentEl("agent-goal").value = goalGuidance[config.goal_id] ? config.goal_id : "battle-end";
  updateGoalHint();
  agentEl("agent-skill").value = config.skill_id || "injured-potion-then-attack";
  agentEl("agent-seed").value = String(config.seed);
  agentEl("agent-battle-count").value = String(config.battle_count || 1);
  const forced = config.fault_selection_mode === "legacy"
    ? selectedFaults(config) : config.forced_faults || [];
  for (const box of document.querySelectorAll(".agent-force-fault")) {
    box.checked = forced.includes(box.value);
  }
  agentEl("agent-provider").value = config.provider_mode;
  agentEl("agent-paid").checked = false;
  updateDefectHint();
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
    ? "真实模型读取默认策略：开局满血喝药、半血以下补药；伤害动作优先重击，冷却时按陨石→火球→魔法飞弹循环，EP 不足时普通攻击；终态后再验证一次行动限制。离线替身按所选目标使用内置动作脚本。"
    : "已选自定义文本策略。只有真实模型会根据上传内容选择动作；所有动作仍受工具、规则和预算约束。";
  agentEl("agent-skill-content").hidden = !skill || skill.built_in;
  agentEl("agent-skill-content").textContent = skill?.built_in ? "" : skill?.instructions || "";
  if (skill && !skill.built_in) agentEl("agent-skill-details").open = true;
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
    agentFeedback(realAccessRequestOnly
      ? `已将 skill“${skill.name}”保存到此标签页，可在策略说明中查看；运行需申请真实模型权限。`
      : `已添加 skill“${skill.name}”。选择 deepseek-flash 后可运行。`, "success");
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
    return ["读取初始战斗状态", `第 ${s.battle_index}/${s.battle_count} 场 · 玩家 Lv.${s.player.level} ${s.player.hp}/${s.player.max_hp} HP · ${agentEnemyNames[s.enemy_type || "slime"]} Lv.${s.enemy_level} ${s.slime.hp}/${s.slime.max_hp} HP`, s];
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
    const potionAmount = before.ruleset !== "v1"
      ? `药水效力 ${potionEvent?.value}，实际回血 ${potionEvent?.player_hp - before.player.hp}`
      : `实际回血 ${potionEvent?.player_hp - before.player.hp}`;
    const detail = potionEvent
      ? `HTTP ${item.observed_status ?? "未知"} · ${potionAmount}；生命 ${before.player.hp}→${potionEvent.player_hp}/${before.player.max_hp}，药水 ${before.player.potions}→${potionEvent.potions} · 回合结束生命 ${after.player.hp}（可能已受反击）`
      : `HTTP ${item.observed_status ?? "未知"} · 玩家 ${before.player.hp}→${after.player.hp} HP · ${agentEnemyNames[before.enemy_type] || "敌人"} ${before.slime.hp}→${after.slime.hp} HP · 金币 ${before.player.gold ?? 0}→${after.player.gold ?? 0}`;
    return [
      `第 ${item.index} 步：${actionNames[item.action] || item.action}`,
      `第 ${after.battle_index}/${after.battle_count} 场 · Lv.${before.player.level}→${after.player.level} · 经验 ${before.player.exp}→${after.player.exp}；${detail}${events ? ` · 事件：${events}` : ""}${failedRules.length ? ` · 规则失败：${failedRules.join("、")}` : ""}`,
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
    li.dataset.kind = item.kind;
    if (item.kind === "action") li.id = `agent-action-${item.index}`;
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

function latestCoverage(run) {
  if (run.result?.coverage) return run.result.coverage;
  return [...run.steps].reverse().find((item) => item.kind === "coverage") || null;
}

function renderGoalConditions(listId, goalId, coverage) {
  const list = agentEl(listId);
  list.replaceChildren();
  const conditions = coverage?.conditions?.length
    ? coverage.conditions
    : (goalGuidance[goalId]?.criteria || []).map((detail) => ({ met: false, detail }));
  for (const condition of conditions) {
    const item = document.createElement("li");
    const status = document.createElement("span");
    status.className = `goal-coverage-status${condition.met ? " met" : ""}`;
    status.textContent = condition.met ? "已满足" : "待完成";
    const detail = document.createElement("span");
    detail.textContent = condition.detail;
    item.append(status, detail);
    if (condition.met && Number.isInteger(condition.evidence_step)) {
      const link = document.createElement("a");
      link.className = "goal-evidence-link";
      link.href = `#agent-action-${condition.evidence_step}`;
      link.textContent = `查看第 ${condition.evidence_step} 步原始记录`;
      link.addEventListener("click", () => {
        const record = document.getElementById(`agent-action-${condition.evidence_step}`);
        const raw = record?.querySelector("details");
        if (raw) raw.open = true;
      });
      item.append(link);
    }
    list.append(item);
  }
}

function resultReason(run) {
  const result = run.result;
  const stop = result.stop_detail ? ` ${result.stop_detail}` : "";
  const earlyStop = result.goal_met ? "" : "检出缺陷后测试已停止，因此完整目标覆盖显示未满足。";
  switch (result.verdict) {
    case "defect_detected": {
      const pending = result.untriggered_faults?.length
        ? `另有 ${result.untriggered_faults.map((fault) => defectNames[fault]).join("、")} 尚未到生效节点。`
        : "";
      return `已检出 ${detectedFaults(run).map((fault) => defectNames[fault]).join("、")}；独立规则 ${result.target_rules_hit.join("、")} 失败。${pending}${earlyStop}`;
    }
    case "rule_failure":
      return `独立规则 ${result.failing_rules.join("、")} 失败；请查看动作详情中的期望与实际。${stop}`;
    case "agent_missed":
      return `已触发但未检出：${(result.missed_faults || selectedFaults(run.config)).map((fault) => defectNames[fault]).join("、")}。${stop}`;
    case "goal_incomplete":
      return `测试目标尚未覆盖。${stop}`;
    case "not_triggered":
      return `未实际触发：${(result.untriggered_faults || selectedFaults(run.config)).map((fault) => defectNames[fault]).join("、")}。本次未走到预定的候选节点；不能据此判断 Agent 漏检。${stop}`;
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
  card.dataset.verdict = run.status === "error" ? "execution_error" : run.result.verdict;
  agentEl("agent-result-facts").replaceChildren();
  if (run.status === "error") {
    agentEl("agent-final-goal").hidden = true;
    agentEl("agent-result-heading").textContent = "执行错误";
    agentEl("agent-result-reason").textContent = run.message;
    agentEl("agent-report-link").hidden = true;
    return;
  }
  const result = run.result;
  agentEl("agent-final-goal").hidden = false;
  renderGoalConditions("agent-final-goal-list", run.config.goal_id, result.coverage);
  const name = verdictNames[result.verdict] || result.verdict;
  agentEl("agent-result-heading").textContent = result.verdict === "defect_detected"
    ? `${name}：${detectedFaults(run).map((fault) => defectNames[fault]).join("、")}` : name;
  agentEl("agent-result-reason").textContent = resultReason(run);
  addFact("缺陷选择", faultLabel(run.config));
  addFact("计划生效节点", faultScheduleLabel(run.config));
  addFact("目标覆盖", result.goal_met ? "已满足" : "未满足");
  const triggerDetails = result.trigger_details || (result.triggered
    ? [{ fault_id: selectedFaults(run.config)[0] }] : []);
  addFact("缺陷触发", triggerDetails.length
    ? triggerDetails.map((item) => {
      const name = defectNames[item.fault_id] || item.fault_id;
      return item.battle_index == null ? name
        : `${name}（第 ${item.battle_index} 场、回合 ${item.turn}）`;
    }).join("、") : "未触发");
  addFact("已检出缺陷", detectedFaults(run).length
    ? detectedFaults(run).map((fault) => defectNames[fault]).join("、") : "无");
  addFact("未触发缺陷", result.untriggered_faults?.length
    ? result.untriggered_faults.map((fault) => defectNames[fault]).join("、") : "无");
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
    previousAgentRun.config.battle_count === run.config.battle_count &&
    previousAgentRun.config.provider_mode === run.config.provider_mode &&
    JSON.stringify(selectedFaults(previousAgentRun.config)) !== JSON.stringify(selectedFaults(run.config));
  agentEl("agent-compare-card").hidden = !comparable;
  if (comparable) {
    agentEl("agent-compare-text").textContent =
      `${faultLabel(previousAgentRun.config)}：${verdictNames[previousAgentRun.result.verdict]}；` +
      `${faultLabel(run.config)}：${verdictNames[run.result.verdict]}。` +
      "两次运行使用相同目标、种子和 skill，Agent 选择的动作可能不同。";
  }
}

function renderAgentRun(run) {
  currentAgentRun = run;
  agentEl("agent-empty-state").hidden = true;
  agentEl("agent-run-card").hidden = false;
  agentEl("agent-process-card").hidden = false;
  const status = agentEl("agent-run-status");
  status.textContent = { queued: "等待中", running: "运行中", completed: "已结束", error: "执行错误" }[run.status];
  status.className = `status-chip ${run.status === "error" ? "error" : run.status === "running" ? "running" : run.result?.verdict === "defect_detected" ? "detected" : ""}`;
  agentEl("agent-model-count").textContent = `${run.model_calls} / ${run.budget.max_model_calls}`;
  agentEl("agent-action-count").textContent = `${run.actions_attempted} / ${run.budget.max_action_attempts}`;
  agentEl("agent-run-config").textContent = `${goalNames[run.config.goal_id] || run.config.goal_id} · ${run.skill?.name || "默认策略"} · ${run.config.battle_count || 1} 场 · seed ${run.config.seed} · ${faultLabel(run.config)}`;
  agentEl("agent-run-id").textContent = run.run_id;
  renderGoalConditions("agent-progress-goal-list", run.config.goal_id, latestCoverage(run));
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
    try { window.gamepilotVisitor.storage().setItem(AGENT_STORAGE_KEY, run.run_id); } catch { /* 存储可能被禁用 */ }
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
for (const box of document.querySelectorAll(".agent-force-fault")) {
  box.addEventListener("change", () => {
    rerunLegacyProfile = null;
    updateDefectHint();
  });
}
agentEl("agent-skill").addEventListener("change", updateSkillHint);
agentEl("agent-skill-upload").addEventListener("change", () => void uploadSkill());
agentEl("agent-provider").addEventListener("change", updateProviderFields);
agentEl("agent-goal").addEventListener("change", updateGoalHint);
agentEl("agent-form").addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    const config = formConfig();
    if (rerunLegacyProfile !== null) {
      delete config.forced_faults;
      config.profile = rerunLegacyProfile;
      rerunLegacyProfile = null;
    }
    void startAgentRun(config);
  } catch (error) { agentFormError(error.message); }
});
agentEl("agent-rerun").addEventListener("click", () => {
  if (!currentAgentRun) return;
  fillForm(currentAgentRun.config);
  if (currentAgentRun.config.provider_mode === "real") {
    rerunLegacyProfile = currentAgentRun.config.fault_selection_mode === "legacy"
      ? currentAgentRun.config.profile : null;
    agentFeedback("请再次确认本次真实模型测试，再点击开始。", "busy");
    agentEl("agent-paid").focus();
  } else {
    const config = formConfig();
    if (currentAgentRun.config.fault_selection_mode === "legacy") {
      delete config.forced_faults;
      config.profile = currentAgentRun.config.profile;
    }
    void startAgentRun(config);
  }
});
agentEl("agent-report-link").addEventListener("click", async (event) => {
  if (!window.gamepilotVisitor.isPreview()) return;
  event.preventDefault();
  const link = agentEl("agent-report-link");
  try {
    const report = await agentRequest(link.href);
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)],
      { type: "application/json" }));
    const download = document.createElement("a");
    download.href = url;
    download.download = link.download;
    download.click();
    setTimeout(() => URL.revokeObjectURL(url), 1_000);
  } catch (error) {
    agentFormError(error.message || "报告下载失败。");
  }
});
agentEl("agent-switch-defect").addEventListener("click", () => {
  if (!currentAgentRun) return;
  rerunLegacyProfile = null;
  fillForm(currentAgentRun.config);
  agentFeedback("已保留目标、skill 和种子。调整勾选后开始新测试；旧报告仍保留。", "busy");
  document.querySelector(".agent-force-fault")?.focus();
});

async function initializeAgentPage() {
  updateGoalHint();
  updateDefectHint();
  updateProviderFields();
  try {
    const config = await agentRequest(`${AGENT_API}/config`);
    realAvailable = config.real_available;
    realAccessRequestOnly = config.real_access_request_only === true;
    randomFaultPercent = config.random_fault_percent;
    agentEl("agent-real-option").disabled = !realAvailable && !realAccessRequestOnly;
    if (realAccessRequestOnly) agentEl("agent-real-option").textContent = "真实模型测试（需申请权限）";
    if (realAccessRequestOnly) {
      agentEl("agent-upload-hint").textContent =
        "文件不超过 16 KB、内容最多 4,000 字符。上传后仅在此标签页查看，关闭后清空；运行自定义策略需申请真实模型权限。";
    }
    updateDefectHint();
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
  try { lastId = window.gamepilotVisitor.storage().getItem(AGENT_STORAGE_KEY); } catch { /* 存储可能被禁用 */ }
  if (lastId) {
    showMode("agent");
    await pollAgentRun(lastId);
    if (currentAgentRun) fillForm(currentAgentRun.config);
  }
}

void initializeAgentPage();
