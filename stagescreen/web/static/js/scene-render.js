/* event-stage-screen – scene renderer shared by the kiosk render page and the editor.
 *
 * A scene (see stagescreen/domain/scene.py) is rendered into a "stage": a div
 * of the scene's base resolution in CSS pixels, scaled to its container.
 * Elements are absolutely positioned; x/y is the top-left corner of the
 * unrotated box, rotation is around the box centre, flipping and cropping
 * apply to the content inside the box.
 */
(function () {
  "use strict";

  const SR = (window.SceneRender = window.SceneRender || {});
  const GENERIC = ["sans-serif", "serif", "monospace"];

  // ------------------------------------------------------------------ time formatting
  const formatters = {};
  function parts(date, timeZone, locale) {
    const key = (timeZone || "") + "|" + locale;
    if (!formatters[key]) {
      const base = { hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" };
      const opts = (extra) => { const o = Object.assign({}, base, extra); if (timeZone) o.timeZone = timeZone; return o; };
      let num, names;
      try {
        num = new Intl.DateTimeFormat("en-GB", opts({}));
        names = new Intl.DateTimeFormat(locale, opts({ weekday: "long", month: "long" }));
      } catch (e) { // unknown time zone → local time
        num = new Intl.DateTimeFormat("en-GB", base);
        names = new Intl.DateTimeFormat(locale, Object.assign({}, base, { weekday: "long", month: "long" }));
      }
      formatters[key] = { num, names };
    }
    const f = formatters[key];
    const out = {};
    f.num.formatToParts(date).forEach((p) => { out[p.type] = p.value; });
    f.names.formatToParts(date).forEach((p) => { if (p.type === "weekday") out.weekdayName = p.value; if (p.type === "month") out.monthName = p.value; });
    if (out.hour === "24") out.hour = "00";
    return out;
  }

  const TOKENS = /yyyy|yy|MMMM|MMM|MM|M|dddd|ddd|dd|d|HH|H|hh|h|mm|m|ss|s|a|\[[^\]]*\]/g;
  SR.formatTime = function (date, format, timeZone, locale) {
    const p = parts(date, timeZone, locale || "en");
    const H = parseInt(p.hour, 10), h12 = H % 12 || 12;
    const map = {
      yyyy: p.year, yy: p.year.slice(-2), MMMM: p.monthName || "", MMM: (p.monthName || "").slice(0, 3), MM: p.month,
      M: String(parseInt(p.month, 10)), dddd: p.weekdayName || "", ddd: (p.weekdayName || "").slice(0, 3), dd: p.day,
      d: String(parseInt(p.day, 10)), HH: p.hour, H: String(H), hh: String(h12).padStart(2, "0"), h: String(h12),
      mm: p.minute, m: String(parseInt(p.minute, 10)), ss: p.second, s: String(parseInt(p.second, 10)), a: H < 12 ? "AM" : "PM",
    };
    return format.replace(TOKENS, (tok) => (tok[0] === "[" ? tok.slice(1, -1) : map[tok]));
  };
  SR.timeParts = function (date, timeZone) {
    const p = parts(date, timeZone, "en");
    return { h: parseInt(p.hour, 10), m: parseInt(p.minute, 10), s: parseInt(p.second, 10) };
  };

  // ------------------------------------------------------------------ helpers
  function fontStack(font) {
    if (GENERIC.includes(font)) return font;
    return `"${String(font).replace(/"/g, "")}", "DejaVu Sans", sans-serif`;
  }
  const H_ALIGN = { left: "flex-start", center: "center", right: "flex-end" };
  const V_ALIGN = { top: "flex-start", middle: "center", bottom: "flex-end" };

  SR.textStyle = textStyle;
  function textStyle(node, p) {
    node.style.fontFamily = fontStack(p.font);
    node.style.fontSize = p.size + "px";
    node.style.fontWeight = p.weight;
    node.style.fontStyle = p.italic ? "italic" : "normal";
    node.style.color = p.color;
    node.style.background = p.background;
    node.style.display = "flex";
    node.style.justifyContent = H_ALIGN[p.align];
    node.style.alignItems = V_ALIGN[p.valign];
    node.style.textAlign = p.align;
    node.style.padding = p.padding + "px";
    node.style.boxSizing = "border-box";
    node.style.textShadow = p.shadow ? `0 ${Math.max(1, p.size * 0.04)}px ${Math.max(2, p.size * 0.12)}px rgba(0,0,0,.85)` : "none";
    node.style.fontVariantNumeric = "tabular-nums";
  }

  // ------------------------------------------------------------------ element content
  function contentFor(el, ctx) {
    const p = el.props;
    const node = document.createElement("div");
    node.className = "sr-content";
    node.style.width = "100%";
    node.style.height = "100%";
    if (el.type === "color") {
      node.style.background = p.color;
      node.style.borderRadius = p.radius + "px";
      if (p.border_width > 0) node.style.border = `${p.border_width}px solid ${p.border_color}`;
      node.style.boxSizing = "border-box";
    } else if (el.type === "image") {
      if (p.media_id) {
        const img = document.createElement("img");
        img.src = ctx.mediaUrl(p.media_id);
        img.alt = "";
        img.draggable = false;
        img.style.cssText = `width:100%;height:100%;object-fit:${p.fit};display:block`;
        node.appendChild(img);
      } else if (ctx.editor) {
        node.className += " sr-placeholder";
        node.textContent = ctx.t ? ctx.t("No image selected") : "No image selected";
      }
    } else if (el.type === "text") {
      textStyle(node, p);
      node.style.lineHeight = p.line_height;
      node.style.textTransform = p.uppercase ? "uppercase" : "none";
      const span = document.createElement("span");
      span.style.whiteSpace = "pre-wrap";
      span.style.overflowWrap = "anywhere";
      span.textContent = p.text;
      node.appendChild(span);
    } else if (el.type === "erm") {
      node._erm = el;
      ctx.ermNodes.push(node);
      if (SR.renderErm) SR.renderErm(node, el, ctx);
    } else if (el.type === "clock") {
      if (p.style === "analog") {
        node.innerHTML = analogSvg(p);
        node.dataset.clock = "analog";
      } else {
        textStyle(node, p);
        node.style.whiteSpace = "pre";
        node.dataset.clock = "digital";
      }
      node._clock = p;
      ctx.clocks.push(node);
      updateClock(node, ctx);
    }
    return node;
  }

  // Colours end up in SVG markup: only accept the formats the server validates.
  const COLOR_RE = /^(#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|transparent)$/;
  const safeColor = (c) => (COLOR_RE.test(String(c)) ? c : "#000000");

  function analogSvg(raw) {
    const p = Object.assign({}, raw, {
      face_color: safeColor(raw.face_color), hand_color: safeColor(raw.hand_color),
      second_color: safeColor(raw.second_color), tick_color: safeColor(raw.tick_color),
    });
    let ticks = "";
    for (let i = 0; i < 60; i++) {
      const major = i % 5 === 0;
      ticks += `<line x1="50" y1="${major ? 6 : 7.5}" x2="50" y2="${major ? 13 : 10}" stroke="${p.tick_color}" stroke-width="${major ? 2 : 0.8}" stroke-linecap="round" transform="rotate(${i * 6} 50 50)"/>`;
    }
    return `<svg viewBox="0 0 100 100" width="100%" height="100%" preserveAspectRatio="xMidYMid meet" style="display:block">
      <circle cx="50" cy="50" r="48" fill="${p.face_color}"/>${ticks}
      <line class="h" x1="50" y1="54" x2="50" y2="27" stroke="${p.hand_color}" stroke-width="4" stroke-linecap="round"/>
      <line class="m" x1="50" y1="56" x2="50" y2="15" stroke="${p.hand_color}" stroke-width="2.6" stroke-linecap="round"/>
      ${p.show_seconds ? `<line class="s" x1="50" y1="60" x2="50" y2="11" stroke="${p.second_color}" stroke-width="1.1" stroke-linecap="round"/>` : ""}
      <circle cx="50" cy="50" r="2.6" fill="${p.show_seconds ? p.second_color : p.hand_color}"/></svg>`;
  }

  /** Clock time: ERM server time (default, falls back to the Pi clock) or the Pi clock. */
  function clockTime(p, ctx) {
    if (!SR.ermClock) return ctx.now();
    const c = SR.ermClock(ctx);
    return p.time_source === "system" ? c.pi : (c.erm !== null ? c.erm : c.pi);
  }

  function updateClock(node, ctx) {
    const p = node._clock;
    const now = new Date(clockTime(p, ctx));
    if (node.dataset.clock === "analog") {
      const t = SR.timeParts(now, ctx.timezone);
      const set = (cls, deg) => { const l = node.querySelector("." + cls); if (l) l.setAttribute("transform", `rotate(${deg} 50 50)`); };
      set("h", (t.h % 12) * 30 + t.m * 0.5);
      set("m", t.m * 6 + t.s * 0.1);
      set("s", t.s * 6);
    } else {
      const text = SR.formatTime(now, p.format, ctx.timezone, ctx.locale);
      if (node.textContent !== text) node.textContent = text;
    }
  }

  // ------------------------------------------------------------------ element box
  SR.applyGeometry = function (node, el) {
    node.style.left = el.x + "px";
    node.style.top = el.y + "px";
    node.style.width = el.w + "px";
    node.style.height = el.h + "px";
    node.style.transform = el.rotation ? `rotate(${el.rotation}deg)` : "";
    node.style.opacity = el.opacity;
    const flip = node.firstChild;
    flip.style.transform = (el.flip_h || el.flip_v) ? `scale(${el.flip_h ? -1 : 1}, ${el.flip_v ? -1 : 1})` : "";
    const c = el.crop, inner = flip.firstChild;
    const cw = el.w / (1 - c.left - c.right), ch = el.h / (1 - c.top - c.bottom);
    inner.style.left = (-c.left * cw) + "px";
    inner.style.top = (-c.top * ch) + "px";
    inner.style.width = cw + "px";
    inner.style.height = ch + "px";
  };

  function elementNode(el, ctx) {
    const node = document.createElement("div");
    node.className = "sr-el";
    node.dataset.id = el.id;
    node.style.position = "absolute";
    node.style.transformOrigin = "50% 50%";
    if (!el.visible) node.style.display = "none";
    const flip = document.createElement("div");
    flip.className = "sr-flip";
    flip.style.cssText = "position:absolute;inset:0;overflow:hidden";
    const inner = document.createElement("div");
    inner.className = "sr-inner";
    inner.style.position = "absolute";
    inner.appendChild(contentFor(el, ctx));
    flip.appendChild(inner);
    node.appendChild(flip);
    SR.applyGeometry(node, el);
    return node;
  }

  // ------------------------------------------------------------------ stage
  /**
   * Create a stage inside ``container``.
   * opts: mediaUrl(id), timezone, locale, now() (ms), editor (bool), t (translate)
   */
  SR.createStage = function (container, opts) {
    const ctx = {
      mediaUrl: opts.mediaUrl || ((id) => "/media/" + id),
      timezone: opts.timezone, locale: opts.locale || "en",
      now: opts.now || (() => Date.now()), editor: !!opts.editor, t: opts.t, clocks: [], ermNodes: [],
      erm: opts.erm || {},
    };
    const root = document.createElement("div");
    root.className = "sr-stage";
    root.style.cssText = "position:absolute;left:0;top:0;transform-origin:0 0;overflow:hidden";
    container.appendChild(root);
    let scene = null;
    const timer = setInterval(() => {
      ctx.clocks.forEach((n) => updateClock(n, ctx));
      ctx.ermNodes.forEach((n) => { if (n._tick) n._tick(); });
    }, 250);

    const stage = {
      root, ctx,
      get scene() { return scene; },
      render(next) {
        scene = next;
        ctx.clocks = [];
        ctx.ermNodes = [];
        root.innerHTML = "";
        root.style.width = scene.width + "px";
        root.style.height = scene.height + "px";
        root.style.background = scene.background;
        scene.elements.forEach((el) => root.appendChild(elementNode(el, ctx)));
      },
      /** Replace one element (e.g. after a property change). */
      patch(el) {
        const old = root.querySelector(`.sr-el[data-id="${CSS.escape(el.id)}"]`);
        ctx.clocks = ctx.clocks.filter((n) => !old || !old.contains(n));
        ctx.ermNodes = ctx.ermNodes.filter((n) => !old || !old.contains(n));
        const node = elementNode(el, ctx);
        if (old) old.replaceWith(node); else root.appendChild(node);
        return node;
      },
      /** New eventrentmanager data: refresh only the ERM elements and clocks. */
      setErm(erm) {
        ctx.erm = erm || {};
        if (SR.renderErm) ctx.ermNodes.forEach((n) => SR.renderErm(n, n._erm, ctx));
        ctx.clocks.forEach((n) => updateClock(n, ctx));
      },
      node(id) { return root.querySelector(`.sr-el[data-id="${CSS.escape(id)}"]`); },
      /** Scale the stage into a box of w×h CSS px; returns {scale, x, y, sx, sy}. */
      fit(w, h, scaling) {
        if (!scene) return null;
        let sx = w / scene.width, sy = h / scene.height;
        if (scaling !== "stretch") sx = sy = Math.min(sx, sy);
        const x = (w - scene.width * sx) / 2, y = (h - scene.height * sy) / 2;
        root.style.transform = `translate(${x}px, ${y}px) scale(${sx}, ${sy})`;
        return { scale: Math.min(sx, sy), sx, sy, x, y };
      },
      destroy() { clearInterval(timer); root.remove(); },
    };
    return stage;
  };
})();
