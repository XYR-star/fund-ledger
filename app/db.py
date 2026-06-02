from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from .config import ensure_data_dirs, settings


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    ensure_data_dirs()
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
