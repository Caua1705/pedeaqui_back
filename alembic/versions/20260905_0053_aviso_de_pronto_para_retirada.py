"""aviso de pronto para retirada: o quarto tipo de aviso

Revision ID: 20260905_0053
Revises: 20260905_0052
Create Date: 2026-09-05

## O buraco que ela fecha

Pedido de RETIRADA nao passa por `out_for_delivery` e nao recebe "foi
entregue" — as duas frases sao de entrega, e a revisao `0051` deixou as duas
de fora daquele fluxo de proposito.

O resultado e que quem retira recebia **um aviso so, o do aceite**, e depois
nada. A pessoa fica esperando sem saber que a comida ja esta pronta — e a
informacao existe: e o lojista apertando "pronto" no painel.

E o pior caso da frente inteira. Nos outros, o cliente deixa de receber uma
confirmacao do que ele ja imagina; aqui ele deixa de receber **a unica coisa
que ele nao tem como saber**.

## `ready`, e nao `completed`

`ready` e o estado em que o lojista diz "acabei de fazer". `completed` na
retirada e "a pessoa ja veio buscar" — avisar ali seria contar o que ela
acabou de fazer.

## Por que ele NAO vale para entrega

Numa entrega, `ready` significa "pronto para SAIR", e quem vem buscar e o
motoboy. O cliente recebe o `out_for_delivery` no passo seguinte, que e a
informacao dele. Mandar "pronto para retirada" para quem pediu entrega e
mandar a pessoa buscar um pedido que vai ate ela.

A regra geral, e ela agora esta escrita em `_TIPOS_DE_PEDIDO_POR_AVISO`:
**cada aviso vale nos tipos de pedido em que a FRASE dele e verdadeira.**

## O CHECK e recriado, e o downgrade tem um preco

`ck_whatsapp_messages_kind` espelha `WHATSAPP_MESSAGE_KINDS`
(`src/core/constants.py`) e esta registrado em `scripts/espelhos_de_enum.py` —
armadilha 15. Acrescentar valor exige DROP + CREATE, nao ha `ALTER` de CHECK.

**O downgrade FALHA se ja houver linha com o valor novo**, e isso e o certo:
o CHECK antigo nao consegue descrever aquelas linhas. Quem precisar voltar
apaga antes as linhas de `order_ready_for_pickup` — e apagar registro de aviso
enviado e uma decisao, nao um detalhe da migracao.
"""

from alembic import op


revision = "20260905_0053"
down_revision = "20260905_0052"
branch_labels = None
depends_on = None


_KINDS_COM_RETIRADA = (
    "kind = ANY (ARRAY['order_accepted'::text, 'order_ready_for_pickup'::text, "
    "'order_out_for_delivery'::text, 'order_delivered'::text])"
)

_KINDS_SEM_RETIRADA = (
    "kind = ANY (ARRAY['order_accepted'::text, 'order_out_for_delivery'::text, "
    "'order_delivered'::text])"
)


def upgrade() -> None:
    op.drop_constraint("ck_whatsapp_messages_kind", "whatsapp_messages", type_="check")
    op.create_check_constraint(
        "ck_whatsapp_messages_kind", "whatsapp_messages", _KINDS_COM_RETIRADA
    )


def downgrade() -> None:
    # Falha se existir linha `order_ready_for_pickup`. Ver o cabecalho: e o
    # comportamento certo, e nao um descuido.
    op.drop_constraint("ck_whatsapp_messages_kind", "whatsapp_messages", type_="check")
    op.create_check_constraint(
        "ck_whatsapp_messages_kind", "whatsapp_messages", _KINDS_SEM_RETIRADA
    )
