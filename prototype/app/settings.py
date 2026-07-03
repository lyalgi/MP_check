from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SAOL_", extra="ignore")

    analytics_provider: str = "wb_public"            # wb_public | mock
    visual_search_provider: str = "wb_photo"         # wb_photo | mock | http
    visual_search_url: str = ""                      # эндпоинт реального визуального поиска WB
    min_feedbacks: int = 10                          # см. ТЗ, шаг 1: отсечка карточек
    # экономия прокси-трафика (сервер↔WB): картинку в визуальный поиск ужимаем,
    # карточки тянем только по ближайшим N результатам (рычаг цена/качество)
    visual_upload_max_px: int = 800                  # макс. сторона JPEG для визуального поиска WB
    visual_upload_quality: int = 75                  # JPEG-качество для WB upload
    # карточки с отзывами в visual-выдаче РАЗРЕЖЕНЫ (из 256 только ~5 с feedbacks),
    # и часто ранжируются за 100 → жёсткий кап ломал категорию (LOW_FEEDBACKS/
    # HETEROGENEOUS). Берём почти всё; экономию прокси даём кэшем карточек, не капом.
    visual_max_cards: int = 300                      # верхняя граница (WB отдаёт ~256)

    db_path: str = "./data/saol.db"

    host: str = "0.0.0.0"
    port: int = 8088

    rating_green: float = 0.6
    rating_yellow: float = 0.3
    # абсолютный спрос: при стольких продажах/мес на WB товар ликвиден сам по себе,
    # даже если лидеры ниши продают в разы больше (защита от false RED в top-heavy)
    absolute_demand_target_month: float = 400.0
    request_timeout_seconds: float = 10.0
    slow_lookup_ms: int = 25_000
    snapshot_min_interval_hours: int = 6
    snapshot_min_days_for_velocity: float = 1.0
    benchmark_cache_max_age_hours: int = 168   # кэш топ-N ниши: свежесть 7 дней

    classifiers_dir: str = "./classifiers"

    @property
    def db_url(self) -> str:
        if self.db_path == ":memory:":
            return "sqlite:///:memory:"
        return f"sqlite:///{Path(self.db_path).resolve()}"


settings = Settings()
