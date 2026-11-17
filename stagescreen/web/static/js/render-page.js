/* event-stage-screen – kiosk render page for one HDMI output.
 *
 * Loads /api/render/<n>/state, renders the live scene full screen and follows
 * changes via Server-Sent Events. Switching scenes uses the target scene's
 * transition (cut or fade); updates of the same scene are applied in place.
 * Without a live scene the splash screen is shown.
 */
(function () {
  "use strict";

  const output = window.ESS_OUTPUT;
  const params = new URLSearchParams(location.search);
  const token = params.get("k") || "";
  const q = token ? "?k=" + encodeURIComponent(token) : "";
  const frame = document.getElementById("frame");
  const splash = document.getElementById("splash");
  document.getElementById("fonts-css").href = "/api/fonts.css" + q;

  let state = null;
  let current = null; // {layer, stage, sceneId, json}
  let lastJson = "";
  let erm = {};

  /** ERM data plus the skew between this browser's clock and the Pi clock. */
  function withSkew(e) {
    return Object.assign({}, e, { skew: e && e.now_ms ? e.now_ms - Date.now() : 0 });
  }

  const mediaUrl = (id) => `/media/${encodeURIComponent(id)}${q}`;

  // ------------------------------------------------------------------ layout
  function frameBox() {
    const vw = window.innerWidth, vh = window.innerHeight;
    const m = state && state.mirror;
    const rot = m ? m.rotation : 0;
    const swap = rot === 90 || rot === 270;
    const w = swap ? vh : vw, h = swap ? vw : vh;
    frame.style.width = w + "px";
    frame.style.height = h + "px";
    const flip = m ? `scale(${m.flip_h ? -1 : 1}, ${m.flip_v ? -1 : 1})` : "";
    frame.style.transform = `translate(-50%, -50%) rotate(${rot}deg) ${flip}`;
    return { w, h };
  }

  function layout() {
    const box = frameBox();
    if (current && current.stage.scene) {
      const scaling = state.mirror ? state.mirror.scaling : current.stage.scene.scaling;
      current.stage.fit(box.w, box.h, scaling);
    }
    return box;
  }
  window.addEventListener("resize", layout);

  // ------------------------------------------------------------------ splash
  function showSplash(st) {
    const mode = st.splash.mode;
    splash.innerHTML = "";
    if (mode !== "black") {
      if (st.splash.logo) {
        const img = document.createElement("img");
        img.src = mediaUrl(st.splash.logo);
        img.alt = "";
        splash.appendChild(img);
      }
      if (mode === "info") {
        const add = (cls, text) => { const d = document.createElement("div"); d.className = cls; d.textContent = text; splash.appendChild(d); };
        add("host", st.hostname);
        const port = st.port && st.port !== 80 ? ":" + st.port : "";
        add("addr", (st.ip_addresses.length ? st.ip_addresses : ["–"]).map((ip) => ip + port).join("   "));
        add("addr", `${st.hostname}.local${port}`);
        add("out", st.output_name);
      }
    }
    splash.style.display = "flex";
  }

  // ------------------------------------------------------------------ scenes
  function newLayer(scene) {
    const layer = document.createElement("div");
    layer.className = "layer";
    frame.appendChild(layer);
    const stage = SceneRender.createStage(layer, { mediaUrl, timezone: state.timezone, locale: state.locale || "en", erm,
      ndiMode: "stream", ndiQuery: token ? "&k=" + encodeURIComponent(token) : "" });
    stage.render(scene);
    return { layer, stage };
  }

  function removeLayer(entry) {
    if (!entry) return;
    entry.stage.destroy();
    entry.layer.remove();
  }

  function apply(st) {
    state = st;
    if (!st.enabled || !st.scene) {
      removeLayer(current);
      current = null;
      if (st.enabled) showSplash(st); else { splash.innerHTML = ""; splash.style.display = "none"; }
      layout();
      return;
    }
    splash.style.display = "none";
    const json = JSON.stringify(st.scene);
    if (current && current.sceneId === st.scene_id) {
      if (current.json !== json) { current.stage.render(st.scene); current.json = json; }
      layout();
      return;
    }
    const prev = current;
    const next = newLayer(st.scene);
    current = Object.assign(next, { sceneId: st.scene_id, json });
    layout();
    const tr = st.scene.transition;
    if (prev && tr.type === "fade" && tr.duration_ms > 0) {
      next.layer.classList.add("fading-in");
      next.layer.style.transition = `opacity ${tr.duration_ms}ms ease-in-out`;
      requestAnimationFrame(() => requestAnimationFrame(() => next.layer.classList.remove("fading-in")));
      setTimeout(() => removeLayer(prev), tr.duration_ms + 50);
    } else {
      removeLayer(prev);
    }
  }

  // ------------------------------------------------------------------ data
  async function load() {
    try {
      const res = await fetch(`/api/render/${output}/state${q}`, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const st = (await res.json()).result;
      erm = withSkew(st.erm);
      const rest = Object.assign({}, st, { erm: null });
      const json = JSON.stringify(rest);
      if (json !== lastJson) { lastJson = json; apply(st); }
      else if (current) current.stage.setErm(erm);
    } catch (e) {
      console.warn("render state unavailable", e);
    }
  }

  let pending = null;
  function scheduleLoad() { clearTimeout(pending); pending = setTimeout(load, 80); }

  let ermPending = null;
  async function loadErm() {
    try {
      const res = await fetch(`/api/render/${output}/erm${q}`, { cache: "no-store" });
      if (!res.ok) return;
      erm = withSkew((await res.json()).result);
      if (current) current.stage.setErm(erm);
    } catch (e) { /* keep showing the last state */ }
  }

  function connect() {
    const es = new EventSource("/api/render/stream" + q);
    es.addEventListener("changed", (m) => {
      const data = JSON.parse(m.data);
      if (data.topic === "erm") { clearTimeout(ermPending); ermPending = setTimeout(loadErm, 50); return; }
      if (data.topic === "media") document.getElementById("fonts-css").href = "/api/fonts.css" + q + (q ? "&" : "?") + "t=" + Date.now();
      scheduleLoad();
    });
    es.onopen = scheduleLoad; // resync after reconnects (e.g. service restart)
  }

  load();
  connect();
  setInterval(load, 30000); // fallback if events are lost
})();
