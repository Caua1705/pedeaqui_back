"""adota o indice de order_items.order_id sob o nome canonico

Revision ID: 20260810_0012
Revises: 20260809_0011
Create Date: 2026-08-10

O relatorio de produtos (`GET /admin/reports/products`) junta itens com
pedidos:

    FROM order_items JOIN orders ON orders.id = order_items.order_id
    WHERE orders.restaurant_id = ... AND orders.created_at >= ... < ...

O recorte do lado de `orders` ja esta coberto por
`ix_orders_restaurant_created_at` (20260730_0006). Os `GROUP BY` das cinco
rotas de Desempenho rodam sobre as linhas que aquele recorte reduziu, e
nenhuma consulta filtra pelas colunas de agrupamento — indice nelas nao
ajudaria. O que restava era o lado de `order_items`.

E ele JA EXISTE em producao, criado a mao, com o nome
`idx_order_items_order_id`. Entao esta revisao nao cria um indice novo: ela
adota o que existe sob o nome da convencao do projeto.

Por que nao bastou criar `ix_order_items_order_id` com IF NOT EXISTS: **o
IF NOT EXISTS casa por NOME, nao por definicao**. Dois indices com nomes
diferentes sobre a mesma coluna sao, para o Postgres, dois indices — os dois
seriam mantidos em toda escrita em `order_items`, dobrando o custo de INSERT
de item de pedido para nao servir a nenhuma consulta a mais. A protecao que
a 20260806_0010 estabeleceu resolve colisao de nome; ela nao enxerga
duplicata de definicao. Ver `scripts/audit_indexes.py`, que procura as duas
coisas.

DROP antes do CREATE, e nao o contrario, para nao manter as duas copias no
disco ao mesmo tempo. Os dois comandos rodam na mesma transacao: ou a
`order_items` termina com o indice canonico, ou termina como estava.

ATENCAO AO DEPLOY: `CREATE INDEX` (sem CONCURRENTLY) trava ESCRITA em
`order_items` enquanto roda, e `order_items` e a maior tabela do banco.
Como o entrypoint do container agora roda `alembic upgrade head` antes do
Uvicorn, essa espera acontece com a API fora do ar e nenhum pedido novo
consegue ser gravado. Numa tabela grande, aplicar esta revisao em horario de
pico e derrubar a operacao. Rode fora do horario de movimento — ou, se a
janela nao existir, faca a troca a mao com CREATE INDEX CONCURRENTLY (que
nao roda dentro de transacao, e por isso nao cabe aqui) e depois
`alembic stamp 20260810_0012`.

O indice tambem serve fora do relatorio: todo carregamento de detalhe de
pedido (painel, cliente logado, token de acompanhamento) passa por
`selectinload(Order.items)`, que emite `WHERE order_items.order_id IN (...)`.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260810_0012"
down_revision: Union[str, None] = "20260809_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Nomes escritos como literal, e nao como constante do modulo, de proposito:
# `scripts/audit_indexes.py` descobre o que o Alembic e dono lendo
# `op.create_index("...")` das revisoes com expressao regular. Um nome vindo
# de variavel escapa dessa leitura, e o indice desta revisao apareceria no
# relatorio como "sem dono" — justamente o que ela existe para consertar.
#
# `idx_order_items_order_id` e o nome que a criacao a mao usou.


def upgrade() -> None:
    # IF EXISTS porque nem todo banco tem o nome antigo: um ambiente montado
    # depois da Fase 1 nunca passou pelos .sql aplicados a mao.
    op.drop_index("idx_order_items_order_id", table_name="order_items", if_exists=True)
    # IF NOT EXISTS pela convencao da 20260806_0010: `order_items` e do
    # baseline e o nome canonico tambem pode ja estar ocupado.
    op.create_index(
        "ix_order_items_order_id",
        "order_items",
        ["order_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Devolve a `order_items` ao estado anterior, nome antigo inclusive.

    Recriar `idx_order_items_order_id` parece estranho — e um nome fora da
    convencao nascendo de uma migracao. Mas o downgrade existe para desfazer
    um deploy ruim, e o codigo antigo precisa do indice tanto quanto o novo:
    sair daqui sem indice nenhum em `order_id` trocaria um problema por um
    pior.
    """
    op.create_index(
        "idx_order_items_order_id",
        "order_items",
        ["order_id"],
        if_not_exists=True,
    )
    op.drop_index("ix_order_items_order_id", table_name="order_items", if_exists=True)
