'use strict';

const CACHE = 'babymonitor-v1';
const STATIC_ASSETS = [
  '/',
  '/static/app.js',
  '/static/style.css',
  '/static/hls.min.js',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  // Remove any old cache versions
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  // Never intercept: non-GET, API calls, HLS stream, WebSocket upgrades
  if (
    request.method !== 'GET' ||
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/stream/') ||
    url.pathname.startsWith('/ws/')
  ) {
    return;
  }

  // Cache-first for static assets; network-first for everything else
  const isStatic = url.pathname.startsWith('/static/') || url.pathname === '/sw.js';

  if (isStatic) {
    e.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(res => {
        if (res.ok) caches.open(CACHE).then(c => c.put(request, res.clone()));
        return res;
      }))
    );
  } else {
    // Network-first: serve fresh HTML; fall back to cache when offline
    e.respondWith(
      fetch(request).then(res => {
        if (res.ok) caches.open(CACHE).then(c => c.put(request, res.clone()));
        return res;
      }).catch(() => caches.match(request))
    );
  }
});
