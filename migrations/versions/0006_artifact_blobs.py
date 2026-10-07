"""External immutable blobs; preserve existing inline artifacts and identities."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("artifacts", "content", existing_type=sa.LargeBinary(), nullable=True)
    op.add_column("artifacts", sa.Column("blob_key", sa.String(64), nullable=True))
    op.create_check_constraint(
        "ck_artifacts_content_location",
        "artifacts",
        "(content IS NOT NULL AND blob_key IS NULL) OR (content IS NULL AND blob_key IS NOT NULL)",
    )


def downgrade():
    raise RuntimeError("External artifact identities and bytes must be retained; downgrade is disabled")
