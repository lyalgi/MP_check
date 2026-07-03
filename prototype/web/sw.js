// Сервис-воркер выключатель.
// Прошлый SW кэшировал оболочку по принципу «сначала кэш» и залипал на старой версии.
// Этот SW при активации: чистит все кэши, разрегистрирует себя и
// перезагружает открытые вкладки — после чего страница грузится напрямую
// с сети, без посредника. PWA-офлайн для прототипа не нужен.
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
    await self.clients.claim();
    try {
      await self.registration.unregister();
    } catch (e) { /* игнорируем */ }
    const clients = await self.clients.matchAll({ type: "window" });
    for (const client of clients) {
      try { client.navigate(client.url); } catch (e) { /* игнорируем */ }
    }
  })());
});

// Ничего не перехватываем — все запросы идут напрямую в сеть.
