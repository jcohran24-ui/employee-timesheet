const CACHE_NAME = 'employee-timesheet-v1';
const STATIC_ASSETS = [
  '/static/styles.css',
  '/static/app.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);

  if (url.origin === self.location.origin && url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      }))
    );
    return;
  }

  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/static/styles.css').then(() => new Response(
      '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Employee Timesheet</title><style>body{font-family:system-ui;padding:2rem}main{max-width:520px;margin:auto}</style></head><body><main><h1>Employee Timesheet</h1><p>You appear to be offline. Reconnect to the internet to view or submit timesheets.</p></main></body></html>',
      {headers:{'Content-Type':'text/html'}}
    ))));
  }
});
