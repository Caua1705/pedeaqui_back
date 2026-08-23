"""cartao online: status in_review e valor estornado no pedido

Revision ID: 20260823_0035
Revises: 20260823_0034
Create Date: 2026-08-23

Duas colunas de schema que o cartao de credito online exige, e que o pix
nunca precisou. Estao na mesma revisao porque nascem da mesma frente e nao
fazem sentido separadas: sem `in_review` o antifraude nao tem onde ser
gravado, e sem `refunded_amount` o estorno parcial (que so o cartao torna
comum) continua invisivel.

## `in_review` no CHECK de `payment_status`

O pix nao passa por antifraude: a cobranca ou e paga ou expira. O cartao tem
um terceiro desfecho — `in_process` no vocabulario do Mercado Pago — em que a
analise pode levar **ate 48h uteis**.

Ele NAO libera o pedido, exatamente como `pending`. Existe separado porque as
duas esperas pedem conversas opostas com o cliente:

    pending     "o cliente ainda nao pagou"      -> ligar para o cliente
    in_review   "o gateway esta analisando"      -> nao ha o que fazer, esperar

Com um estado so, o lojista le "AGUARDANDO PAGAMENTO" nos dois casos e nao tem
como saber qual das duas ligacoes fazer.

O CHECK e ACRESCIDO, nunca reescrito para menos: toda linha existente ja
satisfaz o predicado novo, porque ele e um superconjunto do antigo. Por isso o
`NOT VALID` seguido de `VALIDATE`: a constraint passa a valer para escrita
nova imediatamente, e a varredura de conferencia do que ja esta gravado roda
depois, com lock mais fraco. Em `orders`, que e a segunda maior tabela, a
diferenca e entre um `ACCESS EXCLUSIVE` durante a varredura inteira e um
`SHARE UPDATE EXCLUSIVE`.

## `refunded_amount`

No Mercado Pago, **estorno parcial mantem o pagamento em `approved`**. Nao ha
status novo, nao ha notificacao diferente: o que muda e
`transaction_amount_refunded` na consulta do pagamento. Sem uma coluna aqui, a
devolucao de parte do dinheiro nao existe em lugar nenhum do nosso lado — o
webhook chega, traduz para `paid`, ve que o pedido ja esta `paid` e retorna
`already_applied` sem sequer logar.

**A comissao NAO muda com ela, e isso e decisao tomada, nao pendencia.** A
plataforma cobra sobre a venda que aconteceu; se o lojista devolveu parte por
um erro dele, o custo e dele. `billable_order_conditions` continua olhando so
`payment_status != 'refunded'`.

Entao por que gravar? Porque a decisao contraria (comissao proporcional ao que
sobrou) e plausivel, e ela e **irrecuperavel para tras**: o valor estornado so
existe do lado do Mercado Pago, e nao ha como reconstitui-lo depois para
pedidos antigos. A coluna custa uma migracao hoje e compra a possibilidade de
mudar de ideia; nao grava-la fecha a porta em silencio.

`NOT NULL DEFAULT 0` nao reescreve a tabela: desde o Postgres 11, `ADD COLUMN`
com default nao-volatil e operacao de catalogo. O default fica na coluna para
que pedido novo nasca com zero sem depender de o ORM lembrar.

## O que esta revisao NAO faz

Nao cria estado `partially_refunded`. Ele obrigaria a mexer no grafo de
transicoes, nos rotulos da comanda e a responder se pedido parcialmente
estornado ainda pode ser preparado — e a resposta e "sim", porque o estorno
parcial costuma acontecer DEPOIS da entrega. A coluna responde a mesma
pergunta sem tocar em nada disso.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260823_0035"
down_revision: Union[str, None] = "20260823_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAYMENT_STATUS_CHECK = "ck_orders_payment_status"

STATUSES_ANTES = "'on_delivery', 'pending', 'paid', 'failed', 'refunded'"
STATUSES_DEPOIS = "'on_delivery', 'pending', 'in_review', 'paid', 'failed', 'refunded'"


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column(
            "refunded_amount",
            sa.Numeric(12, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    _trocar_check(STATUSES_DEPOIS)


def downgrade() -> None:
    # O CHECK volta ANTES da coluna sair, e a ordem importa: com o predicado
    # antigo de volta, qualquer pedido gravado como `in_review` derruba a
    # validacao — que e o comportamento certo. Voltar a imagem antiga com
    # pedido em analise no banco e uma decisao que precisa doer, nao passar
    # em silencio deixando um status que o codigo antigo nao sabe ler.
    _trocar_check(STATUSES_ANTES)
    op.drop_column("orders", "refunded_amount")


def _trocar_check(statuses: str) -> None:
    """Substitui o CHECK de payment_status pelo predicado informado.

    `NOT VALID` + `VALIDATE` em vez de um `create_check_constraint` simples:
    o segundo faz a varredura da tabela inteira segurando `ACCESS EXCLUSIVE`,
    e `orders` e grande. Separados, a constraint vale para escrita nova
    imediatamente e a conferencia do passado roda com lock mais fraco.
    """
    op.drop_constraint(PAYMENT_STATUS_CHECK, "orders", type_="check")
    op.execute(
        f"ALTER TABLE orders ADD CONSTRAINT {PAYMENT_STATUS_CHECK} "
        f"CHECK (payment_status IN ({statuses})) NOT VALID"
    )
    op.execute(f"ALTER TABLE orders VALIDATE CONSTRAINT {PAYMENT_STATUS_CHECK}")
