// Minimal service worker: makes the page installable and keeps the page itself available
// offline. Network first; API calls, downloads and the certificate always go to the network.
const CACHE = 'sns-v1';

self.addEventListener('install', () => { self.skipWaiting(); });

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n.startsWith('sns-') && n !== CACHE).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/files/') || url.pathname.startsWith('/cert.')) return;
  event.respondWith(
    fetch(request)
      .then((r) => {
        if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((cache) => cache.put(request, copy)); }
        return r;
      })
      .catch(() => caches.match(request).then((m) => m || Response.error()))
  );
});
