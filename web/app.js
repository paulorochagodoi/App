'use strict';

// ── Config injected by server ──────────────────────────────────────────────
const API_TOKEN = document.querySelector('meta[name="api-token"]')?.content || '';

// ── State ──────────────────────────────────────────────────────────────────
let isRecording = false;
let alertDismissed = false;
let alertTimeout = null;
let hls = null;
let hlsRetryTimer = null;
let wsRetryDelay = 1000;
const WS_MAX_DELAY = 30000;

// ── Authenticated fetch ────────────────────────────────────────────────────
function apiFetch(url, opts = {}) {
  if (API_TOKEN) {
    opts.headers = { ...(opts.headers || {}), 'X-Api-Token': API_TOKEN };
  }
  return fetch(url, opts);
}

// ── Screen navigation ──────────────────────────────────────────────────────
function showScreen(name) {
  document.querySelectorAll('.screen').forEach(el => {
    el.classList.toggle('active', el.id === `screen-${name}`);
    el.classList.toggle('hidden', el.id !== `screen-${name}`);
  });
  document.querySelectorAll('.tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.screen === name);
  });

  if (name === 'recordings') loadRecordings();
  if (name === 'live') startHls();
}

// ── HLS live stream ────────────────────────────────────────────────────────
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
      maxBufferLength: 4,
      maxMaxBufferLength: 8,
      liveSyncDuration: 2,
      liveMaxLatencyDuration: 5,
      liveDurationInfinity: true,
    });
    hls.loadSource(src);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      video.play().catch(() => {});
      errorEl.classList.add('hidden');
    });
    hls.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        errorEl.classList.remove('hidden');
        hlsRetryTimer = setTimeout(startHls, 3000);
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari / iOS native HLS
    video.removeAttribute('src');
    video.load();
    video.src = src;
    video.addEventListener('loadedmetadata', () => {
      video.play().catch(() => {});
      errorEl.classList.add('hidden');
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

// ── Recording ──────────────────────────────────────────────────────────────
async function toggleRecording() {
  const btn = document.getElementById('rec-btn');
  try {
    if (!isRecording) {
      const res = await apiFetch('/api/recording/start', { method: 'POST' });
      if (!res.ok) throw new Error(await res.text());
      isRecording = true;
      btn.textContent = '⏹ PARAR';
      btn.classList.add('recording');
    } else {
      await apiFetch('/api/recording/stop', { method: 'POST' });
      isRecording = false;
      btn.textContent = '⏺ GRAVAR';
      btn.classList.remove('recording');
    }
  } catch (e) {
    console.error('Recording toggle failed:', e);
  }
}

// ── Recordings list ────────────────────────────────────────────────────────
async function loadRecordings() {
  const list = document.getElementById('recordings-list');
  list.innerHTML = '<div class="empty-list">Carregando...</div>';
  try {
    const res = await apiFetch('/api/recordings');
    const data = await res.json();
    const items = data.recordings || [];
    if (items.length === 0) {
      list.innerHTML = '<div class="empty-list">Nenhuma gravação encontrada</div>';
      return;
    }
    list.innerHTML = items.map((r, i) => {
      const meta = [formatDate(r.created), formatSize(r.size)];
      if (r.duration_s != null) meta.push(formatDuration(r.duration_s));
      return `
        <div class="rec-item">
          <div class="rec-info">
            <div class="rec-name">${r.filename}</div>
            <div class="rec-meta">${meta.join(' · ')}</div>
          </div>
          <button class="btn btn-play" data-idx="${i}">▶ Reproduzir</button>
        </div>`;
    }).join('');
    list.querySelectorAll('.btn-play').forEach(btn => {
      btn.addEventListener('click', () => playRecording(items[+btn.dataset.idx].filename));
    });
  } catch (e) {
    list.innerHTML = '<div class="empty-list">Erro ao carregar gravações</div>';
  }
}

function playRecording(filename) {
  const video = document.getElementById('playback-video');
  document.getElementById('playback-title').textContent = filename;
  video.src = `/api/recordings/${encodeURIComponent(filename)}`;
  video.load();
  video.play().catch(() => {});
  showScreen('playback');
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
  const txt = document.getElementById('cry-text');
  txt.textContent = `🔴 CHORO DETECTADO ${Math.round(confidence * 100)}%`;
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

// ── WebSocket for cry alerts (exponential backoff) ─────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/alerts`);

  ws.onopen = () => {
    wsRetryDelay = 1000;  // reset on successful connection
    setConnectionStatus(true);
  };

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
  if (dot) {
    dot.textContent = connected ? '● AO VIVO' : '○ RECONECTANDO';
    dot.classList.toggle('disconnected', !connected);
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────
function formatDate(iso) {
  try {
    return new Date(iso).toLocaleString('pt-BR', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (_) { return iso; }
}

function formatSize(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDuration(s) {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  startHls();
  connectWebSocket();

  // Register service worker for offline asset caching
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  // Sync recording state with server on load
  apiFetch('/api/status').then(r => r.json()).then(data => {
    if (data.recording) {
      isRecording = true;
      const btn = document.getElementById('rec-btn');
      btn.textContent = '⏹ PARAR';
      btn.classList.add('recording');
    }
  }).catch(() => {});
});
