"""a comanda passa a ser configuravel por filial

Revision ID: 20260821_0029
Revises: 20260820_0028
Create Date: 2026-08-21

Duas configuracoes de impressao que hoje nao existem em lugar nenhum, e que
por isso saem iguais em toda loja da plataforma:

1. **A mensagem do rodape da via do cliente.** A bobina ja sai e ja chega na
   mao de quem pediu; o fim dela e espaco em branco pago. "Peca direto pelo
   nosso site e ganhe 5% de volta" e o Instagram da loja moram ai.
2. **Quantas vias saem, por tipo de pedido.** Hoje sai uma via do cliente e
   uma de cada setor, sempre. Retirada nao precisa da via da sacola, e a
   loja que grampeia a comanda no pacote precisa de duas.

## Os dois regimes, e por que eles sao diferentes

A divisao e a mesma da revisao `20260818_0025`, e a pergunta que a decide e
"um padrao do restaurante responde alguma pergunta aqui?".

**A mensagem do rodape: da filial COM heranca, nullable, NULL = herda.**
E texto de MARCA. O dono de cinco lojas escreve "@juniordapicanha" uma vez, e
mudar a campanha continua sendo UMA edicao — que e exatamente o argumento de
`min_order_value` e da taxa de servico. A filial que precisar divergir (o
quiosque com WhatsApp proprio) escreve o proprio texto e ele passa a valer so
ali. Por isso a coluna nasce nos DOIS lados: `restaurant_settings` guarda o
padrao e `branches` guarda a sobrescrita.

**A string vazia NAO e nulo, e essa diferenca e o unico jeito de desligar.**
`NULL` na filial significa "herda"; `''` significa "esta loja nao imprime
rodape". Sem os dois estados, a filial que nao quer a mensagem da rede nao
teria como recusa-la — e o `or` que colapsasse os dois cairia na armadilha 35
com outro nome.

**As quatro contagens de via: SO da filial, NOT NULL, sem heranca.**
Elas descrevem o BALCAO: quantas impressoras existem, se a comanda vai
grampeada no pacote, se o motoboy leva uma via. Nada disso e negociado pela
marca, e o resto da configuracao de impressao (setor, `printer_name`, o
proprio agente) ja pende de filial sem heranca nenhuma — dar um regime
proprio so a estas quatro colunas colocaria dois regimes na MESMA tela do
painel, que e o jeito mais barato de fazer alguem preencher o campo errado.

## Por que o default e 1 em todas as quatro, inclusive na producao da retirada

Porque 1 e o que sai hoje. A tentacao e nascer com
`production_copies_pickup = 0` — "retirada nao precisa da via da sacola" e
justamente o caso que motivou a feature — mas a via de PRODUCAO da retirada e
a comanda da COZINHA, e ela precisa sair: o pedido de retirada tambem e
preparado. Quem nao precisa sair na retirada e a via do CLIENTE (a que vai
grampeada na sacola), e mesmo essa e decisao do lojista, nao nossa.

Uma revisao que mudasse o comportamento de impressao sozinha e o mesmo
defeito do UPDATE que faltava em `20260818_0025`, de cabeca para baixo: la o
restaurante fechado reabria no deploy; aqui a cozinha pararia de receber
comanda no deploy, e nada no log diria por que. **Migracao nao muda operacao;
migracao abre a porta para o lojista mudar.**

## O teto de 5 copias

`CHECK` no banco, e nao so no schema do Pydantic. O dedo que digita 50 no
lugar de 5 gasta a bobina inteira num pedido e para a impressao dos
seguintes — e o campo e editavel por gerente, numa tela de balcao.

## O downgrade nao perde nada que importe

Volta ao comportamento fixo de uma via de cada. Quem tiver configurado 2
copias perde a configuracao, mas nao perde comanda: o codigo antigo imprime
uma de cada, que e o que ele sempre fez.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260821_0029"
down_revision: Union[str, None] = "20260820_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Teto de copias por via. Espelhado em `MAX_PRINT_COPIES`
# (src/schemas/admin_printing_schema.py) — o Pydantic responde 422 com
# mensagem, o CHECK garante que ninguem escreve por fora.
MAX_COPIAS = 5

# As quatro contagens, com o default que preserva o que sai hoje: uma via do
# cliente e uma de cada setor, em entrega e em retirada.
COLUNAS_DE_COPIAS = (
    "print_customer_copies_delivery",
    "print_production_copies_delivery",
    "print_customer_copies_pickup",
    "print_production_copies_pickup",
)


def upgrade() -> None:
    # O padrao da marca e a sobrescrita da loja. Nullable nos dois: nulo no
    # restaurante e "nao ha mensagem", nulo na filial e "herda".
    op.add_column(
        "restaurant_settings",
        sa.Column("receipt_footer_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "branches",
        sa.Column("receipt_footer_message", sa.Text(), nullable=True),
    )

    for coluna in COLUNAS_DE_COPIAS:
        op.add_column(
            "branches",
            sa.Column(
                coluna,
                sa.SmallInteger(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        # Estrito, sem `if_not_exists`: a coluna nasce nesta revisao, entao
        # nao ha constraint homonima possivel em producao (armadilha 4).
        op.create_check_constraint(
            f"ck_branches_{coluna}",
            "branches",
            f"{coluna} BETWEEN 0 AND {MAX_COPIAS}",
        )

    # NAO ha UPDATE de copia de dados, ao contrario de 20260818_0025. Aqui o
    # default JA e o comportamento antigo — copiar alguma coisa seria inventar
    # uma configuracao que ninguem pediu.


def downgrade() -> None:
    for coluna in reversed(COLUNAS_DE_COPIAS):
        op.drop_constraint(f"ck_branches_{coluna}", "branches", type_="check")
        op.drop_column("branches", coluna)

    op.drop_column("branches", "receipt_footer_message")
    op.drop_column("restaurant_settings", "receipt_footer_message")
