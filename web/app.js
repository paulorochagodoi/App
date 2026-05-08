'use strict';

const API_TOKEN = document.querySelector('meta[name="api-token"]')?.content || '';

let alertDismissed = false;
let alertTimeout = null;
let hls = null;
let hlsRetryTimer = null;
let webrtcPc = null;
let webrtcWs = null;
let wsRetryDelay = 1000;
const WS_MAX_DELAY = 30000;

function apiFetch(url, opts = {}) {
  if (API_TOKEN) {
    opts.headers = { ...(opts.headers || {}), 'X-Api-Token': API_TOKEN };
  }
  return fetch(url, opts);
}

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(el => {
    el.classList.toggle('active', el.id === `screen-${name}`);
    el.classList.toggle('hidden', el.id !== `screen-${name}`);
  });
  document.querySelectorAll('.tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.screen === name);
  });
  if (name === 'live') startLiveStream();
}

// ── WebRTC ─────────────────────────────────────────────────────────────────
function stopWebRTC() {
  if (webrtcWs) { try { webrtcWs.close(); } catch (_) {} webrtcWs = null; }
  if (webrtcPc) { try { webrtcPc.close(); } catch (_) {} webrtcPc = null; }
  const video = document.getElementById('live-video');
  if (video.srcObject) {
    video.srcObject.getTracks().forEach(t => t.stop());
    video.srcObject = null;
  }
  setStreamMode(null);
}

