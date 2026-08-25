"""para quantas pessoas o produto serve, como campo proprio

Revision ID: 20260825_0039
Revises: 20260823_0038
Create Date: 2026-08-25

O QUE FORCOU A COLUNA. Em 25/08/2026 o atendente de voz, perguntado "e essa
serve para quantas pessoas?", respondeu que a picanha "nao vem com a quantidade
servida especifica" e que "normalmente e servida por peso". A primeira metade do
conserto foi mandar a `description` no resultado da busca — antes ela existia so
no JSON que vai para a TELA, e o modelo estava sem resposta.

Isso resolveu o caso, e resolveu por acidente: funciona **enquanto o lojista
tiver escrito "Serve 2 pessoas" no texto livre da descricao**. Quem nao escreveu
continua sem resposta, e o assistente nao tem como saber a diferenca entre "esse
produto serve uma pessoa" e "ninguem preencheu".

## Por que NULO nao e zero, e por que isso e a coisa mais importante daqui

`NULL` quer dizer **o lojista nao disse**, e nunca "serve para ninguem". A
diferenca decide o comportamento do assistente: com valor ele responde, com nulo
ele diz que nao sabe. A alternativa — default 1 — faria o banco inventar um fato
sobre todo produto ja cadastrado, que e exatamente o defeito que esta coluna
existe para corrigir.

Por isso NAO ha backfill. Nenhum dos produtos existentes ganha valor nesta
migracao: eles ficam nulos ate alguem digitar, e ate la o assistente responde
pela descricao, como hoje.

## O CHECK, e o que ele nao alcanca

`serves_people > 0` barra o zero e o negativo, que sao lixo de digitacao. Ele
nao barra o exagero — "serve 400 pessoas" passa, e o teto de sanidade fica no
schema do painel (`MAX_SERVES_PEOPLE`), onde uma recusa vira mensagem de tela em
vez de erro de banco.

## O que continua no texto livre

Acompanhamento, tamanho, corte e modo de preparo continuam na `description`, e
continuam viajando no resultado da busca. Esta coluna cobre UMA pergunta, que e
a que foi feita em voz alta e errada. Estruturar o resto e outra rodada — e a
pendencia irma, o campo de restricao alimentar (vegetariano, sem lactose, sem
gluten), esta registrada no cabecalho de `src/ai/voice/voice_prompt.py` e
NAO entra aqui: adivinhar alergenico machuca alguem, e o campo tem que nascer
com regra de preenchimento propria.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_0039"
down_revision = "20260823_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("serves_people", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_products_serves_people_positivo",
        "products",
        "serves_people IS NULL OR serves_people > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_products_serves_people_positivo", "products", type_="check")
    op.drop_column("products", "serves_people")
