from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from .config import settings


database_url = f"sqlite:///{settings.db_path}"
engine = create_engine(database_url, connect_args={"check_same_thread": False}, echo=False)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    with Session(engine) as session:
        yield session
