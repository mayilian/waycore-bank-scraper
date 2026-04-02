"""Deduplicate bank_connections that differ only by trailing slash.

Merges accounts, transactions, balances, sync_jobs, and account_sync_results
from duplicate connections into the canonical (earliest-created) connection,
then deletes the duplicates.

Revision ID: 004
Revises: 003
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()

    # Find groups of connections with the same (user_id, bank_slug, login_url_normalized)
    # Keep the earliest-created one as canonical
    dupes = conn.execute(
        sa.text("""
            SELECT login_url_normalized, user_id, bank_slug,
                   array_agg(id ORDER BY created_at) as ids
            FROM bank_connections
            WHERE login_url_normalized IS NOT NULL
            GROUP BY login_url_normalized, user_id, bank_slug
            HAVING count(*) > 1
        """)
    ).fetchall()

    for row in dupes:
        ids = row.ids
        canonical_id = ids[0]  # earliest created
        duplicate_ids = ids[1:]

        for dup_id in duplicate_ids:
            # For each duplicate connection's accounts, check if the canonical
            # connection already has an account with the same external_id
            dup_accounts = conn.execute(
                sa.text("SELECT id, external_id FROM accounts WHERE connection_id = :dup"),
                {"dup": dup_id},
            ).fetchall()

            for dup_acc in dup_accounts:
                # Check if canonical connection has this account
                canonical_acc = conn.execute(
                    sa.text("""
                        SELECT id FROM accounts
                        WHERE connection_id = :conn AND external_id = :ext
                    """),
                    {"conn": canonical_id, "ext": dup_acc.external_id},
                ).fetchone()

                if canonical_acc:
                    # Move transactions (ON CONFLICT skip dupes)
                    conn.execute(
                        sa.text("""
                            UPDATE transactions SET account_id = :target
                            WHERE account_id = :source
                            AND NOT EXISTS (
                                SELECT 1 FROM transactions t2
                                WHERE t2.account_id = :target AND t2.external_id = transactions.external_id
                            )
                        """),
                        {"target": canonical_acc.id, "source": dup_acc.id},
                    )
                    # Delete remaining duplicate transactions
                    conn.execute(
                        sa.text("DELETE FROM transactions WHERE account_id = :source"),
                        {"source": dup_acc.id},
                    )
                    # Move balances (all of them — append-only, no conflicts)
                    conn.execute(
                        sa.text(
                            "UPDATE balances SET account_id = :target WHERE account_id = :source"
                        ),
                        {"target": canonical_acc.id, "source": dup_acc.id},
                    )
                    # Move account_sync_results
                    conn.execute(
                        sa.text("""
                            UPDATE account_sync_results SET account_id = :target
                            WHERE account_id = :source
                        """),
                        {"target": canonical_acc.id, "source": dup_acc.id},
                    )
                    # Delete the duplicate account
                    conn.execute(
                        sa.text("DELETE FROM accounts WHERE id = :id"),
                        {"id": dup_acc.id},
                    )
                else:
                    # No canonical equivalent — just re-parent the account
                    conn.execute(
                        sa.text("UPDATE accounts SET connection_id = :conn WHERE id = :id"),
                        {"conn": canonical_id, "id": dup_acc.id},
                    )

            # Move sync_jobs from duplicate to canonical connection
            conn.execute(
                sa.text(
                    "UPDATE sync_jobs SET connection_id = :target WHERE connection_id = :source"
                ),
                {"target": canonical_id, "source": dup_id},
            )

            # Delete the duplicate connection
            conn.execute(
                sa.text("DELETE FROM bank_connections WHERE id = :id"),
                {"id": dup_id},
            )


def downgrade() -> None:
    # Data migration — cannot be reversed
    pass
