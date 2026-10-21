import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
import pytest
from app.models import Base, engine


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
