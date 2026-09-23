from __future__ import annotations

from app.db.base import Base
from app.db.bootstrap import ensure_reference_data
from app.db.session import get_engine


def init_db(database_url: str | None = None) -> None:
    # Import models here so metadata is fully populated before create_all runs.
    from app.db import models  # noqa: F401
    from app.db.session import get_db_session

    engine = get_engine(database_url)
    Base.metadata.create_all(bind=engine)

    with get_db_session(database_url) as session:
        ensure_reference_data(session)
