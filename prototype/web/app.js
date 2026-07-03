(() => {
  const $ = (id) => document.getElementById(id);
  let lastPrice = 0;

  const imageInput = $("image");
  const galleryInput = $("image-gallery");
  const imagePreview = $("image-preview");
  const imageClear = $("image-clear");
  const priceInput = $("price");
  const seedUrl = $("seed-url");
  const queryInput = $("query");
  const submitBtn = $("submit");
  const errorBox = $("error");
  const formCard = $("form");
  const resultCard = $("result");
  const resetBtn = $("reset");

  // Вердикт крупно — на языке закупщика, а не «ликвидный/нелеквид».
  // Новинка из Китая = гипотеза. Любой положительный вердикт = РАЗМЕР ТЕСТА,
  // а не «закупать как проверенный товар».
  const VERDICT = {
    STRONG:  { label: "БРАТЬ — БОЛЬШОЙ ТЕСТ", sub: "Очень сильный спрос на Wildberries" },
    GREEN:   { label: "БРАТЬ НА ТЕСТ",        sub: "Хороший спрос на Wildberries" },
    YELLOW:  { label: "МАЛЫЙ ТЕСТ",           sub: "Спрос есть — возьми чуть-чуть на пробу" },
    RED:     { label: "НЕ БРАТЬ",             sub: "Спрос на Wildberries слабый" },
    UNKNOWN: { label: "НЕТ ДАННЫХ",           sub: "Не удалось оценить — переснимите фото" },
  };

  // Заметки — только человеко-понятные предупреждения. Без OZON, снапшотов,
  // долей рынка и прочей кухни.
  const NOTE_LABELS = {
    LOW_MARGIN: "Маржа тонкая — цена закупа близка к цене на WB",
    HIGH_STOCK_PRESSURE: "На WB много остатков — товар может оседать",
    DECLINING_TREND: "Спрос снижается",
    OFFLINE_DECLINING: "Продажи похожего товара падают год к году",
    LOW_OFFLINE_PROFITABILITY: "У похожих товаров слабая рентабельность",
    HEURISTIC_BENCHMARK: "Оценка по похожим товарам — точного топа ниши нет",
    LOW_SAMPLE: "Мало данных по товару — это ориентир, не точная цифра",
    DEAD_NICHE: "В этой нише почти не продают",
    LEADERS_CARRY: "Спрос держат лидеры типа — типичная карточка слабее (спрос на тип есть)",
    HETEROGENEOUS_SUBJECTS: "Категория определилась нечётко — переснимите фото",
    CATEGORY_UNRESOLVED: "Категория не определена — переснимите фото",
    TOP_HEAVY_CATEGORY: "Спрос держат несколько лидеров — новичку тяжело войти",
  };

  // Сжатие фото в браузере перед отправкой: 5-8 МБ → ~150-350 КБ. Критично для
  // слабого/трансграничного интернета. HEIC браузер не жмёт → шлём оригинал.
  const MAX_UPLOAD_PX = 1280;
  function compressImage(file) {
    return new Promise((resolve) => {
      if (!file || !file.type || !file.type.startsWith("image/") || /heic|heif/i.test(file.type)) {
        return resolve(null);
      }
      const objUrl = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(objUrl);
        const scale = Math.min(1, MAX_UPLOAD_PX / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        canvas.toBlob((blob) => resolve(blob && blob.size < file.size ? blob : null), "image/jpeg", 0.8);
      };
      img.onerror = () => { URL.revokeObjectURL(objUrl); resolve(null); };
      img.src = objUrl;
    });
  }

  // ── Поиск по фото НА ТЕЛЕФОНЕ закупщика ───────────────────────────────
  // WB search-by-photo (uploadsearch) разрешает CORS с нашего домена, поэтому
  // фото ищем прямо с телефона (его мобильный IP) — снимаем самый банимый запрос
  // с серверного IP и экономим прокси. Подпись — порт из chrome-расширения WB
  // (ключ уже публичен). Любой сбой → null → сервер сделает поиск сам (фолбэк).
  const WB_ENCODED_KEY = new Uint8Array([84,7,81,11,3,86,84,91,82,0,85,86,83,3,83,94,4,10,2,15,6,3,81,90,7,5,7,4,1,82,5,87,4,85,89,80,82,0,89,7,85,87,5,12,87,6,82,9,90,2,84,85,2,86,84,1,1,84,83,83,84,7,82,94]);
  const WB_SALT = new TextEncoder().encode("b723375b3aac60afa239c149");
  function wbDecodeKey() {
    const o = new Uint8Array(WB_ENCODED_KEY.length);
    for (let i = 0; i < o.length; i++) o[i] = WB_ENCODED_KEY[i] ^ WB_SALT[i % WB_SALT.length];
    return o;
  }
  let _wbKey = null;
  function wbAesKey() {
    if (!_wbKey) {
      _wbKey = crypto.subtle.digest("SHA-256", wbDecodeKey())
        .then((d) => crypto.subtle.importKey("raw", d, { name: "AES-CTR" }, false, ["encrypt"]));
    }
    return _wbKey;
  }
  function b64(bytes) { let s = ""; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); }
  async function wbSignature(message) {
    const key = await wbAesKey();
    let payload = new TextEncoder().encode(message);
    let out = "";
    for (let i = 0; i < 3; i++) {
      const iv = crypto.getRandomValues(new Uint8Array(16));
      const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-CTR", counter: iv, length: 128 }, key, payload));
      const combined = new Uint8Array(16 + ct.length);
      combined.set(iv); combined.set(ct, 16);
      out = b64(combined);
      payload = new TextEncoder().encode(out);
    }
    return out;
  }
  function uuid4() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = crypto.getRandomValues(new Uint8Array(1))[0] % 16;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }
  async function phoneVisualSearch(blob) {
    if (!(window.crypto && crypto.subtle)) return null;   // нет WebCrypto (http?) → сервер
    try {
      const ru = uuid4();
      const sig = await wbSignature("RequestUUID:" + ru);
      const fd = new FormData();
      fd.append("image", blob, "photo.jpg");
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 35000);
      try {
        const r = await fetch("https://search-by-photo.wb.ru/uploadsearch", {
          method: "POST",
          headers: { "Signature": sig, "RequestUUID": ru, "test-properties": "ab_testing=false", "userid": "0" },
          body: fd,
          signal: ctl.signal,
        });
        if (!r.ok) return null;
        const d = await r.json();
        if (d.status !== "OK") return null;
        return (d.result || []).map((x) => parseInt(x.im_name, 10)).filter((n) => n > 0);
      } finally { clearTimeout(t); }
    } catch (e) { return null; }
  }

  // Текстовый поиск WB (как поле «Уточните, что ищете» в приложении WB). CORS: *.
  // Возвращает множество nm_id, подходящих под текст — пересекаем с фото-выдачей.
  async function phoneTextSearch(query) {
    try {
      const u = "https://search.wb.ru/exactmatch/ru/common/v9/search"
        + "?appType=1&curr=rub&dest=-1257786&resultset=catalog&sort=popular&spp=30&query="
        + encodeURIComponent(query);
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 20000);
      try {
        const r = await fetch(u, { signal: ctl.signal });
        if (!r.ok) return null;
        const d = await r.json();
        const ps = (d.data && d.data.products) || d.products || [];
        const s = new Set(ps.map((p) => parseInt(p.id, 10)).filter((n) => n > 0));
        return s.size ? s : null;
      } finally { clearTimeout(t); }
    } catch (e) { return null; }
  }

  // Фото может прийти из камеры (#image) или из галереи (#image-gallery).
  function currentFile() {
    return (imageInput.files && imageInput.files[0])
        || (galleryInput.files && galleryInput.files[0]) || null;
  }
  function onPick(picked) {
    // показываем превью выбранного; второй input очищаем, чтобы был один источник
    if (picked === "camera") galleryInput.value = "";
    else imageInput.value = "";
    const f = currentFile();
    imagePreview.innerHTML = "";
    if (f) {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(f);
      imagePreview.appendChild(img);
      imagePreview.classList.remove("hidden");
      imageClear.classList.remove("hidden");
    } else {
      imagePreview.classList.add("hidden");
      imageClear.classList.add("hidden");
    }
  }
  imageInput.addEventListener("change", () => onPick("camera"));
  galleryInput.addEventListener("change", () => onPick("gallery"));

  function clearPhoto() {
    imageInput.value = "";
    galleryInput.value = "";
    imagePreview.innerHTML = "";
    imagePreview.classList.add("hidden");
    imageClear.classList.add("hidden");
  }
  imageClear.addEventListener("click", clearPhoto);

  function showError(msg) { errorBox.textContent = msg; errorBox.classList.remove("hidden"); }
  function clearError() { errorBox.classList.add("hidden"); errorBox.textContent = ""; }

  const REQUEST_TIMEOUT_MS = 80_000;
  let progressTimer = null;
  function startProgress() {
    const t0 = Date.now();
    progressTimer = setInterval(() => {
      submitBtn.textContent = `Оцениваю… ${Math.round((Date.now() - t0) / 1000)} с`;
    }, 1000);
  }
  function stopProgress() { if (progressTimer) { clearInterval(progressTimer); progressTimer = null; } }

  async function submit() {
    clearError();
    const price = parseFloat(priceInput.value);
    if (!Number.isFinite(price) || price <= 0) return showError("Укажите закупочную цену");
    lastPrice = price;
    const file = currentFile();
    const url = (seedUrl.value || "").trim();
    if (!file && !url) return showError("Сфотографируйте товар или вставьте ссылку с WB");

    submitBtn.disabled = true;
    submitBtn.textContent = "Оцениваю…";
    startProgress();

    const q = (queryInput.value || "").trim();
    const fd = new FormData();
    fd.append("purchase_price", String(price));
    if (file) {
      const blob = (await compressImage(file).catch(() => null)) || file;
      let nmIds = await phoneVisualSearch(blob);     // фото-поиск (мобильный IP)
      if (nmIds && nmIds.length && q) {
        const textIds = await phoneTextSearch(q);    // + текст (как «Уточните» в WB)
        if (textIds) {
          const inter = nmIds.filter((n) => textIds.has(n));
          if (inter.length >= 3) nmIds = inter;      // сужаем фото→текст; мало пересечений → не сужаем
        }
      }
      if (nmIds && nmIds.length) fd.append("nm_ids", nmIds.join(","));
      else fd.append("image", blob, "photo.jpg");    // резервный путь: визуальный поиск на сервере
    }
    if (url) fd.append("seed_url", url);
    if (q) fd.append("query", q);
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const r = await fetch("api/v1/lookup", { method: "POST", body: fd, signal: controller.signal });
      if (!r.ok) throw new Error("Сервис не ответил. Попробуйте ещё раз.");
      renderResult(await r.json());
    } catch (e) {
      showError(e.name === "AbortError"
        ? "Долго нет ответа. Повторите — поиск по фото иногда не отвечает с первого раза."
        : (e.message || "Ошибка. Попробуйте ещё раз."));
    } finally {
      clearTimeout(timeoutId);
      stopProgress();
      submitBtn.disabled = false;
      submitBtn.textContent = "Оценить";
    }
  }

  const MONTHS_RU = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"];
  function renderSeason(boxId, s, title) {
    const box = $(boxId);
    box.innerHTML = "";
    if (!s || !s.monthly) { box.classList.add("hidden"); return; }
    const hi = Math.max(...s.monthly) || 1;
    const peakIdx = s.peak_month - 1, curIdx = s.current_month - 1;
    const bars = s.monthly.map((v, i) => {
      const h = Math.max(4, Math.round((v / hi) * 64));
      let cls = "sbar";
      if (i === peakIdx) cls += " peak";
      if (i === curIdx) cls += " cur";
      const val = (i === peakIdx || i === curIdx) ? `<b class="sval">${Math.round(v)}</b>` : `<b class="sval empty"></b>`;
      return `<div class="sb">${val}<i class="${cls}" style="height:${h}px"></i><span>${MONTHS_RU[i]}</span></div>`;
    }).join("");
    const pct = Math.round((s.current_ratio || 0) * 100);
    const phase = pct >= 80 ? "пик сезона" : pct >= 50 ? "средний сезон" : "низкий сезон";
    box.innerHTML = `<div class="season-title">${title} · по ${s.based_on} товарам</div>`
      + `<div class="spark">${bars}</div>`
      + `<div class="season-note">📈 Пик продаж — <b>${s.peak_label}</b>. Сейчас (${s.current_label}) — ${pct}% от пика, ${phase}.</div>`;
    box.classList.remove("hidden");
  }

  function renderResult(d) {
    const v = d.verdict || "UNKNOWN";
    const info = VERDICT[v] || VERDICT.UNKNOWN;
    const vEl = $("verdict");
    vEl.className = "verdict " + v;
    vEl.textContent = info.label;
    $("verdict-sub").textContent = info.sub;

    const rub = (x) => Math.round(x).toLocaleString("ru-RU");

    // ── Ярлык широты: узнан ли ИМЕННО этот вид, или оценка широкая (по категории) ──
    const scopeNote = $("scope-note");
    const subjName = d.wb_subject_name
      ? (d.wb_parent_name ? `${d.wb_parent_name} / ${d.wb_subject_name}` : d.wb_subject_name) : "категории";
    if (d.niche_scope === "type") {
      scopeNote.className = "scope-note broad";
      scopeNote.textContent = `⚠ Вид не распознан — оценка ШИРОКАЯ, по «${subjName}», а не по этому товару`;
      scopeNote.classList.remove("hidden");
    } else if (d.niche_scope === "vid") {
      scopeNote.className = "scope-note narrow";
      scopeNote.textContent = "✓ Оценка по этому виду товара";
      scopeNote.classList.remove("hidden");
    } else {
      scopeNote.classList.add("hidden");
    }

    // ── Момент САМОГО товара (если он уже на WB) — самый прямой сигнал, наверх ──
    const seedBox = $("seed-box");
    seedBox.innerHTML = "";
    if (d.seed && (d.seed.orders_month != null || d.seed.redeemed_month != null)) {
      const s = d.seed;
      const img = s.image ? `<img class="seed-img" src="${s.image}" alt="">` : "";
      const dem = s.orders_month != null ? Math.round(s.orders_month) : Math.round(s.redeemed_month);
      let recent = "";
      if (s.orders_30d != null && dem > 0) {
        const r = s.orders_30d;
        const arrow = r > dem * 1.2 ? "↑ растёт" : r < dem * 0.8 ? "↓ остывает" : "";
        if (arrow) recent = ` · за 30д ${r} ${arrow}`;
      }
      seedBox.innerHTML = `${img}<div class="seed-info"><div class="seed-cap">ЭТОТ ТОВАР НА WB</div>`
        + `<div class="seed-demand"><b>${dem} зак/мес</b>${recent}</div>`
        + `<div class="seed-name">${s.name || ""}</div></div>`;
      seedBox.classList.remove("hidden");
    } else {
      seedBox.classList.add("hidden");
    }

    // ── 4 ключевые строки (быстрое считывание) ──
    // Тест — стартовая партия от объёма рынка похожих (не от числа точек).
    const units = d.test_units;
    if (units != null && units > 0) {
      $("k-test").textContent = `${units} шт`;
      $("k-test-sub").textContent = d.test_capital ? `риск ~${rub(d.test_capital)} ₽` : "";
    } else {
      $("k-test").textContent = (v === "RED") ? "0 шт" : "—";
      $("k-test-sub").textContent = "";
    }

    const month = Math.round(d.wb_demand_units_month || 0);     // выкупы типичной
    const ordersM = Math.round(d.wb_orders_units_month || 0);   // заказы типичной
    const leadM = Math.round(d.lead_orders_month || 0);         // заказы лидеров типа
    const word = ({ GREEN: "высокий", YELLOW: "средний", RED: "слабый" })[d.wb_demand_verdict] || "—";
    const buyout = d.buyout_pct_median != null ? Math.round(d.buyout_pct_median) : null;
    // если спрос держат лидеры (типичная слабее) — в заголовок выносим лидеров, чтобы не было
    // противоречия «примеры 66/мес ↔ вердикт». Иначе — типичная.
    const leadCarry = (d.verdict_reasons || []).includes("LEADERS_CARRY");
    $("k-demand").textContent = (leadCarry && leadM > 0)
      ? `${word} · лидеры ~${leadM} зак/мес`
      : (ordersM > 0 ? `${word} · ${ordersM} зак/мес` : word);

    // Ниша = MPStats similar ≈ категория, поэтому «выручка типа» (а не позиция) — главный сигнал спроса
    $("k-cat").textContent = d.niche_revenue_month != null ? `~${rub(d.niche_revenue_month)} ₽/мес` : "—";

    const mp = d.market_price_median;
    const markup = (d.markup != null) ? d.markup : ((mp && lastPrice > 0) ? (mp / lastPrice) : null);
    $("k-markup").textContent = markup ? `×${markup.toFixed(1)}` : "—";

    // ── Заметки-предупреждения (видны сразу, под ключевыми) ──
    const notes = $("notes");
    notes.innerHTML = "";
    const seen = new Set();
    for (const code of d.verdict_reasons || []) {
      const txt = NOTE_LABELS[code];
      if (txt && !seen.has(txt)) {
        seen.add(txt);
        const li = document.createElement("li");
        li.textContent = txt;
        notes.appendChild(li);
      }
    }

    // ── Подробнее (детали) ──
    const trendPct = d.trend_ratio != null
      ? `${d.trend_ratio >= 1 ? "+" : "−"}${Math.abs(Math.round((d.trend_ratio - 1) * 100))}% г/г` : "";
    $("d-score").textContent = d.liquidity_score != null
      ? `${Math.round(d.liquidity_score)}/100 (спрос × маржа × тренд)` : "—";
    $("d-niche-rev").textContent = d.niche_revenue_month != null ? `~${rub(d.niche_revenue_month)} ₽/мес` : "—";
    $("d-price-seg").textContent = d.price_segment
      ? `${rub(d.price_segment.low)}–${rub(d.price_segment.high)} ₽ (${d.price_segment.share}% выкупов)` : "—";
    const sp = d.size_spread;
    $("d-size").textContent = sp
      ? `${sp.lo}–${sp.hi} ${sp.unit}${sp.filtered_to ? ` · отфильтровано по ~${sp.filtered_to} ${sp.unit}` : ""}`
      : "—";
    $("d-trend").textContent = d.trend_label ? `${d.trend_label}${trendPct ? ` (${trendPct})` : ""}` : "нет данных";
    $("d-orders").textContent = ordersM > 0
      ? `~${ordersM} зак/мес${buyout != null ? ` · выкуп ${buyout}%` : ""}` : "—";
    $("d-lead").textContent = leadM > 0
      ? `~${leadM} зак/мес${d.lead_revenue_month ? ` · ~${rub(d.lead_revenue_month)} ₽/мес` : ""}` : "—";
    $("d-demand").textContent = month > 0
      ? `~${month} вык/мес${d.ratio_to_top != null ? ` · ${Math.round(d.ratio_to_top * 100)}% от топ-10` : ""}` : "—";
    $("d-price").textContent = mp != null ? `${Math.round(mp)} ₽` : "—";
    $("d-pot-margin").textContent = d.potential_margin != null ? `~${rub(d.potential_margin)} ₽` : "—";
    const subj = d.wb_subject_name
      ? (d.wb_parent_name ? `${d.wb_parent_name} / ${d.wb_subject_name}` : d.wb_subject_name) : "—";
    $("d-category").textContent = subj;
    $("d-razno-markup").textContent = d.retail_history_markup != null
      ? `×${d.retail_history_markup.toFixed(2)}`
      + (d.retail_history_profitability != null ? ` · рентаб. ${d.retail_history_profitability.toFixed(0)}%` : "")
      : "нет данных";

    renderSeason("season-box", d.seasonality, "Сезонность похожих");
    renderSeason("season-cat-box", d.category_seasonality, "Сезонность всей категории на WB");

    const ex = $("examples");
    ex.innerHTML = "";
    for (const a of (d.examples || []).slice(0, 6)) {
      const price = a.sale_price ?? a.price ?? 0;
      const ord = a.orders_month != null ? `${Math.round(a.orders_month)} зак/мес`
        : (a.redeemed_month != null ? `${Math.round(a.redeemed_month)} вык/мес`
        : (a.feedbacks != null ? `${a.feedbacks} отзывов` : ""));
      const bo = a.buyout_pct != null ? ` · выкуп ${Math.round(a.buyout_pct)}%` : "";
      const li = document.createElement("li");
      li.className = "ex-item";
      const img = a.image ? `<img class="ex-img" src="${a.image}" loading="lazy" alt="">` : "";
      li.innerHTML = `<a href="${a.url}" target="_blank" rel="noopener">${img}`
        + `<span class="ex-body"><span class="ex-name">${a.name || a.nm_id}</span>`
        + `<span class="meta">${Math.round(price)} ₽ · ${ord}${bo}</span></span></a>`;
      ex.appendChild(li);
    }

    formCard.classList.add("hidden");
    resultCard.classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  resetBtn.addEventListener("click", () => {
    resultCard.classList.add("hidden");
    formCard.classList.remove("hidden");
    queryInput.value = "";
    clearPhoto();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  submitBtn.addEventListener("click", submit);

  // Service worker не используем (раньше режим «сначала кэш» залипал). Сносим старые
  // регистрации и кэши; свежесть статики держим версией ?v= и no-store на сервере.
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.getRegistrations().then((rs) => rs.forEach((r) => r.unregister())).catch(() => {});
  }
  if (window.caches && caches.keys) {
    caches.keys().then((ks) => ks.forEach((k) => caches.delete(k))).catch(() => {});
  }
})();
