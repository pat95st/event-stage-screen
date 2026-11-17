/* event-stage-screen – shared front-end helpers: i18n, API, toasts, live stream, status bar. */
(function () {
  "use strict";

  const ESS = (window.ESS = window.ESS || {});

  // ------------------------------------------------------------------ i18n
  // English is the source language; window.ESS_I18N holds the catalogue for the active UI language.
  ESS.lang = window.ESS_LANG || "en";
  ESS.t = function (text, params) {
    let s = (window.ESS_I18N && window.ESS_I18N[text]) || text;
    if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (params[k] !== undefined ? params[k] : m));
    return s;
  };
  const T = ESS.t;

  // ------------------------------------------------------------------ API
  ESS.api = async function (path, data, method, extraHeaders) {
    const headers = Object.assign({ "X-ESS": "1" }, extraHeaders || {});
    const opts = { method: method || (data !== undefined ? "POST" : "GET"), headers, credentials: "same-origin" };
    if (data !== undefined) {
      if (data instanceof Blob || data instanceof ArrayBuffer) {
        opts.headers["Content-Type"] = "application/octet-stream";
        opts.body = data;
      } else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(data);
      }
    }
    const res = await fetch(path, opts);
    let payload = {};
    try { payload = await res.json(); } catch (e) { /* ignore */ }
    if (res.status === 401 && payload.redirect) { window.location.href = payload.redirect; throw new Error(T("Not logged in")); }
    if (!res.ok || payload.ok === false) throw new Error(payload.error || ("HTTP " + res.status));
    return payload.result !== undefined ? payload.result : payload;
  };

  ESS.toast = function (message, kind) {
    let wrap = document.querySelector(".toast-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.className = "toast-wrap"; document.body.appendChild(wrap); }
    const el = document.createElement("div");
    el.className = "ess-toast " + (kind || "");
    el.setAttribute("role", kind === "err" ? "alert" : "status");
    el.textContent = message;
    wrap.appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 7000 : 3500);
  };

  ESS.run = async function (promiseFactory, okMessage) {
    try {
      const r = await promiseFactory();
      if (okMessage) ESS.toast(typeof okMessage === "function" ? okMessage(r) : okMessage, "ok");
      return r;
    } catch (e) {
      ESS.toast(e.message, "err");
      throw e;
    }
  };

  ESS.confirm = function (message) { return window.confirm(message); };

  ESS.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  };

  ESS.fmtUptime = function (s) {
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + " d " : "") + h + " h " + m + " min";
  };

  // ------------------------------------------------------------------ live stream (SSE)
  const listeners = {};
  ESS.on = function (event, fn) { (listeners[event] = listeners[event] || []).push(fn); };
  function emit(event, data) { (listeners[event] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); }
  ESS.emit = emit;

  let es = null;
  ESS.connect = function () {
    if (es) return;
    es = new EventSource("/api/stream");
    ["status", "changed", "log"].forEach((ev) => es.addEventListener(ev, (m) => emit(ev, JSON.parse(m.data))));
    es.onopen = () => emit("stream", { connected: true });
    es.onerror = () => emit("stream", { connected: false });
  };

  // ------------------------------------------------------------------ status bar
  function setPill(id, state, text, title) {
    const el = document.getElementById(id);
    if (!el) return;
    el.querySelector(".ess-dot").className = "ess-dot " + (state || "");
    el.querySelector(".txt").textContent = text;
    el.title = title || "";
  }
  ESS.setPill = setPill;

  ESS.renderStatus = function (st) {
    const temp = st.temperature_c;
    const tempState = temp == null ? "" : temp >= 80 ? "err" : temp >= 70 ? "warn" : "ok";
    const throttle = st.throttled_now ? "err" : (st.throttled_flags.length ? "warn" : tempState);
    setPill("st-sys", throttle,
      `${Math.round(st.cpu_percent)}% · ${temp == null ? "–" : temp.toFixed(0) + " °C"}`,
      [T("CPU load"), T("Temperature")].join(" / ") + (st.throttled_flags.length ? "\n" + st.throttled_flags.join("\n") : ""));
  };

  ESS.renderOutputPills = function (outputs) {
    outputs.forEach((o) => {
      const state = !o.enabled ? "" : o.connected ? "ok" : "warn";
      setPill("st-out" + o.id, state, o.name, o.connected ? T("Monitor connected") : T("No monitor connected"));
    });
  };

  ESS.renderErmPill = function (list) {
    const configured = list.filter((o) => o.status !== "unconfigured");
    if (!configured.length) { setPill("st-erm", "", "ERM", T("eventrentmanager not configured")); return; }
    const bad = configured.filter((o) => o.status === "offline" || o.status === "unauthorized");
    const ok = configured.every((o) => o.status === "online");
    setPill("st-erm", bad.length ? "err" : ok ? "ok" : "warn", "ERM",
      configured.map((o) => `${T("Output {n}", { n: o.id })}: ${o.display_name || o.status}${o.message ? " – " + o.message : ""}`).join("\n"));
  };

  ESS.refreshStatus = async function () {
    try {
      const d = await ESS.api("/api/dashboard");
      ESS.renderStatus(d.status);
      ESS.renderOutputPills(d.outputs);
      ESS.renderErmPill(d.erm);
      emit("dashboard", d);
    } catch (e) { /* status bar is best effort */ }
  };

  ESS.on("status", (st) => ESS.renderStatus(st));
  let refreshTimer = null;
  ESS.on("changed", () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(ESS.refreshStatus, 150); });

  document.addEventListener("DOMContentLoaded", () => {
    const logout = document.getElementById("btn-logout");
    const lang = document.getElementById("lang-select");
    if (lang) lang.addEventListener("change", async () => {
      try { await ESS.api("/api/ui/language", { language: lang.value }); window.location.reload(); }
      catch (e) { ESS.toast(e.message, "err"); }
    });
    if (logout) logout.addEventListener("click", async (e) => {
      e.preventDefault();
      await ESS.api("/api/auth/logout", {});
      window.location.href = "/login";
    });
    if (document.body.dataset.authed === "1") {
      ESS.connect();
      ESS.refreshStatus();
      setInterval(ESS.refreshStatus, 15000);
    }
  });
})();
