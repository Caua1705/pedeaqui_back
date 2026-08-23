"""anotacoes do lojista para o assistente, separadas da vitrine

Revision ID: 20260823_0034
Revises: 20260822_0033
Create Date: 2026-08-23

`restaurants.description` tinha DOIS destinos, e eles pedem textos opostos:

- a VITRINE publica (`RestaurantPublicResponse`), onde o lojista escreve para
  o cliente decidir pedir — e escreve anuncio, com razao;
- o PROMPT do assistente (`ChatService._build_restaurant_context`, reusado
  pelo agente de voz em `src/api/voice.py`), onde o que serve e o contrario:
  o que a casa faz, o que ela nao faz, o que o atendente precisa saber para
  nao inventar.

Com um campo so, a tela do painel nao conseguia instruir nenhum dos dois sem
mentir sobre o outro. `assistant_notes` e o campo do segundo destino, e a
`description` volta a ser so vitrine.

## Por que em `restaurants` e nao em `restaurant_settings`

`restaurant_settings` e, pelo que ela mesma declara, o PADRAO que a filial
herda. Nao existe anotacao de assistente por filial — o assistente fala da
casa, e a casa e uma so. Alem disso o caminho do chat ja carrega o objeto
`Restaurant` (`_get_active_restaurant`); ler de `restaurant_settings` custaria
um SELECT a mais no caminho quente para nao ganhar nada.

Ela mora ao lado da coluna de que se separa, que e onde quem for entender a
diferenca entre as duas vai olhar.

## Nasce NULA em todo restaurante, e NAO ha backfill

Copiar `description` para ca resolveria o buraco do primeiro dia e criaria um
pior: o campo cujo proposito e "nao escreva anuncio" nasceria cheio de
anuncio, e ninguem depois distinguiria "o lojista escreveu isto" de "a
migracao copiou". Pelo mesmo motivo nao ha fallback em runtime — a leitura NAO
cai para `description` quando isto e nulo. Um fallback preservaria o problema
inteiro para quem nunca preencher, que e justamente quem tem o problema.

O custo e conhecido e aceito: entre este deploy e o primeiro preenchimento, o
prompt sai sem a linha `Sobre a casa`, exatamente como ja sai hoje para quem
tem `description` vazia. Com um restaurante em producao, isso e um UPDATE ou
um salvar na tela.

## O `downgrade` perde o texto, e nao ha o que fazer

Nao ha para onde devolve-lo: `description` e outro campo, com outro dono e
outro publico, e escrever a anotacao interna na vitrine publica seria vazar
para o cliente um texto escrito para o modelo.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260823_0034"
down_revision: Union[str, None] = "20260822_0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nulavel e sem default: `ADD COLUMN` nulavel no Postgres e operacao de
    # catalogo, sem reescrita e sem lock que atrapalhe. `restaurants` e uma
    # tabela minuscula, mas o habito e o mesmo.
    op.add_column(
        "restaurants",
        sa.Column("assistant_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("restaurants", "assistant_notes")
