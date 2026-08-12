"""revogacao de token de lojista

Revision ID: 20260812_0013
Revises: 20260810_0012
Create Date: 2026-08-12

`admin_users.password_changed_at`, o espelho do que `customers` ganhou na
revisao 0005.

O QUE FALTAVA. O token de lojista vale 12h e nao havia como mata-lo antes
disso. Um token roubado — do log de um proxy, do `localStorage` de um
navegador emprestado, do `config.ini` do balcao — seguia abrindo o painel
inteiro do restaurante ate expirar sozinho. A unica alavanca era trocar
`ADMIN_AUTH_SECRET`, que desloga TODO lojista e para TODO agente de
impressao instalado com token fixo: um botao de emergencia caro demais para
o caso comum de "desconfio que vazou a minha senha".

COMO ELA REVOGA. `AdminAuthService._load_admin_from_token` passa a comparar
o `iat` do token com esta coluna. Trocou a senha, todo token emitido antes
daquele instante deixa de valer — inclusive o do ladrao, que e o ponto.
Mesmo mecanismo, mesma resolucao de segundo e mesmo arredondamento
conservador de `customers`: token emitido no MESMO segundo da troca cai
junto, porque derrubar uma sessao legitima custa um login e o outro lado
deixa a conta invadida aberta.

NULA NAS LINHAS EXISTENTES, de proposito. Sem `password_changed_at` nada e
revogado, que e o comportamento de hoje — a coluna so passa a valer para
quem trocar a senha depois desta revisao. Preenche-la com `now()` aqui
deslogaria os dois lojistas no deploy e pararia o agente de impressao no
meio do expediente, sem ninguem ter pedido.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260812_0013"
down_revision: Union[str, None] = "20260810_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column("password_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("admin_users", "password_changed_at")
