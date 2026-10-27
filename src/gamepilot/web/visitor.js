"use strict";

// sessionStorage 按标签页保存身份；刷新沿用，关闭标签页后浏览器自动丢弃。
(() => {
  const VISITOR_KEY = "gamepilot:preview-visitor-id";
  const VISITOR_API = "/api/v1/preview-visitor";
  const pageId = crypto.randomUUID();
  let visitorId;
  try { visitorId = sessionStorage.getItem(VISITOR_KEY); } catch { /* 禁用存储时只保留当前页面 */ }
  if (!visitorId) {
    visitorId = crypto.randomUUID();
    try { sessionStorage.setItem(VISITOR_KEY, visitorId); } catch { /* 当前页面仍可使用 */ }
  }
  let previewEnabled = false;
  let heartbeat = null;

  async function open() {
    const response = await fetch(`${VISITOR_API}/open`, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visitor_id: visitorId, page_id: pageId }),
    });
    if (!response.ok) throw new Error("无法创建预览访客会话");
    previewEnabled = (await response.json()).preview_enabled === true;
    if (previewEnabled) {
      // 升级前版本曾把 ID 放在 localStorage；预览模式不再读取它们。
      try {
        localStorage.removeItem("gamepilot:last-session-id");
        localStorage.removeItem("gamepilot:last-agent-run-id");
      } catch { /* 浏览器可能禁用存储 */ }
      if (heartbeat === null) heartbeat = setInterval(() => void touch(), 30_000);
    }
  }

  let registration = open();

  function headers(existing) {
    const result = new Headers(existing || {});
    if (previewEnabled) {
      result.set("X-GamePilot-Visitor", visitorId);
      result.set("X-GamePilot-Page", pageId);
    }
    return result;
  }

  async function touch() {
    try {
      await registration;
      if (previewEnabled) {
        await fetch(`${VISITOR_API}/touch`, { method: "POST", cache: "no-store", headers: headers() });
      }
    } catch { /* 网络不可用时由服务端过期机制兜底 */ }
  }

  window.gamepilotVisitor = {
    get ready() { return registration; },
    isPreview: () => previewEnabled,
    storage: () => previewEnabled ? sessionStorage : localStorage,
    async fetch(path, options = {}) {
      await registration;
      return fetch(path, { ...options, headers: headers(options.headers) });
    },
  };

  window.addEventListener("pagehide", () => {
    if (heartbeat !== null) clearInterval(heartbeat);
    heartbeat = null;
    if (!previewEnabled) return;
    const body = new Blob([JSON.stringify({ visitor_id: visitorId, page_id: pageId })],
      { type: "application/json" });
    navigator.sendBeacon(`${VISITOR_API}/close`, body);
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) registration = open();
  });
})();
