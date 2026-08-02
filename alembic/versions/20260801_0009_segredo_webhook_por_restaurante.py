"""segredo de webhook por restaurante

Revision ID: 20260801_0009
Revises: 20260801_0008
Create Date: 2026-08-01

Ate aqui, a assinatura do webhook do Mercado Pago era conferida com
MERCADOPAGO_WEBHOOK_SECRET, uma variavel global do .env — funcionava para o
piloto (um restaurante, uma conta), mas cada restaurante tem sua PROPRIA
conta no Mercado Pago (nao e um app de marketplace com OAuth compartilhado)
e configura essa assinatura no proprio painel. Manter isso no .env
significaria uma variavel por restaurante e reiniciar o servidor a cada
cliente novo — o oposto do "adicionar restaurante = so dado no banco" que
motivou o BLOCO G (`restaurant_payment_credentials`,
20260801_0008_credenciais_pagamento_por_restaurante.py).

Esta migracao acrescenta `webhook_secret_encrypted` na MESMA linha do
access_token — mesma chave de escopo (restaurante, ambiente), mesma cifra
(Fernet, ver src/utils/crypto.py), mesma regra de nunca logar. Nullable
de proposito: a "Assinatura secreta" so existe depois de cadastrar a
Notification URL no painel do Mercado Pago, um passo que pode acontecer
DEPOIS do cadastro do access_token — uma credencial sem o segredo do
webhook ainda e uma credencial valida para criar cobranca, so o webhook
dela responde 503 ate o segredo ser cadastrado (mesmo comportamento que
uma credencial ausente ja tinha).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0009"
down_revision: Union[str, None] = "20260801_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "restaurant_payment_credentials",
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("restaurant_payment_credentials", "webhook_secret_encrypted")
