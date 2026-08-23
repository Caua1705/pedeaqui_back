"""must_change_password: a marca que a senha temporaria deixa

Revision ID: 20260823_0037
Revises: 20260823_0036
Create Date: 2026-08-23

A frente de usuarios do painel cadastra gente com uma senha TEMPORARIA gerada
pelo servidor, mostrada uma vez na resposta do POST. Essa senha atravessa um
canal informal — WhatsApp, papel, voz do balcao —, e o que limita o prejuizo
disso e a troca obrigatoria no primeiro acesso. Esta coluna e o sinal dela.

## Por que NAO da para reusar `password_changed_at IS NULL`

Foi a primeira ideia, e ela derrubaria a plataforma inteira no dia do deploy.

`password_changed_at` nulo significa hoje "nunca trocou a senha desde a
revisao 0013" — e **todo lojista antigo esta assim**, inclusive os que
escolheram a propria senha no `create_admin_user.py`. Ler esse nulo como
"precisa trocar" poria cada um deles numa tela de troca de senha sem que nada
tivesse acontecido com a conta.

Os dois campos respondem perguntas diferentes e sao independentes de
proposito: `password_changed_at` diz QUANDO a senha mudou (e e o que revoga
token emitido antes disso); `must_change_password` diz se a senha atual foi
escolhida pela PESSOA ou entregue pelo servidor.

## Nasce FALSA em todo mundo

Nenhum usuario existente recebeu senha temporaria — as que existem hoje foram
digitadas por quem rodou o script. Marcar qualquer um deles como pendente
seria inventar um fato sobre a senha dele.

O `server_default` fica na coluna depois da migracao, e nao e enfeite: o
`scripts/create_admin_user.py` continua criando usuario sem mencionar este
campo, e sem o default no banco esse INSERT passaria a violar o NOT NULL.

## Custo de aplicacao

`ADD COLUMN ... NOT NULL DEFAULT <constante>` nao reescreve a tabela no
Postgres 11+: o default fica no catalogo e as linhas existentes o herdam na
leitura. `admin_users` e minuscula de qualquer jeito, mas o habito e o que
evita a surpresa numa tabela grande (armadilha 5).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260823_0037"
down_revision: Union[str, None] = "20260823_0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    # Volta perdendo a informacao de quem ainda nao trocou a temporaria: essas
    # pessoas continuam entrando com a senha que receberam, e o painel para de
    # exigir a troca. Nao ha para onde guardar esse fato — e o unico campo que
    # o representa e este.
    op.drop_column("admin_users", "must_change_password")
