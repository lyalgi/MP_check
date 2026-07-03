# MP_check — оценка товара по фото на основе статистики Wildberries

Инструмент закупщика розничной сети: по **фото товара** и закупочной цене оценивает,
**стоит ли брать его на тест**, используя данные Wildberries (через MPStats) как датчик
потребительского спроса.

- Опознаёт «вид» товара по фото (AI-подбор похожих; для одежды — по принту, для предметов — по форме/размеру).
- Читает реальный спрос ВБ (заказы, выкупы, цена, тренд), считает наценку и размер тестовой партии.
- Честно помечает, когда вид не распознан; вердикт = **гипотеза на тест**, а не готовое решение.

Подробнее о методологии — `prototype/docs/МЕТОДОЛОГИЯ_v2.md`, о структуре — `PROJECT_STRUCTURE_NOTES.md`.

## Локальный запуск

```bash
cd prototype
python -m venv .venv
# Windows:  .\.venv\Scripts\Activate.ps1
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
python -m saol2.serve            # → http://localhost:8765
```

## Токен MPStats (обязательно)

Код берёт токен из переменной окружения **`MPSTATS_TOKEN`**, иначе из файла `API.md` в корне.

> ⚠️ `API.md` содержит секретный токен и **исключён из репозитория** (`.gitignore`).
> Для локального запуска создайте `API.md` в корне с токеном внутри, либо задайте
> `MPSTATS_TOKEN` в окружении. В облаке (Render и т.п.) задавайте переменную
> `MPSTATS_TOKEN` — **не** коммитьте токен в репозиторий.

## Развёртывание на Render

- **Root Directory:** `prototype`
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn saol2.web_app:app --host 0.0.0.0 --port $PORT`
- **Environment:** переменная `MPSTATS_TOKEN` = ваш токен

## Отдельный модуль

`wb_visual_search.py` — самостоятельный (без остального проекта) поиск похожих товаров ВБ
по фото: `python wb_visual_search.py photo.jpg`. Зависимости: `requests`, `cryptography`.
