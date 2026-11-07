from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SavedFilter(Base):
    """A named Explore/collection filter combination (included and excluded
    values per filter section) a user can re-apply in one click. `scope`
    identifies the page kind it was saved on (e.g. "/movies"), since each
    page offers different filter sections."""

    __tablename__ = "saved_filters"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", "name", name="uq_saved_filter_name"),
    )

    id         : Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id    : Mapped[int]      = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    scope      : Mapped[str]      = mapped_column(String(128), nullable=False)
    name       : Mapped[str]      = mapped_column(String(64), nullable=False)
    # {"genre": ["Horror"], "exclude_genre": ["Comedy"], "lang": ["en"], ...}
    params     : Mapped[dict]     = mapped_column(JSONB, nullable=False)
    created_at : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at : Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
