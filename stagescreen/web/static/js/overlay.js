/* event-stage-screen – setup aids drawn above everything on a kiosk output:
 * blackout, test patterns (colour bars, grid, edge markers – each with the
 * real output resolution) and "identify output" (big number for a few seconds).
 * The overlay sits outside the mirror transform, so it always shows the
 * physical output as it is.
 */
(function () {
  "use strict";

  const root = document.createElement("div");
  root.style.cssText = "position:fixed;inset:0;z-index:50;pointer-events:none";
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;display:none";
  const ident = document.createElement("div");
  ident.style.cssText = "position:absolute;inset:0;display:none;align-items:center;justify-content:center;flex-direction:column;" +
    "background:rgba(0,0,0,.55);color:#fff;font-family:'Inter','DejaVu Sans',sans-serif;text-align:center";
  const black = document.createElement("div");
  black.style.cssText = "position:absolute;inset:0;background:#000;display:none";
  root.append(canvas, ident, black);
  document.body.appendChild(root);

  let pattern = null, label = "", identifyUntil = 0, identTimer = null;

  function resolutionText() {
    const dpr = window.devicePixelRatio || 1;
    return `${Math.round(window.innerWidth * dpr)} × ${Math.round(window.innerHeight * dpr)}`;
  }

  function drawText(g, w, h, lines) {
    const size = Math.round(h / 14);
    g.font = `700 ${size}px Inter, "DejaVu Sans", sans-serif`;
    g.textAlign = "center"; g.textBaseline = "middle";
    const boxW = Math.max(...lines.map((l) => g.measureText(l).width)) + size, boxH = size * 1.4 * lines.length + size * 0.4;
    g.fillStyle = "rgba(0,0,0,.75)";
    g.fillRect((w - boxW) / 2, (h - boxH) / 2, boxW, boxH);
    g.fillStyle = "#fff";
    lines.forEach((l, i) => g.fillText(l, w / 2, h / 2 - ((lines.length - 1) / 2 - i) * size * 1.4));
  }

  function draw() {
    if (!pattern) { canvas.style.display = "none"; return; }
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(window.innerWidth * dpr), h = Math.round(window.innerHeight * dpr);
    canvas.width = w; canvas.height = h;
    canvas.style.display = "block";
    const g = canvas.getContext("2d");
    if (pattern === "bars") {
      const colors = ["#c0c0c0", "#c0c000", "#00c0c0", "#00c000", "#c000c0", "#c00000", "#0000c0"];
      colors.forEach((c, i) => { g.fillStyle = c; g.fillRect(Math.floor((i * w) / 7), 0, Math.ceil(w / 7), h * 0.67); });
      const mid = ["#0000c0", "#131313", "#c000c0", "#131313", "#00c0c0", "#131313", "#c0c0c0"];
      mid.forEach((c, i) => { g.fillStyle = c; g.fillRect(Math.floor((i * w) / 7), h * 0.67, Math.ceil(w / 7), h * 0.08); });
      for (let i = 0; i < 16; i++) { const v = Math.round((i / 15) * 255); g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(Math.floor((i * w) / 16), h * 0.75, Math.ceil(w / 16), h * 0.25); }
    } else if (pattern === "grid") {
      g.fillStyle = "#000"; g.fillRect(0, 0, w, h);
      g.strokeStyle = "#ffffff"; g.lineWidth = 1;
      const step = Math.max(8, Math.round(Math.min(w, h) / 12));
      g.beginPath();
      for (let x = 0.5; x < w; x += step) { g.moveTo(x, 0); g.lineTo(x, h); }
      for (let y = 0.5; y < h; y += step) { g.moveTo(0, y); g.lineTo(w, y); }
      g.stroke();
      g.strokeStyle = "#ffd200"; g.lineWidth = Math.max(2, dpr * 2);
      g.beginPath(); g.moveTo(w / 2, 0); g.lineTo(w / 2, h); g.moveTo(0, h / 2); g.lineTo(w, h / 2); g.stroke();
      g.beginPath(); g.arc(w / 2, h / 2, Math.min(w, h) * 0.45, 0, Math.PI * 2); g.stroke();
    } else if (pattern === "edges") {
      g.fillStyle = "#000"; g.fillRect(0, 0, w, h);
      const edge = (c, inset) => { g.strokeStyle = c; g.lineWidth = 1; g.strokeRect(inset + 0.5, inset + 0.5, w - 2 * inset - 1, h - 2 * inset - 1); };
      edge("#ff0000", 0); edge("#00ff00", 1); edge("#0080ff", 2);
      g.strokeStyle = "#ffffff"; g.lineWidth = Math.max(2, dpr * 2);
      const m = Math.min(w, h) * 0.05;  // 5 % safe-area marks
      g.strokeRect(m, m, w - 2 * m, h - 2 * m);
      g.fillStyle = "#fff";
      const a = Math.min(w, h) * 0.06;
      [[0, 0, 1, 1], [w, 0, -1, 1], [0, h, 1, -1], [w, h, -1, -1]].forEach(([x, y, sx, sy]) => {
        g.beginPath(); g.moveTo(x, y); g.lineTo(x + sx * a, y); g.lineTo(x, y + sy * a); g.closePath(); g.fill();
      });
    }
    drawText(g, w, h, [resolutionText(), label]);
  }

  function updateIdentify() {
    const left = identifyUntil - Date.now();
    ident.style.display = left > 0 ? "flex" : "none";
    clearTimeout(identTimer);
    if (left > 0) identTimer = setTimeout(updateIdentify, Math.min(left, 1000));
  }

  window.addEventListener("resize", draw);

  /** Apply the overlay part of the render state. */
  window.ESSOverlay = {
    apply(st) {
      const o = st.overlay || {};
      black.style.display = o.blackout ? "block" : "none";
      const newLabel = st.output_name || "";
      if (o.test_pattern !== pattern || newLabel !== label) { pattern = o.test_pattern || null; label = newLabel; draw(); }
      if (o.identify_ms > 0) {
        identifyUntil = Date.now() + o.identify_ms;
        ident.innerHTML = "";
        const num = document.createElement("div");
        num.textContent = String(st.output_id);
        num.style.cssText = "font-size:45vh;font-weight:800;line-height:1";
        const name = document.createElement("div");
        name.textContent = `${st.output_name} · ${resolutionText()}`;
        name.style.cssText = "font-size:5vh;font-weight:600;opacity:.9";
        ident.append(num, name);
      } else {
        identifyUntil = 0;
      }
      updateIdentify();
    },
  };
})();
