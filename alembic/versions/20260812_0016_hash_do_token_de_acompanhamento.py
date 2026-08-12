"""acrescenta orders.tracking_token_hash e popula a partir dos tokens em claro

Revision ID: 20260812_0016
Revises: 20260812_0015
Create Date: 2026-08-12

Primeira das DUAS revisoes que tiram o token de acompanhamento do texto puro.
Esta so ACRESCENTA; quem apaga a coluna em claro e a 0017, de proposito
separada — depois do apaga nao ha volta possivel, porque hash nao se
desfaz.

O QUE ESTAVA ERRADO. `orders.tracking_token` guardava, em texto puro, a
credencial que abre `GET /restaurants/{slug}/orders/track/{token}` — endereco
residencial, telefone, itens e historico do pedido. Todo outro segredo do
projeto e gravado em hash (codigo de verificacao, token de reset). Um dump de
banco ou um backup vazado entregava acesso a TODOS os pedidos.

POR QUE SHA-256 SEM CHAVE, e nao o `_hmac_hex` dos outros segredos. O que uma
chave compra e resistencia a forca bruta sobre a ENTRADA. O codigo de
verificacao tem 6 digitos — um milhao de possibilidades, quebravel num
piscar sem a chave, e por isso ele e HMAC. O token de acompanhamento e
`secrets.token_urlsafe(32)`: 256 bits. Nao existe dicionario, nao existe
rainbow table, e um atacante com o banco inteiro nas maos nao tem por onde
comecar. Em troca, sem chave:

- o backfill nao precisa de segredo nenhum dentro da migracao;
- nao existe uma variavel de ambiente cuja perda apaga o acesso de TODO
  cliente aos proprios pedidos de uma vez.

O CUIDADO QUE ESTA REVISAO EXISTE PARA TOMAR: **link ja enviado a cliente
continua funcionando.** O hash e calculado a partir do token que ja esta
gravado, entao o mesmo link que esta no WhatsApp de alguem casa com a linha
nova. Nenhum token e regerado aqui. Se esta revisao regerasse os tokens,
todo link em circulacao morreria em silencio — o cliente veria 404 e ninguem
teria como devolver o token perdido (nao ha rota de reemissao, armadilha 19).

BACKFILL EM PYTHON, e nao em SQL com `digest()`. `digest` vem do pgcrypto, que
existe no Supabase mas NAO existe no Postgres descartavel da suite `db` — a
revisao morreria no CI. `hashlib` roda em qualquer lugar e nao depende de em
qual schema a extensao foi instalada.

`tracking_token` PERDE O NOT NULL aqui. E o que permite ao codigo novo parar
de escrever a coluna em claro sem esperar pela 0017: durante a janela entre
as duas revisoes a coluna existe, aceita nulo, e ninguem a le.

ATENCAO AO DEPLOY: o `CREATE UNIQUE INDEX` do fim trava ESCRITA em `orders`
enquanto constroi, e o entrypoint roda o upgrade com a API fora do ar
(armadilha 5). Fora do horario de movimento.
"""

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0016"
down_revision: Union[str, None] = "20260812_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Um lote grande demais segura a transacao e a memoria a toa; pequeno demais
# vira uma viagem ao banco por punhado de pedido. Mil e o meio termo comum.
TAMANHO_DO_LOTE = 1000


def preencher_os_hashes_que_faltam(connection) -> int:
    """Calcula o hash dos tokens em claro que ainda nao o tem.

    Devolve quantas linhas escreveu. Roda em lotes ate nao sobrar nenhuma,
    e e idempotente: rodar duas vezes na segunda nao encontra nada.

    A 0017 chama esta mesma funcao antes de apagar a coluna em claro, para
    varrer o que tiver entrado entre as duas revisoes.
    """
    escritas = 0
    while True:
        linhas = connection.execute(
            sa.text(
                "SELECT id, tracking_token FROM orders "
                "WHERE tracking_token_hash IS NULL AND tracking_token IS NOT NULL "
                "LIMIT :limite"
            ),
            {"limite": TAMANHO_DO_LOTE},
        ).fetchall()
        if not linhas:
            return escritas

        for id_do_pedido, token in linhas:
            connection.execute(
                sa.text("UPDATE orders SET tracking_token_hash = :hash WHERE id = :id"),
                {
                    "hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "id": id_do_pedido,
                },
            )
        escritas += len(linhas)


def upgrade() -> None:
    op.add_column("orders", sa.Column("tracking_token_hash", sa.Text(), nullable=True))

    preencher_os_hashes_que_faltam(op.get_bind())

    # Depois do backfill, e nao antes: construir o indice sobre a coluna
    # vazia e depois preenche-la faria o Postgres manter o indice a cada um
    # dos UPDATEs acima.
    #
    # UNIQUE porque `tracking_token` era UNIQUE e a busca por hash precisa da
    # mesma garantia: dois pedidos com o mesmo hash tornariam a consulta
    # ambigua. Como sha-256 e injetiva na pratica, uma colisao aqui so
    # aconteceria com token repetido, que o `secrets` nao produz.
    #
    # if_not_exists pela convencao da 20260806_0010: `orders` e tabela do
    # baseline, criada fora do Alembic, e o nome pode ja estar ocupado.
    op.create_index(
        "ix_orders_tracking_token_hash",
        "orders",
        ["tracking_token_hash"],
        unique=True,
        if_not_exists=True,
    )

    # O codigo novo nao escreve mais a coluna em claro. Sem esta linha, todo
    # pedido criado depois do deploy morreria no NOT NULL.
    op.alter_column("orders", "tracking_token", nullable=True)


def downgrade() -> None:
    """Volta ao estado anterior sem perder nada.

    So e reversivel de verdade enquanto a 0017 nao rodou: a coluna em claro
    ainda esta la e ainda tem os tokens. Depois da 0017, o caminho de volta e
    restaurar backup.

    O NOT NULL so volta se nao houver linha com token nulo — o que acontece se
    o codigo novo ja tiver criado pedidos. Nesse caso a coluna fica nullable,
    e o `RAISE NOTICE` diz por que.
    """
    op.drop_index("ix_orders_tracking_token_hash", table_name="orders", if_exists=True)
    op.drop_column("orders", "tracking_token_hash")
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM orders WHERE tracking_token IS NULL) THEN
            RAISE NOTICE 'Ha pedido com tracking_token nulo (criado pelo codigo '
                         'novo). A coluna fica nullable; o NOT NULL nao volta.';
          ELSE
            ALTER TABLE orders ALTER COLUMN tracking_token SET NOT NULL;
          END IF;
        END
        $$;
        """
    )
