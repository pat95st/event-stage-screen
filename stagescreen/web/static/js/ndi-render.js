/* event-stage-screen – NDI elements for the scene renderer.
 *
 * Kiosk outputs read the MJPEG stream of /ndi/stream (framed JPEGs, see
 * stagescreen/infrastructure/ndi.py) and draw the newest frame on a canvas;
 * frames that arrive while one is still decoding are skipped, so latency
 * does not build up. The editor only polls /ndi/snapshot about once per
 * second. When no frame arrives, the element follows its "on_loss" setting.
 */
(function () {
  "use strict";

  const SR = window.SceneRender;
  const LOST_AFTER_MS = 1500;
  const TEXT = {
    en: { noSignal: "No signal", noSource: "No NDI source selected" },
    de: { noSignal: "Kein Signal", noSource: "Keine NDI-Quelle gewählt" },
  };
  const tr = (ctx) => TEXT[(ctx.locale || "en").slice(0, 2)] || TEXT.en;

  function params(p, ctx) {
    const q = new URLSearchParams({ source: p.source, res: p.resolution, fps: String(p.fps), q: String(p.quality) });
    return q.toString() + (ctx.ndiQuery || "");
  }

  function readHeader(buf) {
    const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    return { len: view.getUint32(0), ts: view.getFloat64(4) };
  }

  /** Stream reader: keeps only the newest frame, decodes one at a time. */
  function startStream(state, p, ctx) {
    const url = "/ndi/stream?" + params(p, ctx);
    let latest = null, decoding = false;

    async function decode() {
      if (decoding || !latest || state.disposed) return;
      decoding = true;
      const jpeg = latest;
      latest = null;
      try {
        const bmp = await createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }));
        if (!state.disposed) state.draw(bmp);
        bmp.close();
      } catch (e) { /* corrupt frame: skip */ }
      decoding = false;
      if (latest) decode();
    }

    (async function loop() {
      while (!state.disposed) {
        state.abort = new AbortController();
        try {
          const res = await fetch(url, { signal: state.abort.signal, cache: "no-store" });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            state.message = err.error || "HTTP " + res.status;
            await new Promise((r) => setTimeout(r, res.status === 429 ? 10000 : 3000));
            continue;
          }
          state.message = null;
          const reader = res.body.getReader();
          let buf = new Uint8Array(0);
          for (;;) {
            const { value, done } = await reader.read();
            if (done || state.disposed) break;
            const merged = new Uint8Array(buf.length + value.length);
            merged.set(buf); merged.set(value, buf.length);
            buf = merged;
            while (buf.length >= 12) {
              const h = readHeader(buf);
              if (buf.length < 12 + h.len) break;
              latest = buf.slice(12, 12 + h.len);
              buf = buf.slice(12 + h.len);
              decode();
            }
          }
        } catch (e) {
          if (state.disposed) return;
        }
        if (!state.disposed) await new Promise((r) => setTimeout(r, 2000));
      }
    })();
  }

  /** Editor: one still image per second. */
  function startSnapshots(state, p, ctx) {
    let busy = false;
    const fetchOne = () => {
      if (busy || state.disposed) return;
      busy = true;
      const img = new Image();
      img.onload = () => { busy = false; if (!state.disposed) { state.message = null; state.draw(img); } };
      img.onerror = () => { busy = false; state.message = tr(ctx).noSignal; };
      img.src = "/ndi/snapshot?" + params(p, ctx) + "&t=" + Date.now();
    };
    fetchOne();
    state.timer = setInterval(fetchOne, 1000);
  }

  SR.renderNdi = function (node, el, ctx) {
    const p = el.props;
    node.style.position = "relative";
    node._ndiKey = JSON.stringify(p);
    // Re-rendering the same scene: keep a running stream instead of reconnecting (no flicker).
    const reuse = ctx.ndiReuse && ctx.ndiReuse.get(node._ndiKey);
    if (reuse && reuse.length) {
      const old = reuse.shift();
      while (old.firstChild) node.appendChild(old.firstChild);
      node.style.background = old.style.background;
      node._tick = old._tick;
      node._dispose = old._dispose;
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.style.cssText = `position:absolute;inset:0;width:100%;height:100%;object-fit:${p.fit};display:block`;
    const label = document.createElement("div");
    label.style.cssText = "position:absolute;inset:0;display:none;align-items:center;justify-content:center;text-align:center;" +
      "color:#8a929e;font:600 28px/1.3 'Inter','DejaVu Sans',sans-serif;padding:12px;box-sizing:border-box";
    node.appendChild(canvas);
    node.appendChild(label);
    const g = canvas.getContext("2d");
    const state = { disposed: false, lastFrame: 0, started: Date.now(), message: null, abort: null, timer: null };

    state.draw = (img) => {
      const w = img.width || img.naturalWidth, h = img.height || img.naturalHeight;
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
      g.drawImage(img, 0, 0);
      state.lastFrame = Date.now();
    };

    const showLabel = (text, background) => {
      label.textContent = text;
      label.style.display = text ? "flex" : "none";
      if (label.parentNode) label.parentNode.style.background = background || "";
    };

    if (!p.source) {
      if (ctx.editor) { node.classList.add("sr-placeholder"); showLabel(tr(ctx).noSource); }
      return;
    }
    if (ctx.ndiMode === "snapshot") startSnapshots(state, p, ctx); else startStream(state, p, ctx);

    node._tick = () => {
      const since = state.lastFrame || state.started;
      const lost = Date.now() - since > LOST_AFTER_MS;
      if (!lost) { canvas.style.visibility = "visible"; showLabel(""); return; }
      if (ctx.editor && !state.lastFrame) {
        canvas.style.visibility = "hidden";
        showLabel(`NDI: ${p.source}\n${state.message || tr(ctx).noSignal}`, "#15181c");
        label.style.whiteSpace = "pre-line";
        return;
      }
      const mode = p.on_loss;
      if (mode === "freeze" || (mode === "freeze_then_transparent" && Date.now() - since < p.loss_timeout * 1000 && state.lastFrame)) return;
      canvas.style.visibility = "hidden";
      if (mode === "black") showLabel("", "#000000");
      else if (mode === "placeholder") showLabel(p.placeholder_text || tr(ctx).noSignal, "#000000");
      else showLabel("");
    };
    node._dispose = () => {
      state.disposed = true;
      if (state.abort) state.abort.abort();
      if (state.timer) clearInterval(state.timer);
    };
  };
})();
