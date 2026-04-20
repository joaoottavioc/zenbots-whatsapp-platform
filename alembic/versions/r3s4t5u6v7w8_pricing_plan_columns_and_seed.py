"""Pricing: extend plan + subscription, seed tier plans

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-04-20

Extends `plan` with pricing-tier fields (tier, monthly_order_cap,
fair_use_orders_cap, overage_per_order_brl, billing_cycle_months, is_active,
max_bots, allows_template_messages), adds `plan_id` FK and `is_founder` flag
to `subscription`, and seeds the four launch plans:

  - free         (R$ 0,  cap 15, overage R$ 1,39/pedido)
  - pro_monthly  (R$ 129,90/mês, fair-use 5.000/mês)
  - pro_annual   (R$ 1.068,00/ano = R$ 89/mês, fair-use 5.000/mês)
  - founder      (R$ 59,90/mês lifetime, first 30 signups only)

The seed uses ON CONFLICT (key) DO NOTHING so re-running the migration is
safe.

Backfill of existing bots with a Free Subscription row is intentionally NOT
performed here — missing subscription is treated as implicit Free tier by
the application. A separate migration can create rows later if an explicit
audit trail is needed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "r3s4t5u6v7w8"
down_revision: Union[str, Sequence[str], None] = "q2r3s4t5u6v7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ── plan: new pricing-tier columns ────────────────────────────────
    op.add_column(
        "plan",
        sa.Column(
            "tier",
            sa.String(),
            nullable=False,
            server_default="pro",
        ),
    )
    op.create_index("ix_plan_tier", "plan", ["tier"])

    op.add_column("plan", sa.Column("monthly_order_cap", sa.Integer(), nullable=True))
    op.add_column("plan", sa.Column("fair_use_orders_cap", sa.Integer(), nullable=True))
    op.add_column("plan", sa.Column("overage_per_order_brl", sa.Float(), nullable=True))

    op.add_column(
        "plan",
        sa.Column(
            "billing_cycle_months",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "plan",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "plan",
        sa.Column(
            "max_bots",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "plan",
        sa.Column(
            "allows_template_messages",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── subscription: plan_id FK + founder flag ───────────────────────
    op.add_column("subscription", sa.Column("plan_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "subscription_plan_id_fkey",
        "subscription",
        "plan",
        ["plan_id"],
        ["id"],
    )
    op.create_index("ix_subscription_plan_id", "subscription", ["plan_id"])

    op.add_column(
        "subscription",
        sa.Column(
            "is_founder",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── Seed the four launch plans (idempotent) ───────────────────────
    op.execute(
        """
        INSERT INTO plan (
            key, title, description, price, currency, frequency,
            allows_bot_usage, tier, monthly_order_cap, fair_use_orders_cap,
            overage_per_order_brl, billing_cycle_months, is_active, max_bots,
            allows_template_messages, created_at
        ) VALUES
        (
            'free', 'Grátis',
            'Atendimento por IA, 15 pedidos/mês. R$ 1,39 por pedido excedente.',
            0.0, 'BRL', 1, true,
            'free', 15, NULL, 1.39, 1, true, 1, false, NOW()
        ),
        (
            'pro_monthly', 'Pro Mensal',
            'Pedidos ilimitados, até 3 bots, suporte e-mail 24h, sem marca ZenBots.',
            129.90, 'BRL', 1, true,
            'pro', NULL, 5000, NULL, 1, true, 3, true, NOW()
        ),
        (
            'pro_annual', 'Pro Anual',
            'Pedidos ilimitados, até 3 bots, 31% de desconto vs mensal (R$ 89/mês).',
            1068.00, 'BRL', 12, true,
            'pro', NULL, 5000, NULL, 12, true, 3, true, NOW()
        ),
        (
            'founder', 'Founder Lifetime',
            'Preço de fundador para os primeiros 30 assinantes — R$ 59,90/mês para sempre.',
            59.90, 'BRL', 1, true,
            'founder', NULL, 5000, NULL, 1, true, 3, true, NOW()
        )
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM plan WHERE key IN ('free', 'pro_monthly', 'pro_annual', 'founder')"
    )

    op.drop_index("ix_subscription_plan_id", table_name="subscription")
    op.drop_constraint("subscription_plan_id_fkey", "subscription", type_="foreignkey")
    op.drop_column("subscription", "is_founder")
    op.drop_column("subscription", "plan_id")

    op.drop_column("plan", "allows_template_messages")
    op.drop_column("plan", "max_bots")
    op.drop_column("plan", "is_active")
    op.drop_column("plan", "billing_cycle_months")
    op.drop_column("plan", "overage_per_order_brl")
    op.drop_column("plan", "fair_use_orders_cap")
    op.drop_column("plan", "monthly_order_cap")
    op.drop_index("ix_plan_tier", table_name="plan")
    op.drop_column("plan", "tier")
