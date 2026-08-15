"""a voz e habilitada por restaurante

Revision ID: 20260815_0022
Revises: 20260815_0021
Create Date: 2026-08-15

## Por que uma coluna, e por que nesta tabela

`VOICE_ENABLED` no ambiente e uma chave so para a plataforma inteira: liga-la
acenderia a voz para TODOS os restaurantes no mesmo instante, cada sessao
custando dinheiro. Colocar o primeiro restaurante no ar exige um interruptor
por restaurante.

`restaurant_settings` e onde a configuracao por restaurante ja mora — taxa de
servico, formas de pagamento, tempo de entrega, `is_open`. Nao ha mecanismo
novo aqui: e mais uma coluna na mesma tabela, lida do mesmo jeito.

## Default FALSE, e isso e o ponto

A coluna nasce falsa em todas as linhas. Depois desta revisao ninguem tem voz
ate alguem dizer explicitamente quem tem — que e exatamente o que se quer para
ligar so no Junior primeiro. Um default `true` teria acendido a voz para a
base inteira no `alembic upgrade head`.

Restaurante SEM linha em `restaurant_settings` tambem fica sem voz: a leitura
devolve nada e o servico trata nada como desligado.

## Esta coluna NAO vai para o painel do lojista

Mesmo raciocinio de `platform_commission_percent` (armadilha 17): quem paga a
conta da OpenAI e a plataforma, entao ligar a voz e decisao da plataforma. Um
campo de tela aqui seria o lojista escolhendo quanto gastamos.

Ligar e desligar se faz por SQL, deliberadamente:

    UPDATE restaurant_settings SET voice_enabled = true
     WHERE restaurant_id = '<uuid do restaurante>';

## A 0021 nao foi tocada

Ela ja esta aplicada em producao. Coluna nova e revisao nova.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260815_0022"
down_revision: Union[str, None] = "20260815_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "restaurant_settings",
        sa.Column(
            "voice_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("restaurant_settings", "voice_enabled")
