/* event-stage-screen – eventrentmanager elements for the scene renderer.
 *
 * Renders the "erm" element type from the Display API state
 * (GET /api/display/v1/state, see eventrentmanager docs/display-api.md and
 * docs/stagedisplay-api.md). Semantics follow the ERM browser client
 * (/display): countdown to current.endsAt with warn/critical thresholds and
 * "+m:ss" when overrunning, elapsed time without duration, "starts in" before
 * the program, delay from live.projectedDelaySeconds (|d| < 30 s = on time).
 * Time-dependent parts are recomputed locally on every tick using the ERM
 * server clock offset.
 */
(function () {
  "use strict";

  const SR = window.SceneRender;
  const LABELS = {
    en: { now: "Now", next: "Next", startsIn: "Starts in", finished: "Program finished", onTime: "On time", delay: "Delay",
          ahead: "Ahead", noStrobe: "⚠ NO STROBE", mics: "Microphones", handheld: "handheld", headset: "headset",
          offline: "offline", online: "online", in: "in", min: "min", noProgram: "No program assigned",
          trades: { general: "General", sound: "Sound", light: "Light", video: "Video", stage: "Stage" } },
    de: { now: "Jetzt", next: "Danach", startsIn: "Beginn in", finished: "Programm beendet", onTime: "im Plan", delay: "Verzug",
          ahead: "Vorsprung", noStrobe: "⚠ KEIN STROBOSKOP", mics: "Mikrofone", handheld: "Hand", headset: "Headset",
          offline: "offline", online: "online", in: "in", min: "min", noProgram: "Kein Minutenprogramm zugeordnet",
          trades: { general: "Allgemein", sound: "Ton", light: "Licht", video: "Video", stage: "Bühne" } },
  };
  // compatibility keys of ERM "notes" ↔ trade names in "tradeNotes"
  const LEGACY_TRADES = { Ton: "sound", Sound: "sound", Licht: "light", Light: "light", Video: "video", "Bühne": "stage", Stage: "stage" };
  const COLOR_RE = /^(#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|transparent)$/;
  const safeColor = (c, fallback) => (COLOR_RE.test(String(c)) ? c : fallback);

  let styleInjected = false;
  function injectStyle() {
    if (styleInjected) return;
    styleInjected = true;
    const st = document.createElement("style");
    st.textContent = "@keyframes sr-blink{50%{opacity:.35}} .sr-erm-urgent{animation:sr-blink 1s steps(2,start) infinite}" +
      "@media (prefers-reduced-motion: reduce){.sr-erm-urgent{animation:none}}";
    document.head.appendChild(st);
  }

  const pad = (n) => String(n).padStart(2, "0");
  function dur(sec) {
    sec = Math.abs(Math.round(sec));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h > 0 ? h + ":" + pad(m) : m) + ":" + pad(s);
  }
  const ms = (iso) => (iso ? Date.parse(iso) : NaN);

  // ------------------------------------------------------------------ time
  /** Pi clock (corrects the browser clock in remote editors) and ERM clock. */
  SR.ermClock = function (ctx) {
    const e = ctx.erm || {};
    const pi = Date.now() + (e.skew || 0);
    const online = e.status === "online" && !SR.ermStale(ctx);
    return { pi, erm: online ? pi + (e.offset_ms || 0) : null, ermOrPi: pi + (e.data ? (e.offset_ms || 0) : 0) };
  };
  SR.ermStale = function (ctx) {
    const e = ctx.erm || {};
    if (e.sample) return false;
    if (!e.received_at_ms) return true;
    const pi = Date.now() + (e.skew || 0);
    return pi - e.received_at_ms > Math.max(15000, (e.poll_interval || 3) * 3000);
  };

  // ------------------------------------------------------------------ helpers
  function L(ctx) { return LABELS[(ctx.locale || "en").slice(0, 2)] || LABELS.en; }
  function hm(iso, ctx) { return iso ? SR.formatTime(new Date(iso), "HH:mm", ctx.timezone, ctx.locale) : "–"; }
  function el(tag, text, css) {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    if (css) n.style.cssText = css;
    return n;
  }
  function columnLayout(node, p) {
    node.style.flexDirection = "column";
    node.style.alignItems = { left: "flex-start", center: "center", right: "flex-end" }[p.align];
    node.style.justifyContent = { top: "flex-start", middle: "center", bottom: "flex-end" }[p.valign];
    node.style.gap = (p.size * 0.3) + "px";
  }
  function projectedDelay(rs, ctx) {
    const live = rs.live;
    let d = live.projectedDelaySeconds || 0;
    if (live.tracking && live.overrunSeconds > 0 && ctx.erm.received_at_ms) {
      d += Math.max(0, Math.floor((SR.ermClock(ctx).pi - ctx.erm.received_at_ms) / 1000));
    }
    return d;
  }
  function notesFor(item, rs, p, ctx) {
    const options = (ctx.erm.data && ctx.erm.data.display && ctx.erm.data.display.options) || {};
    const all = ["general", "sound", "light", "video", "stage", "other"];
    const keys = p.notes_filter === "all" ? all : p.notes_filter === "custom" ? p.notes_keys : (options.notes || ["general", "stage"]);
    const t = L(ctx).trades, out = [];
    const notes = item.notes || {};
    if (keys.includes("general") && notes.general) out.push({ label: t.general, text: notes.general, color: null });
    if (Array.isArray(item.tradeNotes)) {
      item.tradeNotes.forEach((tn) => {
        const legacy = LEGACY_TRADES[tn.trade];
        if (p.notes_filter === "all" || (legacy ? keys.includes(legacy) : keys.includes("other"))) {
          out.push({ label: tn.trade, text: tn.text, color: tn.color });
        }
      });
    } else {
      ["sound", "light", "video", "stage"].forEach((k) => { if (keys.includes(k) && notes[k]) out.push({ label: t[k], text: notes[k], color: null }); });
    }
    return out;
  }

  // ------------------------------------------------------------------ fields
  const FIELDS = {
    countdown(node, p, rs, ctx) {
      node.style.whiteSpace = "pre";
      if (!rs) return;
      const th = (ctx.erm.data.display && ctx.erm.data.display.thresholds) || { warnSeconds: 120, criticalSeconds: 30 };
      const c = rs.current;
      node._tick = () => {
        const now = SR.ermClock(ctx).ermOrPi;
        let text = "", color = p.color;
        if (rs.live.state === "finished") text = p.finished_text || "";
        else if (c && c.endsAt) {
          const rem = (ms(c.endsAt) - now) / 1000;
          text = rem < 0 ? "+" + dur(-rem) : dur(rem);
          if (rem < 0 || rem <= th.criticalSeconds) color = p.critical_color;
          else if (rem <= th.warnSeconds) color = p.warn_color;
        } else if (c && c.startsAt) text = dur((now - ms(c.startsAt)) / 1000);
        else if (!c && p.show_pending && rs.program && rs.program.currentStart) text = dur(Math.max(0, (ms(rs.program.currentStart) - now) / 1000));
        if (node.textContent !== text) node.textContent = text;
        node.style.color = color;
      };
    },
    progress(node, p, rs, ctx) {
      node.style.padding = "0";
      node.style.background = p.track_color;
      node.style.borderRadius = p.radius + "px";
      node.style.overflow = "hidden";
      node.style.justifyContent = "flex-start";
      node.style.alignItems = "stretch";
      const bar = el("div", null, `background:${safeColor(p.bar_color, "#22c55e")};width:0;height:100%`);
      node.appendChild(bar);
      if (!rs || !rs.current || !rs.current.endsAt) return;
      const a = ms(rs.current.startsAt), b = ms(rs.current.endsAt);
      node._tick = () => {
        const now = SR.ermClock(ctx).ermOrPi;
        bar.style.width = Math.max(0, Math.min(100, ((now - a) / Math.max(1, b - a)) * 100)) + "%";
      };
    },
    current(node, p, rs, ctx) {
      if (!rs) return;
      const lab = L(ctx);
      if (rs.live.state === "finished") { node.textContent = lab.finished; return; }
      const c = rs.current;
      if (!c) { if (p.show_pending) node.textContent = (p.label ? p.label + " " : "") + lab.startsIn; return; }
      const pos = p.show_position ? `${c.position}/${rs.program.itemCount} · ` : "";
      node.textContent = (p.label ? p.label + " " : "") + pos + c.name;
    },
    description(node, p, rs) {
      node.style.whiteSpace = "pre-line";
      if (rs && rs.current && rs.current.description && rs.live.state !== "finished") node.textContent = rs.current.description;
    },
    notes(node, p, rs, ctx) {
      columnLayout(node, p);
      if (!rs || !rs.current || rs.live.state === "finished") return;
      notesFor(rs.current, rs, p, ctx).forEach((n) => {
        const box = el("div", null, `background:${safeColor(p.note_background, "transparent")};padding:${p.size * 0.15}px ${p.size * 0.35}px;border-radius:${p.size * 0.25}px;white-space:pre-line;text-align:${p.align}`);
        if (p.show_labels) box.appendChild(el("div", n.label, `font-size:.5em;letter-spacing:.1em;text-transform:uppercase;font-weight:700;color:${safeColor(n.color, "#f5b43c")}`));
        box.appendChild(el("div", n.text));
        node.appendChild(box);
      });
    },
    next(node, p, rs, ctx) {
      node.style.whiteSpace = "pre";
      if (!rs || !rs.next || rs.live.state === "finished") return;
      const n = rs.next, lab = L(ctx);
      const head = (p.label ? p.label + " " : "") + n.name + (p.show_time ? ` · ${hm(n.startsAt, ctx)}` : "");
      node._tick = () => {
        let text = head;
        if (p.show_in) text += ` (${lab.in} ${dur(Math.max(0, (ms(n.startsAt) - SR.ermClock(ctx).ermOrPi) / 1000))})`;
        if (node.textContent !== text) node.textContent = text;
      };
    },
    delay(node, p, rs, ctx) {
      if (!rs || rs.live.state === "finished") return;
      const lab = L(ctx);
      node._tick = () => {
        const d = projectedDelay(rs, ctx);
        let text, color;
        if (Math.abs(d) < 30) { text = lab.onTime; color = p.on_time_color; }
        else if (d > 0) { text = `${lab.delay} ${dur(d)}`; color = p.late_color; }
        else { text = `${lab.ahead} ${dur(-d)}`; color = p.early_color; }
        if (node.textContent !== text) node.textContent = text;
        node.style.color = color;
      };
    },
    messages(node, p, rs) {
      columnLayout(node, p);
      node.style.alignItems = "stretch";
      if (!rs || !Array.isArray(rs.messages)) return;
      injectStyle();
      rs.messages.slice(-Math.round(p.max_messages)).forEach((m) => {
        const color = safeColor(m.urgent ? p.urgent_color : p.message_color, "#60a5fa");
        const box = el("div", (m.urgent ? "⚠ " : "") + m.text,
          `border:${Math.max(2, p.size * 0.06)}px solid ${color};background:${color}2e;border-radius:${p.size * 0.3}px;padding:${p.size * 0.2}px ${p.size * 0.4}px;white-space:pre-line;text-align:${p.align}`);
        if (m.urgent) box.className = "sr-erm-urgent";
        node.appendChild(box);
      });
    },
    nostrobe(node, p, rs, ctx) {
      const flagged = rs && ((rs.event && rs.event.noStrobe) || (rs.current && rs.current.noStrobe));
      if (!flagged) { node.style.visibility = "hidden"; return; }
      node.textContent = p.label || L(ctx).noStrobe;
    },
    itemlist(node, p, rs, ctx) {
      columnLayout(node, p);
      node.style.alignItems = "stretch";
      node.style.gap = (p.size * 0.15) + "px";
      if (!rs || !Array.isArray(rs.items)) return;
      let items = rs.items.filter((i) => p.show_done || i.status !== "done");
      items = items.slice(0, Math.round(p.max_rows));
      const hl = safeColor(p.highlight_color, "#2fb4e8");
      items.forEach((i) => {
        const row = el("div", null, `display:flex;gap:${p.size * 0.6}px;align-items:baseline;padding:${p.size * 0.15}px ${p.size * 0.3}px;border-radius:${p.size * 0.2}px`);
        if (i.status === "current") { row.style.background = hl + "33"; row.style.boxShadow = `inset ${p.size * 0.15}px 0 0 ${hl}`; }
        if (i.status === "done") row.style.opacity = ".45";
        row.appendChild(el("span", hm(i.projectedStart, ctx), "font-variant-numeric:tabular-nums;opacity:.8"));
        row.appendChild(el("span", i.name, "flex:1;text-align:left"));
        if (p.show_duration && i.durationMinutes) row.appendChild(el("span", `${i.durationMinutes} ${L(ctx).min}`, "opacity:.6;font-size:.8em"));
        node.appendChild(row);
      });
    },
    mics(node, p, rs, ctx) {
      if (!rs || !rs.program || (!rs.program.handheldMicCount && !rs.program.headsetMicCount)) return;
      const lab = L(ctx);
      node.textContent = `${lab.mics}: ${rs.program.handheldMicCount || 0} ${lab.handheld} · ${rs.program.headsetMicCount || 0} ${lab.headset}`;
    },
    program(node, p, rs, ctx) {
      const display = (ctx.erm.data && ctx.erm.data.display) || {};
      if (!rs) { node.textContent = display.name ? `${display.name} – ${L(ctx).noProgram}` : ""; return; }
      const values = {
        program: rs.program.name || "", room: rs.program.room || "", event: (rs.event && rs.event.name) || "",
        start: hm(rs.program.currentStart, ctx), end: hm(rs.program.projectedEnd, ctx), items: String(rs.program.itemCount || ""),
        display: display.name || "",
      };
      const text = p.template.replace(/\{(\w+)\}/g, (m, k) => (k in values ? values[k] : m));
      node.textContent = text.replace(/^[\s·\-–|]+|[\s·\-–|]+$/g, "").replace(/(\s·\s)+(?=\s·|$)/g, "");
    },
    status(node, p, rs, ctx) {
      node._tick = () => {
        const offline = !ctx.erm.sample && (ctx.erm.status !== "online" || SR.ermStale(ctx));
        const lab = L(ctx);
        const text = offline ? (p.label || lab.offline) : (p.show_online ? lab.online : "");
        if (node.textContent !== text) node.textContent = text;
        node.style.visibility = text ? "visible" : "hidden";
      };
    },
  };

  /** Fill an ERM element's content node from ctx.erm (called on render and on data updates). */
  SR.renderErm = function (node, elDef, ctx) {
    node.innerHTML = "";
    node.style.visibility = "visible";
    node._tick = null;
    const p = elDef.props;
    SR.textStyle(node, p);
    const e = ctx.erm || {};
    const rs = e.data ? e.data.runSheet : null;
    if (!e.data && ctx.editor) {
      node.style.opacity = ".5";
      node.textContent = "ERM: " + p.field;
      return;
    }
    const fn = FIELDS[p.field];
    if (fn && e.data) fn(node, p, rs, ctx);
    if (node._tick) node._tick();
  };

  // ------------------------------------------------------------------ sample data (editor preview)
  SR.sampleErm = function (locale) {
    const de = (locale || "en").startsWith("de");
    const now = Date.now();
    const iso = (offsetS) => new Date(now + offsetS * 1000).toISOString();
    const item = (pos, name, startS, durMin, status, extra) => Object.assign({
      id: "s" + pos, position: pos, name, description: null, notes: { general: null, sound: null, light: null, video: null, stage: null },
      tradeNotes: [], noStrobe: false, durationMinutes: durMin, status,
      plannedStart: iso(startS), plannedEnd: iso(startS + durMin * 60), currentStart: iso(startS), projectedStart: iso(startS),
      projectedEnd: iso(startS + durMin * 60), actualStart: status === "done" || status === "current" ? iso(startS) : null,
    }, extra || {});
    const items = [
      item(1, de ? "Einlass" : "Doors open", -1260, 15, "done"),
      item(2, de ? "Begrüßung" : "Welcome", -360, 8, "current", {
        description: de ? "Moderation begrüßt die Gäste" : "Host welcomes the guests",
        notes: { general: de ? "Pult links" : "Lectern left", sound: "HH 1", light: null, video: null, stage: de ? "Banner fährt ein" : "Banner in" },
        tradeNotes: [{ tradeId: "t1", trade: de ? "Ton" : "Sound", color: "#3b82f6", text: "HH 1" },
                     { tradeId: "t2", trade: de ? "Bühne" : "Stage", color: "#a855f7", text: de ? "Banner fährt ein" : "Banner in" }],
        noStrobe: true }),
      item(3, de ? "Keynote" : "Keynote", 120, 25, "next"),
      item(4, de ? "Pause" : "Break", 1620, 20, "upcoming"),
      item(5, "Award Show", 2820, 45, "upcoming"),
    ];
    const cur = items[1], nxt = items[2];
    const block = (i, startS, durS) => Object.assign({}, i, { durationSeconds: durS, startsAt: iso(startS), endsAt: iso(startS + durS),
      elapsedSeconds: -startS, remainingSeconds: startS + durS });
    return {
      sample: true, status: "online", offset_ms: 0, received_at_ms: now, poll_interval: 3, skew: 0,
      data: {
        display: { name: de ? "Bühnenmonitor" : "Stage monitor", role: "stage", options: { notes: ["general", "stage"] },
                   thresholds: { warnSeconds: 120, criticalSeconds: 30 } },
        runSheet: {
          apiVersion: 1, serverTime: iso(0), revision: "sample",
          event: { name: de ? "Firmengala" : "Company gala", noStrobe: false },
          program: { id: "p", name: de ? "Abendprogramm" : "Evening programme", room: de ? "Großer Saal" : "Main hall",
                     plannedStart: iso(-1260), currentStart: iso(-1260), plannedEnd: iso(5520), projectedEnd: iso(5600),
                     handheldMicCount: 4, headsetMicCount: 2, itemCount: items.length },
          trades: [],
          live: { state: "running", tracking: true, delaySeconds: 90, projectedDelaySeconds: 90, overrunSeconds: 0, finishedAt: null, canControl: false },
          current: block(cur, -360, 480), next: block(nxt, 120, 1500), items,
          messages: [{ id: "m1", text: de ? "Redner kommt 5 Minuten später" : "Speaker arrives 5 minutes late", urgent: false, stage: true, foh: true }],
        },
      },
    };
  };
})();
