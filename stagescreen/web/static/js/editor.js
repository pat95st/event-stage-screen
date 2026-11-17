/* event-stage-screen – scene editor (OBS-like): canvas with handles, layers, properties.
 *
 * The editor works on a local copy of the scene. Changes are autosaved as the
 * scene's draft; "Apply" makes the draft live. With "live editing" enabled,
 * every change is published immediately. Rendering uses SceneRender, the same
 * engine as the kiosk outputs (WYSIWYG).
 */
(function () {
  "use strict";

  const T = ESS.t, esc = ESS.esc;
  const $ = (id) => document.getElementById(id);
  const canvas = $("canvas"), host = $("stage-host"), overlay = $("overlay");
  const PAD = 24;

  const E = {
    list: null,          // ListScenes result
    meta: null,          // open scene: {id, output_id, name, active, ...}
    scene: null,         // working copy (scene document)
    hasDraft: false,
    selected: null,      // element id
    history: [], future: [],
    saveTimer: null, saving: false, saveQueued: false,
    fonts: { bundled: [], uploaded: [], system: [] },
    media: [],
    view: null,          // {scale, x, y}
    drag: null,
  };
  const TYPE_ICON = { text: "T", image: "▣", clock: "◷", color: "■" };
  const TYPE_LABEL = { text: T("Text"), image: T("Image"), clock: T("Clock"), color: T("Colour area") };

  let stage = SceneRender.createStage(host, { editor: true, t: T, locale: ESS.lang });

  // ================================================================== helpers
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const rid = () => "el_" + Math.random().toString(16).slice(2, 12).padEnd(10, "0");
  const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;
  const el = (id) => (E.scene ? E.scene.elements.find((e) => e.id === id) : null);
  const sel = () => (E.selected ? el(E.selected) : null);
  function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
  function setPath(obj, path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    keys.reduce((o, k) => o[k], obj)[last] = value;
  }

  // ================================================================== view / fit
  function fitView() {
    if (!E.scene) return;
    const r = host.getBoundingClientRect();
    E.view = stage.fit(r.width, r.height, "letterbox");
    drawOverlay();
  }
  new ResizeObserver(fitView).observe(canvas);

  function toStage(clientX, clientY) {
    const r = host.getBoundingClientRect();
    return { x: (clientX - r.left - E.view.x) / E.view.scale, y: (clientY - r.top - E.view.y) / E.view.scale };
  }

  // ================================================================== rendering
  function renderStage() {
    if (!E.scene) return;
    stage.render(E.scene);
    markLocked();
    fitView();
  }
  function markLocked() {
    E.scene.elements.forEach((e) => { const n = stage.node(e.id); if (n) n.classList.toggle("locked", e.locked); });
  }
  function patch(e, geometryOnly) {
    const n = stage.node(e.id);
    if (geometryOnly && n) SceneRender.applyGeometry(n, e);
    else { const node = stage.patch(e); node.classList.toggle("locked", e.locked); if (!e.visible) node.style.display = "none"; }
    drawOverlay();
  }

  function drawOverlay(guides) {
    overlay.innerHTML = "";
    const e = sel();
    if (!e || !E.view || !e.visible) return;
    const s = E.view.scale;
    const box = document.createElement("div");
    box.className = "sel-box" + (e.locked ? " locked" : "");
    box.style.left = (PAD + E.view.x + e.x * s) + "px";
    box.style.top = (PAD + E.view.y + e.y * s) + "px";
    box.style.width = e.w * s + "px";
    box.style.height = e.h * s + "px";
    box.style.transform = `rotate(${e.rotation}deg)`;
    box.dataset.role = "move";
    if (!e.locked) {
      const pos = { nw: [0, 0], n: [50, 0], ne: [100, 0], e: [100, 50], se: [100, 100], s: [50, 100], sw: [0, 100], w: [0, 50] };
      Object.entries(pos).forEach(([h, [x, y]]) => {
        const d = document.createElement("div");
        d.className = "hd"; d.dataset.h = h; d.style.left = x + "%"; d.style.top = y + "%";
        box.appendChild(d);
      });
      const line = document.createElement("div"); line.className = "rot-line"; box.appendChild(line);
      const r = document.createElement("div"); r.className = "hd"; r.dataset.h = "rot"; r.style.left = "50%"; r.style.top = "-28px";
      box.appendChild(r);
    }
    overlay.appendChild(box);
    (guides || []).forEach((g) => {
      const d = document.createElement("div");
      if (g.x !== undefined) { d.className = "guide v"; d.style.left = (PAD + E.view.x + g.x * s) + "px"; }
      else { d.className = "guide h"; d.style.top = (PAD + E.view.y + g.y * s) + "px"; }
      overlay.appendChild(d);
    });
  }

  // ================================================================== history & saving
  function snapshot() { return JSON.stringify(E.scene); }
  function resetHistory() { E.history = [snapshot()]; E.future = []; updateButtons(); }
  function commit() {
    const snap = snapshot();
    if (E.history[E.history.length - 1] !== snap) {
      E.history.push(snap);
      if (E.history.length > 150) E.history.shift();
      E.future = [];
      scheduleSave();
    }
    renderLayers();
    renderProps();
    updateButtons();
  }
  function undo() {
    if (E.history.length < 2) return;
    E.future.push(E.history.pop());
    E.scene = JSON.parse(E.history[E.history.length - 1]);
    afterHistory();
  }
  function redo() {
    if (!E.future.length) return;
    const snap = E.future.pop();
    E.history.push(snap);
    E.scene = JSON.parse(snap);
    afterHistory();
  }
  function afterHistory() {
    if (E.selected && !el(E.selected)) E.selected = null;
    renderStage(); renderLayers(); renderProps(); updateButtons(); scheduleSave();
  }

  function setSaveState(text) { $("ed-save").textContent = text; }
  function scheduleSave() {
    if (!E.meta) return;
    setSaveState(T("Unsaved changes …"));
    clearTimeout(E.saveTimer);
    E.saveTimer = setTimeout(save, 500);
  }
  async function save() {
    clearTimeout(E.saveTimer);
    if (!E.meta) return;
    if (E.saving) { E.saveQueued = true; return; }
    E.saving = true;
    setSaveState(T("Saving …"));
    const live = $("opt-live").checked;
    try {
      if (live) await ESS.api(`/api/scenes/${E.meta.id}/publish`, { content: E.scene });
      else await ESS.api(`/api/scenes/${E.meta.id}/draft`, { content: E.scene });
      E.hasDraft = !live;
      setSaveState(live ? T("Live") : T("Draft saved"));
    } catch (e) {
      setSaveState(T("Not saved"));
      ESS.toast(e.message, "err");
    } finally {
      E.saving = false;
      updateState();
      if (E.saveQueued) { E.saveQueued = false; save(); }
    }
  }

  function updateButtons() {
    $("btn-undo").disabled = E.history.length < 2;
    $("btn-redo").disabled = !E.future.length;
    const has = !!sel();
    ["btn-dup", "btn-front", "btn-up", "btn-down", "btn-back", "btn-del"].forEach((id) => ($(id).disabled = !has));
    document.querySelectorAll("[data-add]").forEach((b) => (b.disabled = !E.scene));
  }
  function updateState() {
    const badge = $("ed-state");
    if (!E.meta) { badge.textContent = ""; $("ed-title").textContent = ""; return; }
    $("ed-title").textContent = E.meta.name;
    const active = isActive(E.meta.id);
    badge.className = "badge " + (active ? "bg-danger" : "bg-secondary");
    badge.textContent = active ? T("On air") : T("Not on air");
    const live = $("opt-live").checked;
    $("btn-apply").disabled = live || !E.hasDraft;
    $("btn-discard").disabled = live || !E.hasDraft;
    $("btn-apply").textContent = E.hasDraft ? T("Apply") : T("Applied");
  }
  function isActive(sceneId) {
    if (!E.list) return false;
    return E.list.outputs.some((o) => o.active === sceneId);
  }

  // ================================================================== scene list
  async function loadList() {
    E.list = await ESS.api("/api/scenes");
    renderList();
    updateState();
  }
  function renderList() {
    const box = $("scene-list");
    box.innerHTML = "";
    E.list.outputs.forEach((o) => {
      const wrap = document.createElement("div");
      wrap.className = "scene-out";
      wrap.innerHTML = `<div class="scene-out-head"><span>${esc(o.name)}</span>${o.mirror_of ? `<span title="${esc(T("This output currently mirrors output {n}", { n: o.mirror_of }))}">⇄ ${o.mirror_of}</span>` : ""}</div>`;
      if (!o.scenes.length) {
        const empty = document.createElement("div");
        empty.className = "small text-dim px-2"; empty.textContent = T("No scenes yet");
        wrap.appendChild(empty);
      }
      o.scenes.forEach((sc) => {
        const item = document.createElement("div");
        item.className = "scene-item" + (E.meta && E.meta.id === sc.id ? " open" : "");
        item.innerHTML = `<span class="nm" title="${esc(sc.name)}">${esc(sc.name)}</span>
          ${sc.active ? `<span class="badge-live">${esc(T("LIVE"))}</span>` : ""}
          ${sc.has_draft ? `<span class="badge-draft" title="${esc(T("Changes not applied yet"))}">✎</span>` : ""}
          <button class="btn btn-sm btn-link text-reset p-0 px-1 menu-btn" aria-label="${esc(T("Scene actions"))}">⋯</button>`;
        item.addEventListener("click", (ev) => {
          if (ev.target.closest(".menu-btn")) { showMenu(ev.target.closest(".menu-btn"), sc, o); return; }
          openScene(sc.id);
        });
        item.addEventListener("dblclick", () => goLive(o.id, sc.id));
        wrap.appendChild(item);
      });
      const off = document.createElement("div");
      off.className = "scene-item small text-dim";
      off.innerHTML = `<span class="nm">${esc(T("Splash screen (no scene)"))}</span>${o.active ? "" : `<span class="badge-live">${esc(T("LIVE"))}</span>`}`;
      off.title = T("Double-click to show the splash screen on this output");
      off.addEventListener("dblclick", () => goLive(o.id, null));
      wrap.appendChild(off);
      box.appendChild(wrap);
    });
  }

  let menu = null;
  function closeMenu() { if (menu) { menu.remove(); menu = null; } }
  document.addEventListener("click", (e) => { if (menu && !menu.contains(e.target) && !e.target.closest(".menu-btn")) closeMenu(); });
  function showMenu(anchor, sc, o) {
    closeMenu();
    menu = document.createElement("div");
    menu.className = "scene-menu";
    const items = [
      [T("Show on output (go live)"), () => goLive(o.id, sc.id)],
      [T("Rename …"), () => renameScene(sc)],
      [T("Duplicate"), () => duplicateScene(sc)],
      [T("Export (with images)"), () => { location.href = `/api/scenes/${sc.id}/export?media=1`; }],
      [T("Export (without images)"), () => { location.href = `/api/scenes/${sc.id}/export`; }],
      [T("Delete …"), () => deleteScene(sc), "danger"],
    ];
    items.forEach(([label, fn, cls]) => {
      const b = document.createElement("button");
      b.textContent = label; if (cls) b.className = cls;
      b.addEventListener("click", () => { closeMenu(); fn(); });
      menu.appendChild(b);
    });
    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    menu.style.left = Math.min(r.left, window.innerWidth - 210) + window.scrollX + "px";
    menu.style.top = r.bottom + window.scrollY + 4 + "px";
  }

  async function goLive(outputId, sceneId) {
    if (sceneId && E.meta && E.meta.id === sceneId && E.hasDraft && !$("opt-live").checked) {
      ESS.toast(T("Note: the scene has changes that are not applied yet."), "");
    }
    await ESS.run(() => ESS.api(`/api/outputs/${outputId}/activate`, { scene_id: sceneId }), sceneId ? T("Scene is live") : T("Splash screen is live"));
    loadList();
  }
  async function renameScene(sc) {
    const name = window.prompt(T("New name"), sc.name);
    if (!name || name === sc.name) return;
    await ESS.run(() => ESS.api(`/api/scenes/${sc.id}/rename`, { name }));
    if (E.meta && E.meta.id === sc.id) E.meta.name = name;
    loadList();
  }
  async function duplicateScene(sc) {
    const copy = await ESS.run(() => ESS.api(`/api/scenes/${sc.id}/duplicate`, {}), T("Scene duplicated"));
    await loadList();
    openScene(copy.id);
  }
  async function deleteScene(sc) {
    if (!ESS.confirm(T('Delete the scene "{name}"?', { name: sc.name }))) return;
    await ESS.run(() => ESS.api(`/api/scenes/${sc.id}/delete`, {}), T("Scene deleted"));
    if (E.meta && E.meta.id === sc.id) closeScene();
    loadList();
  }

  // ================================================================== open / close scene
  async function openScene(id) {
    if (E.meta && E.meta.id !== id) await save();
    const data = await ESS.api("/api/scenes/" + id);
    E.meta = { id: data.id, output_id: data.output_id, name: data.name };
    E.scene = clone(data.draft || data.live);
    E.hasDraft = !!data.draft;
    E.selected = null;
    $("ed-empty").hidden = true;
    resetHistory();
    renderStage(); renderLayers(); renderProps(); renderList(); updateState(); updateButtons();
    setSaveState("");
    try { localStorage.setItem("ess.editor.scene", id); } catch (e) { /* ignore */ }
  }
  function closeScene() {
    E.meta = null; E.scene = null; E.selected = null;
    stage.destroy();
    stage = SceneRender.createStage(host, { editor: true, t: T, locale: ESS.lang });
    overlay.innerHTML = "";
    $("ed-empty").hidden = false;
    renderLayers(); renderProps(); updateState(); updateButtons();
  }

  // ================================================================== elements
  function defaultsFor(type) {
    const W = E.scene.width, H = E.scene.height;
    const font = E.fonts.bundled.includes("Inter") ? "Inter" : "DejaVu Sans";
    const mono = E.fonts.bundled.includes("JetBrains Mono") ? "JetBrains Mono" : "DejaVu Sans Mono";
    const textProps = (h, f) => ({ font: f, size: Math.round(h * 0.6), weight: 700, italic: false, color: "#ffffff", background: "transparent",
      align: "center", valign: "middle", padding: 0, shadow: false });
    let w, h, props;
    if (type === "text") { w = W * 0.5; h = H * 0.15; props = Object.assign(textProps(h, font), { text: T("Text"), line_height: 1.2, uppercase: false }); }
    else if (type === "clock") {
      w = W * 0.3; h = H * 0.14;
      props = Object.assign(textProps(h, mono), { style: "digital", format: "HH:mm:ss", face_color: "#111111", hand_color: "#ffffff",
        second_color: "#e5484d", tick_color: "#bbbbbb", show_seconds: true });
    } else if (type === "color") { w = W * 0.3; h = H * 0.2; props = { color: "#2fb4e8", radius: 0, border_width: 0, border_color: "#ffffff" }; }
    else { w = W * 0.3; h = H * 0.3; props = { media_id: null, fit: "contain" }; }
    const count = E.scene.elements.filter((e) => e.type === type).length + 1;
    return {
      id: rid(), type, name: `${TYPE_LABEL[type]} ${count}`, x: round((W - w) / 2), y: round((H - h) / 2), w: round(w), h: round(h),
      rotation: 0, flip_h: false, flip_v: false, opacity: 1, visible: true, locked: false, lock_aspect: type === "image",
      crop: { top: 0, right: 0, bottom: 0, left: 0 }, props,
    };
  }

  async function addElement(type) {
    if (!E.scene) return;
    const e = defaultsFor(type);
    if (type === "image") {
      const media = await pickMedia();
      if (!media) return;
      e.props.media_id = media.id;
      e.name = media.name.replace(/\.[a-z0-9]+$/i, "").slice(0, 60) || e.name;
      const size = await imageSize(media.url);
      if (size) {
        const maxW = E.scene.width * 0.5, maxH = E.scene.height * 0.5;
        const f = Math.min(maxW / size.w, maxH / size.h, 1);
        e.w = round(size.w * f); e.h = round(size.h * f);
        e.x = round((E.scene.width - e.w) / 2); e.y = round((E.scene.height - e.h) / 2);
      }
    }
    E.scene.elements.push(e);
    E.selected = e.id;
    patch(e);
    markLocked();
    commit();
  }

  function imageSize(url) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img.naturalWidth ? { w: img.naturalWidth, h: img.naturalHeight } : null);
      img.onerror = () => resolve(null);
      img.src = url;
    });
  }

  function select(id) {
    E.selected = id;
    drawOverlay(); renderLayers(); renderProps(); updateButtons();
  }
  function removeSelected() {
    const e = sel();
    if (!e) return;
    E.scene.elements = E.scene.elements.filter((x) => x.id !== e.id);
    E.selected = null;
    renderStage(); commit();
  }
  function duplicateSelected() {
    const e = sel();
    if (!e) return;
    const copy = clone(e);
    copy.id = rid(); copy.name = (e.name + " " + T("copy")).slice(0, 60);
    copy.x = round(e.x + 20); copy.y = round(e.y + 20); copy.locked = false;
    E.scene.elements.splice(E.scene.elements.indexOf(e) + 1, 0, copy);
    E.selected = copy.id;
    renderStage(); commit();
  }
  function moveZ(where) {
    const e = sel();
    if (!e) return;
    const list = E.scene.elements, i = list.indexOf(e);
    list.splice(i, 1);
    const j = where === "front" ? list.length : where === "back" ? 0 : where === "up" ? Math.min(list.length, i + 1) : Math.max(0, i - 1);
    list.splice(j, 0, e);
    renderStage(); commit();
  }

  // ================================================================== pointer interaction
  function snapMove(e, nx, ny, free) {
    const guides = [];
    if (!free && $("opt-snap").checked) {
      const th = 7 / E.view.scale, W = E.scene.width, H = E.scene.height;
      const xs = [0, W / 2, W], ys = [0, H / 2, H];
      E.scene.elements.forEach((o) => {
        if (o.id === e.id || !o.visible) return;
        xs.push(o.x, o.x + o.w / 2, o.x + o.w); ys.push(o.y, o.y + o.h / 2, o.y + o.h);
      });
      const best = (cands, pos, size) => {
        let hit = null;
        [0, size / 2, size].forEach((off) => cands.forEach((c) => {
          const d = Math.abs(c - (pos + off));
          if (d < th && (!hit || d < hit.d)) hit = { d, v: c - off, line: c };
        }));
        return hit;
      };
      const bx = best(xs, nx, e.w), by = best(ys, ny, e.h);
      if (bx) { nx = bx.v; guides.push({ x: bx.line }); }
      if (by) { ny = by.v; guides.push({ y: by.line }); }
      if ($("opt-grid").checked) {
        const g = Math.max(4, Number($("opt-grid-size").value) || 40);
        if (!bx) nx = Math.round(nx / g) * g;
        if (!by) ny = Math.round(ny / g) * g;
      }
    }
    return { x: round(nx), y: round(ny), guides };
  }

  const HANDLES = { nw: [-1, -1], n: [0, -1], ne: [1, -1], e: [1, 0], se: [1, 1], s: [0, 1], sw: [-1, 1], w: [-1, 0] };

  canvas.addEventListener("pointerdown", (ev) => {
    if (!E.scene || ev.button !== 0) return;
    canvas.focus({ preventScroll: true });
    const handle = ev.target.closest(".hd");
    const onBox = ev.target.closest(".sel-box");
    const node = ev.target.closest(".sr-el");
    let e = sel();
    if (handle && e && !e.locked) {
      startDrag(ev, handle.dataset.h === "rot" ? "rotate" : "resize", e, handle.dataset.h);
    } else if (onBox && e && !e.locked) {
      startDrag(ev, "move", e);
    } else if (node) {
      const target = el(node.dataset.id);
      if (target && !target.locked) { select(target.id); startDrag(ev, "move", target); }
    } else {
      select(null);
    }
  });

  function startDrag(ev, kind, e, handle) {
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    E.drag = { kind, handle, start: toStage(ev.clientX, ev.clientY), orig: clone(e), id: e.id, moved: false };
  }

  canvas.addEventListener("pointermove", (ev) => {
    const d = E.drag;
    if (!d) return;
    const e = el(d.id), o = d.orig;
    const p = toStage(ev.clientX, ev.clientY);
    const dx = p.x - d.start.x, dy = p.y - d.start.y;
    if (!d.moved && Math.hypot(dx, dy) * E.view.scale < 2) return;
    d.moved = true;
    let guides = [];
    if (d.kind === "move") {
      const r = snapMove(e, o.x + dx, o.y + dy, ev.altKey);
      e.x = r.x; e.y = r.y; guides = r.guides;
    } else if (d.kind === "resize") {
      const [hx, hy] = HANDLES[d.handle];
      const rad = (o.rotation * Math.PI) / 180, cos = Math.cos(rad), sin = Math.sin(rad);
      const lx = dx * cos + dy * sin, ly = -dx * sin + dy * cos; // pointer delta in the element frame
      let dw = hx * lx, dh = hy * ly;
      const keep = e.lock_aspect !== ev.shiftKey; // Shift toggles the aspect lock
      if (keep) {
        const ratio = o.w / o.h;
        if (hx && hy) { const f = Math.max((o.w + dw) / o.w, (o.h + dh) / o.h); dw = o.w * f - o.w; dh = o.h * f - o.h; }
        else if (hx) dh = (o.w + dw) / ratio - o.h;
        else dw = (o.h + dh) * ratio - o.w;
      }
      const w = Math.max(1, o.w + dw), h = Math.max(1, o.h + dh);
      dw = w - o.w; dh = h - o.h;
      // keep the opposite edge fixed: move the centre by half the growth (in the element frame)
      const cxl = hx * dw / 2, cyl = hy * dh / 2;
      const cx = o.x + o.w / 2 + cxl * cos - cyl * sin, cy = o.y + o.h / 2 + cxl * sin + cyl * cos;
      e.w = round(w); e.h = round(h); e.x = round(cx - w / 2); e.y = round(cy - h / 2);
    } else if (d.kind === "rotate") {
      const cx = o.x + o.w / 2, cy = o.y + o.h / 2;
      let a = (Math.atan2(p.y - cy, p.x - cx) * 180) / Math.PI + 90;
      if (ev.shiftKey) a = Math.round(a / 15) * 15;
      else { const near = Math.round(a / 90) * 90; if (Math.abs(a - near) < 3) a = near; }
      a = ((a + 540) % 360) - 180;
      e.rotation = round(a, 1);
    }
    patch(e, true);
    drawOverlay(guides);
    renderPropsValues();
  });

  function endDrag() {
    if (!E.drag) return;
    const moved = E.drag.moved;
    E.drag = null;
    drawOverlay();
    if (moved) commit();
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  // ================================================================== keyboard
  document.addEventListener("keydown", (ev) => {
    if (!E.scene) return;
    const tag = (ev.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag) || ev.target.isContentEditable) return;
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && ev.key.toLowerCase() === "z") { ev.preventDefault(); ev.shiftKey ? redo() : undo(); return; }
    if (mod && ev.key.toLowerCase() === "y") { ev.preventDefault(); redo(); return; }
    if (mod && ev.key.toLowerCase() === "d") { ev.preventDefault(); duplicateSelected(); return; }
    if (mod && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); return; }
    const e = sel();
    if (!e) return;
    if (ev.key === "Escape") { select(null); return; }
    if ((ev.key === "Delete" || ev.key === "Backspace") && !e.locked) { ev.preventDefault(); removeSelected(); return; }
    const step = ev.shiftKey ? 10 : 1;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (moves[ev.key] && !e.locked) {
      ev.preventDefault();
      e.x = round(e.x + moves[ev.key][0]); e.y = round(e.y + moves[ev.key][1]);
      patch(e, true); renderPropsValues();
      clearTimeout(E.keyCommit); E.keyCommit = setTimeout(commit, 400);
    }
  });

  // ================================================================== layers
  function renderLayers() {
    const box = $("layers");
    box.innerHTML = "";
    if (!E.scene) return;
    if (!E.scene.elements.length) { box.innerHTML = `<div class="small text-dim p-2">${esc(T("No elements yet – add one with the toolbar."))}</div>`; return; }
    [...E.scene.elements].reverse().forEach((e) => {
      const row = document.createElement("div");
      row.className = "layer-row" + (e.id === E.selected ? " sel" : "");
      row.innerHTML = `<span class="ic">${TYPE_ICON[e.type]}</span><span class="nm">${esc(e.name)}</span>
        <button class="tg ${e.visible ? "" : "off"}" data-t="visible" title="${esc(T("Show/hide"))}">👁</button>
        <button class="tg ${e.locked ? "" : "off"}" data-t="locked" title="${esc(T("Lock/unlock"))}">🔒</button>`;
      row.addEventListener("click", (ev) => {
        const tg = ev.target.closest(".tg");
        if (tg) {
          e[tg.dataset.t] = !e[tg.dataset.t];
          patch(e);
          commit();
          return;
        }
        select(e.id);
      });
      box.appendChild(row);
    });
  }

  // ================================================================== properties
  const ALIGN = [["left", T("Left")], ["center", T("Centre")], ["right", T("Right")]];
  const VALIGN = [["top", T("Top")], ["middle", T("Middle")], ["bottom", T("Bottom")]];
  const WEIGHTS = [100, 200, 300, 400, 500, 600, 700, 800, 900].map((w) => [w, String(w)]);
  const TEXT_FIELDS = [
    { sec: T("Text") },
    { k: "props.font", l: T("Font"), t: "font", full: true },
    { k: "props.size", l: T("Font size"), t: "num", min: 1, max: 2000 },
    { k: "props.weight", l: T("Font weight"), t: "select", opts: WEIGHTS, num: true },
    { k: "props.color", l: T("Text colour"), t: "color" },
    { k: "props.background", l: T("Background colour"), t: "color", transparent: true },
    { k: "props.align", l: T("Alignment"), t: "select", opts: ALIGN },
    { k: "props.valign", l: T("Vertical alignment"), t: "select", opts: VALIGN },
    { k: "props.padding", l: T("Padding"), t: "num", min: 0, max: 1000 },
    { k: "props.italic", l: T("Italic"), t: "bool" },
    { k: "props.shadow", l: T("Shadow"), t: "bool" },
  ];
  const FIELDS = {
    text: [
      { k: "props.text", l: T("Text"), t: "textarea", full: true },
      ...TEXT_FIELDS,
      { k: "props.line_height", l: T("Line height"), t: "num", min: 0.5, max: 3, step: 0.05 },
      { k: "props.uppercase", l: T("Uppercase"), t: "bool" },
    ],
    clock: [
      { sec: T("Clock") },
      { k: "props.style", l: T("Clock style"), t: "select", opts: [["digital", T("Digital")], ["analog", T("Analog")]] },
      { k: "props.format", l: T("Time format"), t: "text", list: ["HH:mm", "HH:mm:ss", "H:mm", "hh:mm a", "dd.MM.yyyy", "dddd, d. MMMM", "dd.MM.yyyy HH:mm"], digital: true },
      { help: T("HH/H hours, mm minutes, ss seconds, dd/d day, MM/MMM/MMMM month, yyyy year, ddd/dddd weekday, a AM/PM, [text] literal."), digital: true },
      ...TEXT_FIELDS.slice(1).map((f) => Object.assign({ digital: true }, f)),
      { k: "props.face_color", l: T("Face colour"), t: "color", transparent: true, analog: true },
      { k: "props.hand_color", l: T("Hand colour"), t: "color", analog: true },
      { k: "props.second_color", l: T("Second hand colour"), t: "color", analog: true },
      { k: "props.tick_color", l: T("Tick colour"), t: "color", analog: true },
      { k: "props.show_seconds", l: T("Second hand"), t: "bool", analog: true },
    ],
    image: [
      { sec: T("Image") },
      { k: "props.media_id", l: T("Image"), t: "media", full: true },
      { k: "props.fit", l: T("Fit"), t: "select", opts: [["contain", T("Fit (keep aspect ratio)")], ["cover", T("Fill (crop)")], ["fill", T("Stretch")]], full: true },
    ],
    color: [
      { sec: T("Colour area") },
      { k: "props.color", l: T("Colour"), t: "color", transparent: true },
      { k: "props.radius", l: T("Corner radius"), t: "num", min: 0, max: 10000 },
      { k: "props.border_width", l: T("Border width"), t: "num", min: 0, max: 1000 },
      { k: "props.border_color", l: T("Border colour"), t: "color" },
    ],
  };
  const COMMON_FIELDS = [
    { k: "name", l: T("Name"), t: "text", full: true },
    { sec: T("Position & size") },
    { k: "x", l: "X", t: "num" }, { k: "y", l: "Y", t: "num" },
    { k: "w", l: T("Width"), t: "num", min: 1 }, { k: "h", l: T("Height"), t: "num", min: 1 },
    { k: "rotation", l: T("Rotation") + " °", t: "num", min: -360, max: 360, step: 0.5 },
    { k: "opacity", l: T("Opacity") + " %", t: "pct" },
    { k: "lock_aspect", l: T("Keep aspect ratio"), t: "bool" },
    { k: "visible", l: T("Visible"), t: "bool" },
    { k: "flip_h", l: T("Flip horizontally"), t: "bool" },
    { k: "flip_v", l: T("Flip vertically"), t: "bool" },
    { k: "locked", l: T("Locked"), t: "bool" },
  ];
  const CROP_FIELDS = [
    { sec: T("Crop") + " %" },
    { k: "crop.top", l: T("Top"), t: "crop" }, { k: "crop.bottom", l: T("Bottom"), t: "crop" },
    { k: "crop.left", l: T("Left"), t: "crop" }, { k: "crop.right", l: T("Right"), t: "crop" },
  ];
  const SCENE_FIELDS = [
    { sec: T("Scene") },
    { k: "width", l: T("Width"), t: "num", min: 320, max: 7680, step: 1 },
    { k: "height", l: T("Height"), t: "num", min: 200, max: 4320, step: 1 },
    { k: "background", l: T("Background colour"), t: "color" },
    { k: "scaling", l: T("Scaling"), t: "select", opts: [["letterbox", T("Letterbox (keep aspect ratio)")], ["stretch", T("Stretch")]] },
    { sec: T("Transition when going live") },
    { k: "transition.type", l: T("Transition"), t: "select", opts: [["fade", T("Fade")], ["cut", T("Cut")]] },
    { k: "transition.duration_ms", l: T("Duration (ms)"), t: "num", min: 0, max: 5000, step: 50 },
  ];

  function fontOptions(current) {
    const groups = [[T("Bundled"), E.fonts.bundled], [T("Uploaded"), E.fonts.uploaded], [T("System"), E.fonts.system]];
    let html = "", found = false;
    groups.forEach(([label, list]) => {
      if (!list.length) return;
      html += `<optgroup label="${esc(label)}">` + list.map((f) => {
        if (f === current) found = true;
        return `<option value="${esc(f)}" ${f === current ? "selected" : ""} style="font-family:'${esc(f)}'">${esc(f)}</option>`;
      }).join("") + "</optgroup>";
    });
    if (!found && current) html = `<option value="${esc(current)}" selected>${esc(current)} (${esc(T("missing"))})</option>` + html;
    return html;
  }

  function fieldVisible(f, target) {
    if (target.type !== "clock") return true;
    if (f.digital && target.props.style !== "digital") return false;
    if (f.analog && target.props.style !== "analog") return false;
    return true;
  }

  function buildField(f, target) {
    const wrap = document.createElement("div");
    if (f.sec) { wrap.className = "props-sec"; wrap.textContent = f.sec; return wrap; }
    if (f.help) { wrap.className = "full form-text mt-0"; wrap.textContent = f.help; return wrap; }
    wrap.className = f.full || f.t === "textarea" || f.t === "font" || f.t === "media" ? "full" : "";
    const value = getPath(target, f.k);
    const id = "f-" + f.k.replace(/\./g, "-");
    let input;
    if (f.t === "bool") {
      wrap.innerHTML = `<div class="form-check mt-1"><input class="form-check-input" type="checkbox" id="${id}" ${value ? "checked" : ""}><label class="form-check-label small" for="${id}">${esc(f.l)}</label></div>`;
      input = wrap.querySelector("input");
      input.addEventListener("change", () => update(f, input.checked, true));
      input.dataset.path = f.k; input.dataset.kind = f.t;
      return wrap;
    }
    wrap.innerHTML = `<label class="form-label mb-0" for="${id}">${esc(f.l)}</label>`;
    if (f.t === "textarea") {
      input = document.createElement("textarea"); input.className = "form-control"; input.rows = 3; input.value = value;
    } else if (f.t === "select" || f.t === "font") {
      input = document.createElement("select"); input.className = "form-select";
      input.innerHTML = f.t === "font" ? fontOptions(value)
        : f.opts.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(l)}</option>`).join("");
    } else if (f.t === "color") {
      const row = document.createElement("div"); row.className = "color-row";
      input = document.createElement("input"); input.type = "color"; input.className = "form-control form-control-color";
      input.value = value === "transparent" || !value ? "#000000" : value.slice(0, 7);
      row.appendChild(input);
      if (f.transparent) {
        const tr = document.createElement("div");
        tr.className = "form-check mb-0";
        tr.innerHTML = `<input class="form-check-input" type="checkbox" id="${id}-t" ${value === "transparent" ? "checked" : ""}><label class="form-check-label small" for="${id}-t">${esc(T("none"))}</label>`;
        const cb = tr.querySelector("input");
        cb.addEventListener("change", () => update(f, cb.checked ? "transparent" : input.value, true));
        input.addEventListener("input", () => { cb.checked = false; });
        row.appendChild(tr);
      }
      wrap.appendChild(row);
    } else if (f.t === "media") {
      const item = E.media.find((m) => m.id === value);
      const row = document.createElement("div"); row.className = "d-flex gap-2 align-items-center";
      row.innerHTML = `<span class="small flex-grow-1 text-truncate">${esc(item ? item.name : T("No image selected"))}</span>`;
      const btn = document.createElement("button"); btn.type = "button"; btn.className = "btn btn-sm btn-console"; btn.textContent = T("Choose …");
      btn.addEventListener("click", async () => {
        const m = await pickMedia();
        if (m) update(f, m.id, true);
      });
      const ratio = document.createElement("button"); ratio.type = "button"; ratio.className = "btn btn-sm btn-console";
      ratio.textContent = T("Original aspect ratio");
      ratio.addEventListener("click", async () => {
        const e = sel(); const m = E.media.find((x) => x.id === e.props.media_id);
        const size = m && await imageSize(m.url);
        if (!size) return;
        const cx = e.x + e.w / 2, cy = e.y + e.h / 2;
        e.h = round(e.w * size.h / size.w); e.y = round(cy - e.h / 2); e.x = round(cx - e.w / 2);
        patch(e); commit();
      });
      row.appendChild(btn); row.appendChild(ratio);
      wrap.appendChild(row);
      return wrap;
    } else {
      input = document.createElement("input"); input.className = "form-control";
      if (f.t === "text") { input.type = "text"; input.value = value; if (f.list) { const dl = document.createElement("datalist"); dl.id = id + "-list"; dl.innerHTML = f.list.map((x) => `<option value="${esc(x)}">`).join(""); wrap.appendChild(dl); input.setAttribute("list", dl.id); } }
      else {
        input.type = "number";
        if (f.t === "pct") { input.min = 0; input.max = 100; input.step = 1; input.value = Math.round(value * 100); }
        else if (f.t === "crop") { input.min = 0; input.max = 95; input.step = 0.5; input.value = round(value * 100, 1); }
        else { input.step = f.step || 1; if (f.min !== undefined) input.min = f.min; if (f.max !== undefined) input.max = f.max; input.value = round(value); }
      }
    }
    input.id = id;
    input.dataset.path = f.k; input.dataset.kind = f.t;
    if (f.t !== "color") wrap.appendChild(input);
    const read = () => {
      if (f.t === "pct") return Math.min(1, Math.max(0, Number(input.value) / 100));
      if (f.t === "crop") return Math.min(0.95, Math.max(0, Number(input.value) / 100));
      if (f.t === "num") { let v = Number(input.value); if (Number.isNaN(v)) v = getPath(target, f.k); if (f.min !== undefined) v = Math.max(f.min, v); if (f.max !== undefined) v = Math.min(f.max, v); return v; }
      if (f.num) return Number(input.value);
      return input.value;
    };
    input.addEventListener("input", () => update(f, read(), false));
    input.addEventListener("change", () => update(f, read(), true));
    return wrap;
  }

  function update(f, value, final) {
    const sceneLevel = !sel();
    const target = sceneLevel ? E.scene : sel();
    if (!target) return;
    if (f.t === "crop") {
      const side = f.k.split(".")[1], other = { top: "bottom", bottom: "top", left: "right", right: "left" }[side];
      value = Math.min(value, 0.95 - target.crop[other]);
    }
    if (!sceneLevel && target.lock_aspect && (f.k === "w" || f.k === "h")) {
      const ratio = target.w / target.h;
      if (f.k === "w") target.h = round(value / ratio); else target.w = round(value * ratio);
    }
    setPath(target, f.k, value);
    if (sceneLevel) { if (final) renderStage(); }
    else {
      const geometry = ["x", "y", "w", "h", "rotation", "opacity", "flip_h", "flip_v"].includes(f.k) || f.k.startsWith("crop.");
      patch(target, geometry);
    }
    if (final) {
      commit();
      if (f.k === "props.style" || f.k === "name" || f.k === "locked" || f.k === "visible") renderProps();
    } else if (f.k === "w" || f.k === "h") renderPropsValues();
  }

  function renderProps() {
    const box = $("props");
    box.innerHTML = "";
    if (!E.scene) { box.innerHTML = `<div class="small text-dim">${esc(T("No scene open."))}</div>`; $("props-title").textContent = T("Properties"); return; }
    const target = sel();
    const grid = document.createElement("div");
    grid.className = "props-grid";
    let fields;
    if (target) {
      $("props-title").textContent = TYPE_LABEL[target.type];
      fields = [...COMMON_FIELDS, ...FIELDS[target.type], ...(target.type === "clock" && target.props.style === "analog" ? [] : CROP_FIELDS)];
    } else {
      $("props-title").textContent = T("Scene");
      fields = SCENE_FIELDS;
    }
    fields.filter((f) => !target || fieldVisible(f, target)).forEach((f) => grid.appendChild(buildField(f, target || E.scene)));
    box.appendChild(grid);
    if (!target) {
      const hint = document.createElement("div");
      hint.className = "form-text mt-2";
      hint.textContent = T("Select an element on the canvas or in the layer list to edit it.");
      box.appendChild(hint);
    }
  }

  /** Refresh numeric inputs after dragging without rebuilding the form. */
  function renderPropsValues() {
    const e = sel();
    if (!e) return;
    document.querySelectorAll("#props [data-path]").forEach((input) => {
      if (document.activeElement === input) return;
      const k = input.dataset.path;
      if (!["x", "y", "w", "h", "rotation"].includes(k)) return;
      input.value = round(getPath(e, k));
    });
  }

  // ================================================================== media picker
  let pickResolve = null;
  async function loadMedia() { E.media = (await ESS.api("/api/media")).items; }
  function renderMediaGrid() {
    const grid = $("media-grid");
    const images = E.media.filter((m) => m.kind === "image");
    grid.innerHTML = images.length ? "" : `<div class="text-dim small">${esc(T("No images yet – upload one."))}</div>`;
    images.forEach((m) => {
      const card = document.createElement("div");
      card.className = "media-card";
      card.innerHTML = `<div class="thumb"><img src="${esc(m.url)}" alt="" loading="lazy"></div><div class="meta"><div class="nm" title="${esc(m.name)}">${esc(m.name)}</div></div>`;
      card.addEventListener("click", () => { $("dlg-media").close(); if (pickResolve) pickResolve(m); pickResolve = null; });
      grid.appendChild(card);
    });
  }
  async function pickMedia() {
    await loadMedia();
    renderMediaGrid();
    $("dlg-media").showModal();
    return new Promise((resolve) => { pickResolve = resolve; });
  }
  $("media-close").addEventListener("click", () => { $("dlg-media").close(); if (pickResolve) pickResolve(null); pickResolve = null; });
  $("dlg-media").addEventListener("cancel", () => { if (pickResolve) pickResolve(null); pickResolve = null; });
  $("media-upload").addEventListener("change", async (ev) => {
    for (const file of ev.target.files) {
      await ESS.run(() => uploadFile(file), T('"{name}" uploaded', { name: file.name })).catch(() => {});
    }
    ev.target.value = "";
    await loadMedia();
    renderMediaGrid();
  });

  // ================================================================== toolbar & scene actions
  document.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", () => addElement(b.dataset.add)));
  $("btn-undo").addEventListener("click", undo);
  $("btn-redo").addEventListener("click", redo);
  $("btn-dup").addEventListener("click", duplicateSelected);
  $("btn-del").addEventListener("click", removeSelected);
  $("btn-front").addEventListener("click", () => moveZ("front"));
  $("btn-back").addEventListener("click", () => moveZ("back"));
  $("btn-up").addEventListener("click", () => moveZ("up"));
  $("btn-down").addEventListener("click", () => moveZ("down"));
  $("opt-live").addEventListener("change", async () => {
    if ($("opt-live").checked && E.meta) {
      if (E.hasDraft && !ESS.confirm(T("Live editing publishes every change immediately. Apply the current changes now?"))) { $("opt-live").checked = false; return; }
      await save();
    }
    updateState();
  });
  $("btn-apply").addEventListener("click", async () => {
    if (!E.meta) return;
    await save();
    await ESS.run(() => ESS.api(`/api/scenes/${E.meta.id}/publish`, {}), T("Changes applied"));
    E.hasDraft = false;
    setSaveState("");
    updateState();
    loadList();
  });
  $("btn-discard").addEventListener("click", async () => {
    if (!E.meta || !ESS.confirm(T("Discard all changes since the last Apply?"))) return;
    clearTimeout(E.saveTimer);
    await ESS.run(() => ESS.api(`/api/scenes/${E.meta.id}/discard`, {}));
    await openScene(E.meta.id);
    loadList();
  });

  // new scene dialog
  $("btn-new-scene").addEventListener("click", async () => {
    const form = $("form-new");
    const outputs = await ESS.api("/api/outputs");
    form.elements.output.innerHTML = outputs.outputs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
    const sizes = new Map();
    outputs.outputs.forEach((o) => {
      const m = o.current_mode || o.mode;
      if (m) sizes.set(`${m.width}x${m.height}`, `${m.width} × ${m.height} (${o.name})`);
    });
    [["1920x1080", "1920 × 1080 (Full HD)"], ["1280x720", "1280 × 720 (HD)"], ["3840x2160", "3840 × 2160 (4K)"],
     ["1080x1920", "1080 × 1920 (" + T("portrait") + ")"], ["1024x768", "1024 × 768 (4:3)"]].forEach(([k, v]) => { if (!sizes.has(k)) sizes.set(k, v); });
    form.elements.size.innerHTML = [...sizes].map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    if (E.meta) form.elements.output.value = String(E.meta.output_id);
    form.elements.name.value = "";
    $("dlg-new").showModal();
  });
  $("dlg-new").addEventListener("close", async () => {
    if ($("dlg-new").returnValue !== "ok") return;
    const f = $("form-new").elements;
    const [w, h] = f.size.value.split("x").map(Number);
    const sc = await ESS.run(() => ESS.api("/api/scenes", { output_id: Number(f.output.value), name: f.name.value, width: w, height: h }), T("Scene created"));
    await loadList();
    openScene(sc.id);
  });

  // import
  $("import-file").addEventListener("change", async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch (e) { ESS.toast(T("This is not a scene export"), "err"); return; }
    const outputId = E.meta ? E.meta.output_id : 1;
    const sc = await ESS.run(() => ESS.api("/api/scenes/import", { output_id: outputId, data }), T("Scene imported"));
    await loadList();
    openScene(sc.id);
  });

  function uploadFile(file) {
    return ESS.api("/api/media", file, "POST", { "X-Filename": encodeURIComponent(file.name) });
  }
  ESS.uploadFile = uploadFile;

  window.addEventListener("beforeunload", (ev) => {
    if (E.saveTimer && E.meta) { save(); ev.preventDefault(); }
  });
  ESS.on("changed", (d) => { if (d.topic === "scenes") loadList(); if (d.topic === "media") loadMedia(); });

  // ================================================================== start
  (async function init() {
    updateButtons(); renderProps();
    try {
      const [fonts, , info] = await Promise.all([ESS.api("/api/fonts"), loadMedia(), ESS.api("/api/system/time")]);
      E.fonts = fonts;
      stage.ctx.timezone = info.timezone;
      await loadList();
      let last = null;
      try { last = localStorage.getItem("ess.editor.scene"); } catch (e) { /* ignore */ }
      const all = E.list.outputs.flatMap((o) => o.scenes);
      const first = all.find((s) => s.id === last) || all[0];
      if (first) openScene(first.id);
    } catch (e) {
      ESS.toast(e.message, "err");
    }
  })();
})();
