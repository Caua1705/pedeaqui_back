"""o cursor de categorias ja faladas, na sessao de voz

Revision ID: 20260825_0042
Revises: 20260825_0041
Create Date: 2026-08-25

O QUE FORCOU A COLUNA. Em 25/08/2026, perguntado "voces tem mais o que?"
depois de ja ter ouvido duas categorias, o atendente de voz recebeu a MESMA
lista de novo e falou as MESMAS duas. A pergunta pedia o que ainda nao tinha
sido dito, e a ferramenta nao tinha como saber o que ja tinha.

## Por que isto nao se resolve sem estado

`listar_categorias` e stateless por desenho: nao tem parametro nenhum, e a
lista de categorias vendaveis de uma filial e a mesma para qualquer forma de
perguntar. Duas chamadas iguais devolvem a mesma coisa — que e o correto para
"o que voces tem?" e o defeito para "e o que mais?".

As tres saidas possiveis, e por que sobrou esta:

    parametro no modelo   devolver o "ja falei de X e Y" como argumento da
                          ferramenta poe de volta no modelo a decisao que o
                          resto desta frente passou a semana tirando dele. E
                          o modo de falha e o pior: ele erra o argumento e a
                          lista pula categoria que ninguem ouviu.
    memoria do processo   armadilha 20 — nada em memoria sobrevive a mais de
                          um worker. A segunda chamada da mesma sessao cai em
                          outro processo e o cursor volta a zero.
    coluna na sessao      a sessao ja existe, ja tem uma linha por conversa,
                          ja e do banco (entao vale para qualquer worker) e ja
                          morre quando a conversa morre.

## O que a coluna guarda, e o que ela NAO guarda

Ela guarda quantas categorias a FERRAMENTA ja entregou nesta sessao, e nunca
quantas o assistente falou. Sao coisas diferentes e o backend so conhece a
primeira: o audio vai do navegador direto para a OpenAI, e nada do que e dito
passa por aqui (armadilha 43). O cursor anda em `_TETO_FALADO` — o mesmo dois
da frase da busca — porque e esse o tamanho da frase que a ferramenta entrega
pronta; o que sobra continua indo junto, na linha de DADOS, para o modelo
saber o que ha aqui antes de dizer que nao tem.

`NOT NULL DEFAULT 0` porque zero e o comeco de toda sessao, inclusive das que
ja existem no banco: "nenhuma entregue ainda" e um estado legitimo e nao uma
ausencia de dado. Nao ha o regime de herança da armadilha 35 aqui — nao ha o
que herdar de lugar nenhum.

## O que NAO entra aqui

Cursor de BUSCA. `buscar_no_cardapio` continua sem estado nenhum: repetir a
mesma consulta tem que devolver o mesmo cardapio, e "o que mais tem de
picanha?" e uma consulta diferente, nao a pagina dois da anterior.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260825_0042"
down_revision = "20260825_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_voice_sessions",
        sa.Column(
            "categories_offset",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_voice_sessions", "categories_offset")
