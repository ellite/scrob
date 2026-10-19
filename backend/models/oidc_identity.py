"""OIDC identity binding — one row per (issuer, subject) that logs in via SSO.

The stable ``(issuer, sub)`` pair is the primary identity key, so a user's
``User.email`` can change at the IdP without orphaning their account, and the
same ``sub`` from two different IdPs is correctly treated as two people.

See routers/oidc.py for the resolution order on login:
  1. match on ``(issuer, subject)``
  2. link to a local ``User.email`` when the IdP marks the email verified
  3. auto-create (only when ``OIDC_AUTO_CREATE_USERS`` is true)
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class OidcIdentity(Base):
    __tablename__ = "oidc_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_oidc_identities_issuer_subject"),
    )

    id            : Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id       : Mapped[int]            = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Full issuer URL from the id_token ``iss`` claim (or the configured
    # issuer for the userinfo-only compat path). Stored verbatim so two IdPs
    # with overlapping ``sub`` values stay separate.
    issuer        : Mapped[str]            = mapped_column(String(500), nullable=False)
    # The IdP's stable per-user id. Never derived from email - the whole point
    # of this table is that email can change under us.
    subject       : Mapped[str]            = mapped_column(String(255), nullable=False)
    # The email claim at the time this identity was linked, kept for audit /
    # debugging. Not used for lookups.
    email_at_link : Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    # Human-readable provider label captured at link time (OIDC_PROVIDER_NAME).
    # Settings UI shows this so a user can tell which IdP their account is
    # bound to without the operator having to publish issuer URLs.
    provider_name : Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
    created_at    : Mapped[datetime]       = mapped_column(DateTime, server_default=func.now(), nullable=False)
    last_login_at : Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user : Mapped["User"] = relationship(back_populates="oidc_identities")
