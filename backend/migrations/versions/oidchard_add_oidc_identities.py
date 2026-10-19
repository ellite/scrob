"""add oidc_identities table (OIDC hardening — bind (issuer, sub) to users)

Revision ID: oidchard
Revises: wetrakrlists
Create Date: 2026-10-10

Introduces a per-identity row so SSO logins resolve on the IdP's stable
``(issuer, sub)`` pair rather than by stuffing the identifier claim into
``User.email``. See models/oidc_identity.py and routers/oidc.py.

Additive only: no existing rows are rewritten. The first SSO login after the
upgrade lands in the compat path, matches ``User.email`` once, and writes the
identity row for future logins.
"""

from alembic import op
import sqlalchemy as sa


revision = "oidchard"
down_revision = "wetrakrlists"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email_at_link", sa.String(length=255), nullable=True),
        sa.Column("provider_name", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_oidc_identities_issuer_subject"),
    )
    op.create_index("ix_oidc_identities_user_id", "oidc_identities", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_oidc_identities_user_id", table_name="oidc_identities")
    op.drop_table("oidc_identities")
