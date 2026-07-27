import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Integer, String, func, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CollectionSource

class SyncStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"

class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id             : Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id        : Mapped[int]            = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    source         : Mapped[CollectionSource] = mapped_column(Enum(CollectionSource), nullable=False)
    status         : Mapped[SyncStatus]     = mapped_column(Enum(SyncStatus), default=SyncStatus.pending, nullable=False)
    
    total_items     : Mapped[int]           = mapped_column(Integer, default=0)
    processed_items : Mapped[int]           = mapped_column(Integer, default=0)
    errors          : Mapped[int]           = mapped_column(Integer, default=0)
    
    error_message   : Mapped[Optional[str]] = mapped_column(String(1000))
    stats           : Mapped[Optional[dict]] = mapped_column(JSONB)
    warnings        : Mapped[Optional[list]] = mapped_column(JSONB)

    connection_id   : Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("media_server_connections.id", ondelete="SET NULL"), nullable=True)
    job_type        : Mapped[str]           = mapped_column(String(20), nullable=False, server_default="pull")

    created_at      : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at      : Mapped[datetime]      = mapped_column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