async function startWebRTC() {
  if (!window.RTCPeerConnection) return false;

  stopWebRTC();
  const video = document.getElementById('live-video');

  try {
    const pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    });
    webrtcPc = pc;

    const transceiver = pc.addTransceiver('video', { direction: 'recvonly' });
    if (RTCRtpReceiver.getCapabilities) {
      const caps = RTCRtpReceiver.getCapabilities('video');
      if (caps) {
        const h264 = caps.codecs.filter(c => c.mimeType === 'video/H264');
        const rest = caps.codecs.filter(c => c.mimeType !== 'video/H264');
        if (h264.length) transceiver.setCodecPreferences([...h264, ...rest]);
      }
    }

    pc.ontrack = (event) => {
      if (event.streams[0]) {
        video.srcObject = event.streams[0];
        video.play().catch(() => {});
        document.getElementById('stream-error').classList.add('hidden');
      }
    };

    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
        stopWebRTC();
        startHls();
      }
    };

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/webrtc`);
    webrtcWs = ws;

    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = reject;
      setTimeout(reject, 5000);
    });

    pc.onicecandidate = (event) => {
      if (event.candidate && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'ice-candidate',
          candidate: event.candidate.candidate,
          sdpMLineIndex: event.candidate.sdpMLineIndex,
        }));
      }
    };

    ws.onmessage = async (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === 'answer') {
        await pc.setRemoteDescription({ type: 'answer', sdp: msg.sdp });
      } else if (msg.type === 'ice-candidate') {
        await pc.addIceCandidate({ candidate: msg.candidate, sdpMLineIndex: msg.sdpMLineIndex });
      }
    };

    ws.onclose = () => { if (!video.srcObject) { stopWebRTC(); startHls(); } };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: 'offer', sdp: offer.sdp }));

    await new Promise((resolve, reject) => {
      video.addEventListener('playing', resolve, { once: true });
      setTimeout(reject, 8000);
    });

    setStreamMode('webrtc');
    return true;
  } catch (err) {
    console.warn('WebRTC failed, falling back to HLS:', err?.message || err);
    stopWebRTC();
    return false;
  }
}

// ── HLS ────────────────────────────────────────────────────────────────────
function startHls() {
  const video = document.getElementById('live-video');
  const errorEl = document.getElementById('stream-error');

  clearTimeout(hlsRetryTimer);
  hlsRetryTimer = null;
  if (hls) { hls.destroy(); hls = null; }

  const src = '/stream/live.m3u8';

  if (Hls.isSupported()) {
    hls = new Hls({
      lowLatencyMode: true,
      backBufferLength: 0,
      maxBufferLength: 2,
      maxMaxBufferLength: 3,
      liveSyncDuration: 1,
      liveMaxLatencyDuration: 3,
      liveDurationInfinity: true,
    });
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => {});
      errorEl.classList.add('hidden');
      setStreamMode('hls');
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        errorEl.classList.remove('hidden');
        hlsRetryTimer = setTimeout(startHls, 3000);
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.removeAttribute('src');
    video.load();
    video.src = src;
    video.addEventListener('loadedmetadata', () => {
      video.play().catch(() => {});
      errorEl.classList.add('hidden');
      setStreamMode('hls');
    }, { once: true });
    video.addEventListener('error', () => {
      errorEl.classList.remove('hidden');
      hlsRetryTimer = setTimeout(startHls, 3000);
    }, { once: true });
  } else {
    errorEl.querySelector('p').textContent = 'Navegador não suporta HLS.';
    errorEl.classList.remove('hidden');
  }
}

async function startLiveStream() {
  stopWebRTC();
  if (hls) { hls.destroy(); hls = null; }
  const ok = await startWebRTC();
  if (!ok) startHls();
}

// ── WiFi ───────────────────────────────────────────────────────────────────
async function saveWifi() {
  const ssid = document.getElementById('wifi-ssid').value.trim();
  const password = document.getElementById('wifi-pwd').value;
  const status = document.getElementById('wifi-status');

  if (!ssid) {
    status.textContent = 'Informe o nome da rede (SSID)';
    status.className = 'wifi-status err';
    return;
  }

  status.textContent = 'Salvando...';
  status.className = 'wifi-status';

  try {
    const res = await apiFetch('/api/wifi/configure', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid, password }),
    });
    if (!res.ok) throw new Error(await res.text());
    status.textContent = `✓ Salvo: ${ssid}`;
    status.className = 'wifi-status ok';
  } catch (e) {
    status.textContent = 'Erro ao salvar configuração';
    status.className = 'wifi-status err';
  }
}

// ── Cry alert ──────────────────────────────────────────────────────────────
function showCryAlert(confidence) {
  if (alertDismissed) return;
  const el = document.getElementById('cry-alert');
  document.getElementById('cry-text').textContent =
    `🔴 CHORO DETECTADO ${Math.round(confidence * 100)}%`;
  el.classList.remove('hidden');

  clearTimeout(alertTimeout);
  alertTimeout = setTimeout(() => {
    el.classList.add('hidden');
    alertDismissed = false;
  }, 8000);
}

function dismissAlert() {
  document.getElementById('cry-alert').classList.add('hidden');
  alertDismissed = true;
  clearTimeout(alertTimeout);
  setTimeout(() => { alertDismissed = false; }, 30000);
}

// ── WebSocket (alerts + keepalive) ─────────────────────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/alerts`);

  ws.onopen = () => { wsRetryDelay = 1000; setConnectionStatus(true); };
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'cry') showCryAlert(msg.confidence);
    } catch (_) {}
  };
  ws.onclose = () => {
    setConnectionStatus(false);
    setTimeout(connectWebSocket, wsRetryDelay);
    wsRetryDelay = Math.min(wsRetryDelay * 2, WS_MAX_DELAY);
  };
  ws.onerror = () => ws.close();
}

function setConnectionStatus(connected) {
  const dot = document.getElementById('live-indicator');
  if (!dot) return;
  if (!connected) {
    dot.textContent = '○ RECONECTANDO';
    dot.classList.add('disconnected');
    return;
  }
  dot.classList.remove('disconnected');
  if (!dot.dataset.mode) dot.textContent = '● AO VIVO';
}

function setStreamMode(mode) {
  const dot = document.getElementById('live-indicator');
  if (!dot) return;
  dot.dataset.mode = mode || '';
  if (mode === 'webrtc') {
    dot.textContent = '● WebRTC';
    dot.title = 'Streaming via WebRTC (~100–400 ms de delay)';
  } else if (mode === 'hls') {
    dot.textContent = '● HLS';
    dot.title = 'Streaming via HLS (~2 s de delay)';
  } else {
    dot.textContent = '● AO VIVO';
    dot.title = '';
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  startLiveStream();
  connectWebSocket();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
});
