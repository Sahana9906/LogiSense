import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from logisense.db import Base


@pytest.fixture()
def session():
    # StaticPool + check_same_thread=False: FastAPI's TestClient runs
    # requests in a worker thread, and a bare sqlite:///:memory: engine
    # opens a fresh (empty) DB per connection/thread otherwise.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
