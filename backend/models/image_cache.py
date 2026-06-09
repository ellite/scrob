from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ImageCache(Base):
    __tablename__ = "image_cache"

    id            : Mapped[int]      = mapped_column(Integer, primary_key=True)
    path          : Mapped[str]      = mapped_column(String(255), nullable=False)
    size          : Mapped[str]      = mapped_column(String(50), nullable=False)
    image_type    : Mapped[str]      = mapped_column(String(50), nullable=False, default="ondemand")
    file_size     : Mapped[int]      = mapped_column(Integer, nullable=False)
    last_accessed : Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at    : Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("uq_image_cache_path_size", "path", "size", unique=True),
        Index("idx_image_cache_type_accessed", "image_type", "last_accessed"),
    )
