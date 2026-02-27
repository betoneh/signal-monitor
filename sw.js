const CHECK_TIMES = [
  { hour: 8, minute: 15 },
  { hour: 20, minute: 15 }
];
const TZ = 'America/Mexico_City';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
  scheduleNext();
});

function scheduleNext() {
  const now = new Date();
  const nowCST = new Date(now.toLocaleString('en-US', { timeZone: TZ }));
  let nearest = Infinity;

  for (const t of CHECK_TIMES) {
    let target = new Date(nowCST);
    target.setHours(t.hour, t.minute, 0, 0);
    if (target <= nowCST) target.setDate(target.getDate() + 1);
    const diff = target - nowCST;
    if (diff < nearest) nearest = diff;
  }

  setTimeout(() => {
    showNotification();
    scheduleNext();
  }, nearest);
}

async function showNotification() {
  if (navigator.setAppBadge) {
    await navigator.setAppBadge(1);
  }
  await self.registration.showNotification('Signal Monitor', {
    body: 'Nuevo reporte disponible',
    icon: 'icon-192.png',
    badge: 'icon-192.png',
    tag: 'signal-new-report',
    renotify: true,
    data: { url: '/signal-monitor/' }
  });
}

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  if (navigator.clearAppBadge) navigator.clearAppBadge();
  e.waitUntil(
    self.clients.matchAll({ type: 'window' }).then(clients => {
      for (const client of clients) {
        if (client.url.includes('signal-monitor') && 'focus' in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow(e.notification.data?.url || '/signal-monitor/');
    })
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.mode === 'navigate' && navigator.clearAppBadge) {
    navigator.clearAppBadge();
  }
  e.respondWith(fetch(e.request));
});
