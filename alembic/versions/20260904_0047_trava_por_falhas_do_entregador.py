"""trava por falhas de codigo do entregador: contador e bloqueio temporario

Revision ID: 20260904_0047
Revises: 20260904_0046
Create Date: 2026-09-04

## O que segurava os seis digitos ate aqui

Nada alem do rate limit por IP (`COURIER_RATE_LIMIT`, 30/min e 600/h). A
conta que o `docs/entregadores.md` fazia — "um milhao de combinacoes a 600
por hora sao mais de dois meses" — vale para UM IP, e quem tem o link nao
esta preso a um: proxy residencial, 4G, cafe. O limite por IP encarece a
forca bruta; ele nao a fecha, porque o balde nao e o entregador.

Esta revisao poe o contador no lugar certo: **no cadastro que esta sendo
atacado**, onde trocar de rede nao ajuda.

## As tres colunas, em `couriers`

`access_failed_attempts`, `access_failed_at` e `access_blocked_until`.

Aqui e nao em tabela propria porque nao ha historico a guardar: a pergunta
e "este cadastro pode tentar agora?", que e estado de UMA linha, e a
resposta e lida em TODA requisicao do app do motoboy (o par viaja sempre,
sem sessao). Uma tabela de tentativas seria uma juncao a mais em cada
requisicao para responder um numero que cabe numa coluna — e uma varredura
de retencao a mais, porque tentativa de acesso e rastro de gente.

**O contador so anda para quem JA passou pelo link.** A conferencia do link
vem antes na dependencia, e link desconhecido e 404 sem tocar em coluna
nenhuma. Ou seja: quem faz o contador subir e o motoboy digitando errado, ou
alguem que roubou o link — nunca um desconhecido varrendo a internet.

`access_failed_at` existe para a janela, e sem ela a trava seria a armadilha
que ela deveria evitar: dois erros na segunda e tres na sexta somariam
cinco, e o motoboy ficaria de fora no meio do turno por erros de digitacao
de semanas diferentes. Com ela, falha isolada envelhece e o contador
recomeca.

## NOT NULL com `server_default`, e o motivo de nao ser nullable

`access_failed_attempts` nasce `NOT NULL DEFAULT 0`. Poderia ser nullable
com o codigo tratando o nulo, e seria a armadilha 50 escrita de proposito:
`NULL >= 5` nao e falso, e nulo aqui nao tem significado nenhum de produto —
"nunca errou" e zero. `ADD COLUMN ... NOT NULL DEFAULT` nao reescreve a
tabela no PG 11+, e `couriers` e a menor tabela do banco.

Os dois TIMESTAMPs sao nullable, e ai o nulo SIGNIFICA: "nunca falhou" e
"nao esta travado".

## O que esta revisao NAO faz

Nao mexe em `access_link_hash` nem em `access_code_hash`: a trava e sobre
quantas vezes se pode errar, nao sobre o que se compara. E nao ha UPDATE de
dado — toda linha existente nasce com zero falhas e sem trava, que e a
verdade sobre todo entregador neste minuto.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260904_0047"
down_revision: Union[str, None] = "20260904_0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "couriers",
        sa.Column(
            "access_failed_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "couriers",
        sa.Column("access_failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "couriers",
        sa.Column("access_blocked_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    # Contador nao anda para tras: negativo so chegaria por escrita feita por
    # fora (armadilha 33), e ai a trava passaria a nunca fechar.
    op.create_check_constraint(
        "ck_couriers_access_failed_attempts",
        "couriers",
        "access_failed_attempts >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_couriers_access_failed_attempts", "couriers", type_="check")
    op.drop_column("couriers", "access_blocked_until")
    op.drop_column("couriers", "access_failed_at")
    op.drop_column("couriers", "access_failed_attempts")
