"use strict";

const API_ROOT = "/api/v1/game-sessions";
const STORAGE_KEY = "gamepilot:last-session-id";
const $ = (id) => document.getElementById(id);
let snapshot = null;
let busy = false;
const enemyNames = {
  slime: "史莱姆", goblin: "哥布林", skeleton: "骷髅", wolf: "野狼",
  orc: "兽人", troll: "巨魔", wraith: "幽灵", dragon: "巨龙",
};
const spellCosts = { "cast-spark-button": 20, "cast-flame-button": 40,
  "cast-meteor-button": 70, "cast-heal-button": 30 };

function feedback(message, kind = "success") {
  const node = $("feedback");
  node.textContent = message;
  node.className = `feedback ${kind}`;
}

function setServiceStatus(ok) {
  const node = $("service-status");
  node.textContent = ok ? "服务已就绪" : "服务不可用";
  node.className = `service-status ${ok ? "ok" : "error"}`;
}

async function request(path, options = {}) {
  let response;
  try {
    response = await window.gamepilotVisitor.fetch(path, { cache: "no-store", ...options });
  } catch {
    setServiceStatus(false);
    throw new Error("连接不到服务。请确认 Docker / FastAPI 正在运行，再刷新页面。");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.message || (Array.isArray(body?.detail) ? "输入格式不正确" : "请求失败");
    throw new Error(`${detail}（HTTP ${response.status}${body?.code ? ` · ${body.code}` : ""}）`);
  }
  return body;
}

function controls() {
  const active = snapshot?.status === "active";
  $("create-button").disabled = busy;
  $("load-button").disabled = busy;
  $("seed-input").disabled = busy;
  $("battle-count-input").disabled = busy;
  $("session-input").disabled = busy;
  $("attack-button").disabled = busy || !active;
  $("power-strike-button").disabled = busy || !active || !["v3", "v4", "v5"].includes(snapshot.ruleset) ||
    snapshot.player.power_strike_cooldown > 0;
  for (const [button, cost] of Object.entries(spellCosts)) {
    $(button).disabled = busy || !active || !["v4", "v5"].includes(snapshot.ruleset) || snapshot.player.ep < cost;
    $(button).title = !["v4", "v5"].includes(snapshot?.ruleset) ? "当前会话不支持魔法"
      : snapshot.player.ep < cost ? `EP 不足，需要 ${cost} 点` : "魔法无冷却";
  }
  $("potion-button").disabled = busy || !active || snapshot.player.potions < 1 ||
    (snapshot.ruleset === "v1" && snapshot.player.hp >= snapshot.player.max_hp);
  $("next-battle-button").disabled = busy || !["v3", "v4", "v5"].includes(snapshot?.ruleset) ||
    snapshot.status !== "won" || snapshot.battle_index >= snapshot.battle_count;
  $("refresh-button").disabled = busy || !snapshot;
  if (!snapshot) return;
  $("action-hint").textContent = !active
    ? snapshot.status === "won" && snapshot.battle_index < snapshot.battle_count
      ? "当前场已获胜，点击“进入下一场”继续连战。"
      : "连战已结束。可以创建新会话，或读取另一场战斗。"
    : snapshot.player.potions < 1
        ? "药水已用完。"
        : "药水固定效力 25，实际回血不超过生命上限；行动后存活的敌人会反击。";
}

function renderEvents(events) {
  const list = $("event-list");
  list.replaceChildren();
  $("event-count").textContent = `${events.length} 条`;
  $("no-events").hidden = events.length > 0;
  const descriptions = {
    attack: "玩家攻击",
    power_strike: "玩家重击",
    cast_spark: "玩家施放魔法飞弹",
    cast_flame: "玩家施放火球",
    cast_meteor: "玩家施放陨石",
    cast_heal: "玩家施放治疗魔法",
    potion: "玩家使用药水",
    retaliate: "敌人反击",
    drop: "击杀掉落",
    level_up: "玩家升级",
    next_battle: "进入下一场",
  };
  for (const event of events) {
    const item = document.createElement("li");
    const summary = document.createElement("span");
    const name = document.createElement("span");
    name.textContent = event.kind === "retaliate"
      ? `${enemyNames[event.actor] || event.actor}反击`
      : descriptions[event.kind] || event.kind;
    const meta = document.createElement("span");
    meta.className = "event-meta";
    meta.textContent = `第 ${event.turn} 回合 · 玩家 ${event.player_hp} HP · 敌人 ${event.slime_hp} HP`;
    summary.append(name, meta);
    const value = document.createElement("strong");
    value.textContent = event.kind === "drop"
      ? `+${event.value} ${event.item === "gold" ? "金币" : event.item === "exp" ? "经验" : event.item || "物品"}`
      : event.kind === "level_up" ? `Lv.${event.value}`
      : event.kind === "next_battle" ? `第 ${event.value} 场`
      : (event.kind === "potion" && snapshot?.ruleset !== "v1") || event.kind === "cast_heal"
        ? `效力 ${event.value} · 实际 +${Math.max(0, event.player_hp - (events[events.indexOf(event) - 1]?.player_hp ?? 100))}`
        : `${event.kind === "potion" ? "+" : "−"}${event.value}`;
    item.append(summary, value);
    list.append(item);
  }
}

