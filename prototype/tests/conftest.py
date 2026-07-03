import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# изолированная in-memory БД для тестов
os.environ["SAOL_DB_PATH"] = ":memory:"
os.environ["SAOL_ANALYTICS_PROVIDER"] = "mock"
os.environ["ANTHROPIC_API_KEY"] = ""

import pytest  # noqa: E402

from app.db import Base, engine, SessionLocal  # noqa: E402


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        Base.metadata.drop_all(bind=engine)
