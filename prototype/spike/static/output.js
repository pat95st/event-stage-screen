/* Spike output page: renders a representative scene, plays a video transport
 * and reports frame/latency metrics to the server every few seconds.
 *
 * Query parameters:
 *   transport = none | mjpeg | webrtc   (default none)
 *   source    = test | <NDI source name> (default test)
 *   format    = WIDTHxHEIGHT@FPS         (default 1280x720@30)
 *   codec     = h264 | vp8               (webrtc only, default h264)
 *   load      = light | heavy            (extra animated elements)
 *   vrotate   = degrees for the video element (default 0)
 *   vflip     = 1 to mirror the video element horizontally
 */
(() => {
  "use strict";

  const params = new URLSearchParams(location.search);
  const output = Number(location.pathname.split("/").pop()) || 1;
  const cfg = {
    transport: params.get("transport") || "none",
    source: params.get("source") || "test",
    format: params.get("format") || "1280x720@30",
    codec: params.get("codec") || "h264",
    load: params.get("load") || "light",
    vrotate: Number(params.get("vrotate") || 0),
    vflip: params.get("vflip") === "1",
  };

  document.title = `ess-output-${output}`;
  document.getElementById("label").textContent = `Output ${output} · ${cfg.transport} · ${cfg.source}`;

  const $ = (id) => document.getElementById(id);
  const videoBox = $("video-box");
  const video = $("video");
  const canvas = $("canvas");
  const placeholder = $("video-placeholder");
  videoBox.style.transform = `rotate(${cfg.vrotate}deg) scaleX(${cfg.vflip ? -1 : 1})`;

  if (cfg.load === "heavy") {
    const load = $("load");
    for (let i = 0; i < 24; i++) {
      const box = document.createElement("div");
      box.className = "box";
      box.style.left = `${(i * 37) % 95}vw`;
      box.style.top = `${(i * 53) % 90}vh`;
      box.style.animationDelay = `${-i * 0.2}s`;
      load.appendChild(box);
    }
  }

  // ---------------------------------------------------------------- clock
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  const clockText = (d) =>
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;

  const countdownStart = Date.now();
  const countdownMs = 5 * 60 * 1000;

  // ------------------------------------------------------- frame metrics
  const frame = { count: 0, last: 0, longFrames: 0, minFps: Infinity, windowStart: performance.now(), windowFrames: 0 };
  const video_ = { transport: cfg.transport, frames: 0, dropped: 0, latencies: [], extra: {} };

  function tick(now) {
    if (frame.last) {
      const dt = now - frame.last;
      if (dt > 1000 / 60 * 2.5) frame.longFrames++;
    }
    frame.last = now;
    frame.count++;
    frame.windowFrames++;

    const d = new Date();
    $("clock").textContent = clockText(d);
    const left = Math.max(0, countdownMs - (Date.now() - countdownStart));
    $("countdown").textContent = `${pad(Math.floor(left / 60000))}:${pad(Math.floor(left / 1000) % 60)}`;
    $("progress-bar").style.transform = `scaleX(${1 - left / countdownMs})`;
    $("progress-bar").style.width = "100%";

    if (mjpeg.pending) mjpeg.draw();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // --------------------------------------------------------------- MJPEG
  const mjpeg = {
    pending: null,       // {ts, bitmap}
    decoding: false,
    latest: null,        // {ts, jpeg} waiting for decode
    ctx: null,

    async start() {
      placeholder.hidden = true;
      video.hidden = true;
      this.ctx = canvas.getContext("2d");
      const url = `/stream/mjpeg?source=${encodeURIComponent(cfg.source)}&format=${cfg.format}`;
      for (;;) {
        try {
          const res = await fetch(url, { cache: "no-store" });
          const reader = res.body.getReader();
          let buf = new Uint8Array(0);
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            const merged = new Uint8Array(buf.length + value.length);
            merged.set(buf);
            merged.set(value, buf.length);
            buf = merged;
            while (buf.length >= 12) {
              const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
              const len = view.getUint32(0);
              const ts = view.getFloat64(4);
              if (buf.length < 12 + len) break;
              const jpeg = buf.slice(12, 12 + len);
              buf = buf.slice(12 + len);
              if (this.latest) video_.dropped++;
              this.latest = { ts, jpeg };
              this.decode();
            }
          }
        } catch (err) {
          console.warn("mjpeg stream error", err);
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    },

    async decode() {
      if (this.decoding || !this.latest) return;
      this.decoding = true;
      const { ts, jpeg } = this.latest;
      this.latest = null;
      try {
        const bitmap = await createImageBitmap(new Blob([jpeg], { type: "image/jpeg" }));
        if (this.pending) { this.pending.bitmap.close(); video_.dropped++; }
        this.pending = { ts, bitmap };
      } finally {
        this.decoding = false;
        if (this.latest) this.decode();
      }
    },

    draw() {
      const { ts, bitmap } = this.pending;
      this.pending = null;
      if (canvas.width !== bitmap.width) { canvas.width = bitmap.width; canvas.height = bitmap.height; }
      this.ctx.drawImage(bitmap, 0, 0);
      bitmap.close();
      video_.frames++;
      video_.latencies.push(Date.now() - ts);
    },
  };

  // -------------------------------------------------------------- WebRTC
  const webrtc = {
    pc: null,
    lastStats: null,

    start() {
      canvas.hidden = true;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${location.host}/ws/webrtc?source=${encodeURIComponent(cfg.source)}` +
        `&format=${cfg.format}&codec=${cfg.codec}`;
      const ws = new WebSocket(url);
      const pc = new RTCPeerConnection({ iceServers: [] });
      this.pc = pc;
      this.lastStats = null;
      const pendingIce = [];

      pc.ontrack = (e) => {
        try { e.receiver.jitterBufferTarget = 0; } catch (_) { /* older Chromium */ }
        try { e.receiver.playoutDelayHint = 0; } catch (_) { /* not supported */ }
        video.srcObject = e.streams[0] || new MediaStream([e.track]);
        placeholder.hidden = true;
      };
      pc.onicecandidate = (e) => {
        if (e.candidate && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ ice: { candidate: e.candidate.candidate, sdpMLineIndex: e.candidate.sdpMLineIndex } }));
        }
      };
      ws.onmessage = async (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.error) { placeholder.hidden = false; placeholder.textContent = msg.error; return; }
        if (msg.sdp && msg.sdp.type === "offer") {
          await pc.setRemoteDescription(msg.sdp);
          for (const ice of pendingIce.splice(0)) {
            await pc.addIceCandidate(ice).catch((err) => console.warn("ice", err));
          }
          const answer = await pc.createAnswer();
          await pc.setLocalDescription(answer);
          ws.send(JSON.stringify({ sdp: { type: "answer", sdp: answer.sdp } }));
        } else if (msg.ice && !pc.remoteDescription) {
          pendingIce.push(msg.ice);
        } else if (msg.ice) {
          await pc.addIceCandidate(msg.ice).catch((err) => console.warn("ice", err));
        }
      };
      const restart = () => {
        pc.close();
        placeholder.hidden = false;
        setTimeout(() => this.start(), 2000);
      };
      ws.onclose = restart;
      pc.onconnectionstatechange = () => {
        if (pc.connectionState === "failed") ws.close();
      };
    },

    async collect() {
      if (!this.pc) return;
      const stats = await this.pc.getStats();
      stats.forEach((s) => {
        if (s.type !== "inbound-rtp" || s.kind !== "video") return;
        const prev = this.lastStats;
        if (prev) {
          const emitted = s.jitterBufferEmittedCount - prev.jitterBufferEmittedCount;
          const decoded = s.framesDecoded - prev.framesDecoded;
          video_.extra.jitterBufferMs = emitted > 0
            ? Math.round(((s.jitterBufferDelay - prev.jitterBufferDelay) / emitted) * 1000) : null;
          video_.extra.decodeMs = decoded > 0
            ? Math.round(((s.totalDecodeTime - prev.totalDecodeTime) / decoded) * 1000 * 10) / 10 : null;
          video_.frames += decoded;
        }
        video_.dropped = s.framesDropped || 0;
        video_.extra.fps = s.framesPerSecond || null;
        this.lastStats = s;
      });
    },
  };

  if (cfg.transport === "mjpeg") mjpeg.start();
  else if (cfg.transport === "webrtc") webrtc.start();
  else { video.hidden = true; canvas.hidden = true; }

  // ------------------------------------------------------------ reporting
  const percentile = (arr, p) => {
    if (!arr.length) return null;
    const sorted = [...arr].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))];
  };

  async function report() {
    const now = performance.now();
    const secs = (now - frame.windowStart) / 1000;
    const fps = Math.round((frame.windowFrames / secs) * 10) / 10;
    frame.windowStart = now;
    frame.windowFrames = 0;

    if (cfg.transport === "webrtc") await webrtc.collect();
    const lat = video_.latencies;
    const videoFps = cfg.transport === "mjpeg" ? Math.round((video_.frames / secs) * 10) / 10 : video_.extra.fps;
    const payload = {
      output,
      config: cfg,
      screen: { width: screen.width, height: screen.height, dpr: devicePixelRatio,
                innerWidth, innerHeight },
      fps,
      longFrames: frame.longFrames,
      video: cfg.transport === "none" ? null : {
        transport: cfg.transport,
        fps: videoFps,
        framesDropped: video_.dropped,
        latencyAvgMs: lat.length ? Math.round(lat.reduce((a, b) => a + b, 0) / lat.length) : null,
        latencyP95Ms: percentile(lat, 95),
        jitterBufferMs: video_.extra.jitterBufferMs ?? null,
        decodeMs: video_.extra.decodeMs ?? null,
      },
    };
    frame.longFrames = 0;
    if (cfg.transport === "mjpeg") video_.frames = 0;
    video_.latencies = [];

    $("stats").textContent =
      `page ${fps} fps  long ${payload.longFrames}\n` +
      (payload.video
        ? `video ${payload.video.fps ?? "–"} fps  drop ${payload.video.framesDropped}\n` +
          `lat ${payload.video.latencyAvgMs ?? "–"}/${payload.video.latencyP95Ms ?? "–"} ms  ` +
          `jb ${payload.video.jitterBufferMs ?? "–"} dec ${payload.video.decodeMs ?? "–"}`
        : "video off");

    fetch("/api/metrics", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(payload) }).catch(() => {});
  }
  setInterval(report, 5000);
})();
