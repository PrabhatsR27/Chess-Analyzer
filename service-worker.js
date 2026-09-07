const CACHE_NAME = 'endgame-shell-v1';
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

/* ============ Cross-origin isolation (merged in from coi-serviceworker.js) ============
   This used to be a SEPARATE service worker (coi-serviceworker.js) registered at the
   same scope as this one. A scope can only ever be controlled by one service worker,
   so registering both here meant this plain offline-caching worker silently replaced
   the COI one on every load — the page's COOP/COEP headers never actually stuck, so
   window.crossOriginIsolated stayed false forever and the on-device engine always fell
   back to single-threaded, no matter how many times the page reloaded.
   Folding the header-injection into THIS worker's own fetch handler fixes that: there's
   now only one worker, doing both jobs, so nothing can un-set what it just set.
   Logic adapted from https://github.com/gzuidhof/coi-serviceworker (MIT). */
let coepCredentialless = false;

self.addEventListener('message', (ev) => {
  if (!ev.data) return;
  if (ev.data.type === 'deregister') {
    self.registration.unregister()
      .then(() => self.clients.matchAll())
      .then((clients) => clients.forEach((client) => client.navigate(client.url)));
  } else if (ev.data.type === 'coepCredentialless') {
    coepCredentialless = ev.data.value;
  }
});

// Adds the Cross-Origin-Opener-Policy / Cross-Origin-Embedder-Policy headers
// that make the page cross-origin isolated (window.crossOriginIsolated === true),
// which is what unlocks SharedArrayBuffer / multi-threaded WASM Stockfish.
function withCoiHeaders(response) {
  if (!response || response.status === 0) return response;
  const newHeaders = new Headers(response.headers);
  newHeaders.set('Cross-Origin-Embedder-Policy', coepCredentialless ? 'credentialless' : 'require-corp');
  if (!coepCredentialless) newHeaders.set('Cross-Origin-Resource-Policy', 'cross-origin');
  newHeaders.set('Cross-Origin-Opener-Policy', 'same-origin');
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: newHeaders,
  });
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Stale-while-revalidate for the app's own static files (instant repeat loads,
// still updates in the background), PLUS the COOP/COEP headers above on every
// response so the page stays cross-origin isolated. Firebase data requests are
// left completely alone — the app itself already handles caching/incremental
// sync for those — but still get the COI headers, since every response on the
// page needs to carry a compatible policy for isolation to hold.
self.addEventListener('fetch', (event) => {
  const r = event.request;
  if (r.cache === 'only-if-cached' && r.mode !== 'same-origin') return;

  const url = new URL(r.url);
  const request = (coepCredentialless && r.mode === 'no-cors')
    ? new Request(r, { credentials: 'omit' })
    : r;

  if (r.method !== 'GET' || url.hostname.endsWith('firebaseio.com')) {
    event.respondWith(
      fetch(request).then(withCoiHeaders).catch((e) => { console.error(e); return fetch(request); })
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return withCoiHeaders(res);
        })
        .catch(() => cached && withCoiHeaders(cached));
      return cached ? withCoiHeaders(cached) : network;
    })
  );
});