function render(next) {
  snapshot = next;
  $("empty-state").hidden = Boolean(next);
  $("battle-view").hidden = !next;
  $("history-section").hidden = !next;
  if (!next) {
    controls();
    return;
  }
  $("session-input").value = next.session_id;
  $("session-value").textContent = next.session_id;
  $("seed-value").textContent = next.seed;
  $("battle-progress-value").textContent = `${next.battle_index} / ${next.battle_count}`;
  $("turn-value").textContent = next.turn;
  $("battle-status").textContent = { active: "进行中", won: "玩家获胜", lost: "玩家失败" }[next.status] || next.status;
  $("battle-status").className = `status-chip ${next.status}`;
  const enemyName = enemyNames[next.enemy_type] || "史莱姆";
  $("battle-heading").textContent = `玩家 vs ${enemyName}`;
  $("slime-heading").textContent = enemyName;
  $("slime-progress").setAttribute("aria-label", `${enemyName}生命值`);
  $("player-hp").textContent = `${next.player.hp} / ${next.player.max_hp}`;
  $("player-progress").max = next.player.max_hp;
  $("player-progress").value = next.player.hp;
  $("slime-hp").textContent = `${next.slime.hp} / ${next.slime.max_hp}`;
  $("slime-progress").max = next.slime.max_hp;
  $("slime-progress").value = next.slime.hp;
  $("potions-value").textContent = next.player.potions;
  $("gold-value").textContent = next.player.gold ?? 0;
  $("player-level-value").textContent = next.player.level ?? 1;
  $("player-exp-value").textContent = `${next.player.exp ?? 0} / ${next.ruleset === "v5" ? 15 * next.player.level : 15}`;
  $("player-ep-value").textContent = `${next.player.ep ?? 100} / ${next.player.max_ep ?? 100}`;
  $("player-ep-progress").max = next.player.max_ep ?? 100;
  $("player-ep-progress").value = next.player.ep ?? 100;
  $("player-magic-value").textContent = next.player.magic ?? 10;
  const cooldown = next.player.power_strike_cooldown ?? 0;
  $("skill-status").textContent = !["v3", "v4", "v5"].includes(next.ruleset)
    ? "重击：旧规则会话不可用"
    : cooldown > 0
      ? `重击：还需 ${cooldown} 次其他战斗动作（攻击、药水或魔法）`
      : "重击：可用；比普通攻击多 12 点伤害，使用后冷却 2 次动作";
  $("enemy-level-value").textContent = next.enemy_level ?? 1;
  $("raw-json").textContent = JSON.stringify(next, null, 2);
  renderEvents(next.events);
  controls();
}

async function run(label, operation) {
  if (busy) return;
  busy = true;
  controls();
  feedback(`${label}…`, "busy");
  try {
    const next = await operation();
    render(next);
    try { window.gamepilotVisitor.storage().setItem(STORAGE_KEY, next.session_id); } catch { /* 浏览器可能禁用存储 */ }
    setServiceStatus(true);
    feedback(`${label}成功。当前第 ${next.turn} 回合。`);
  } catch (error) {
    feedback(error.message || "操作失败，请重试。", "error");
  } finally {
    busy = false;
    controls();
  }
}

$("create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const raw = $("seed-input").value.trim();
  if (raw && (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw)))) {
    feedback("随机种子必须是安全范围内的整数，或留空由服务生成。", "error");
    return;
  }
  const payload = { ...(raw ? { seed: Number(raw) } : {}),
    battle_count: Number($("battle-count-input").value) };
  void run("创建会话", () => request(API_ROOT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
});

$("load-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = $("session-input").value.trim();
  if (!id) {
    feedback("请先输入会话 ID。", "error");
    return;
  }
  void run("读取会话", () => request(`${API_ROOT}/${encodeURIComponent(id)}`));
});

for (const [button, action, label] of [
  ["attack-button", "attack", "攻击"],
  ["power-strike-button", "power_strike", "重击"],
  ["cast-spark-button", "cast_spark", "魔法飞弹"],
  ["cast-flame-button", "cast_flame", "火球"],
  ["cast-meteor-button", "cast_meteor", "陨石"],
  ["cast-heal-button", "cast_heal", "治疗魔法"],
  ["potion-button", "use_potion", "使用药水"],
  ["next-battle-button", "next_battle", "进入下一场"],
]) {
  $(button).addEventListener("click", () => {
    if (!snapshot) return;
    void run(label, () => request(`${API_ROOT}/${encodeURIComponent(snapshot.session_id)}/actions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    }));
  });
}

$("refresh-button").addEventListener("click", () => {
  if (!snapshot) return;
  void run("刷新状态", () => request(`${API_ROOT}/${encodeURIComponent(snapshot.session_id)}`));
});

async function initialize() {
  try {
    await request("/ready");
    setServiceStatus(true);
  } catch {
    setServiceStatus(false);
    feedback("服务尚未就绪。请确认启动命令仍在运行，然后刷新页面。", "error");
    return;
  }
  let previousId = null;
  try { previousId = window.gamepilotVisitor.storage().getItem(STORAGE_KEY); } catch { /* 浏览器可能禁用存储 */ }
  if (previousId) {
    $("session-input").value = previousId;
    await run("恢复上次会话", () => request(`${API_ROOT}/${encodeURIComponent(previousId)}`));
  }
}

void initialize();
