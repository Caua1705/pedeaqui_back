"""cupom restrito a forma de pagamento e a horario do dia

Revision ID: 20260904_0046
Revises: 20260903_0045
Create Date: 2026-09-04

Duas das tres restricoes que `docs/cupons.md` §7 registrava como "nao
implementadas, com o preco na mao". A terceira (itens do cardapio) continua
de fora: e a mais cara com folga e mexe na base da comissao.

## 1. `allowed_payment_methods text[]` — "so no pix"

Nulo = qualquer forma. Lista = so nestas. O CHECK amarra as duas coisas que
o schema Pydantic tambem confere, para uma escrita por SQL nao furar:

- **lista vazia nao entra.** Vazia nao e "qualquer forma": e "nenhuma", e
  um cupom que nao vale em forma nenhuma e um cupom que ninguem usa. Quem
  quer qualquer forma grava NULL;
- **so formas que a plataforma conhece.** A lista e a MESMA de
  `PAYMENT_METHODS` e do CHECK de `branch_payment_methods.method_type`
  (armadilha 15): metodo novo entra nos tres lugares juntos, ou o cupom
  aceita uma forma que o pedido recusa.

Por que uma coluna de lista e nao uma tabela filha: sao ate sete valores
fixos, lidos inteiros toda vez, nunca consultados por dentro ("quais cupons
aceitam pix?" nao e pergunta de tela nenhuma). Uma tabela filha custaria uma
juncao em cada avaliacao para responder o que `<@` responde na linha.

## 2. `valid_hours_from` / `valid_hours_until time` — "das 15h as 18h"

Hora LOCAL da operacao (`PLATFORM_TIMEZONE`), sem fuso na coluna — e o mesmo
regime de `branch_business_hours.opens_at`, e por isso `time` e nao
`timestamptz`: "das 15h as 18h" nao e um instante, e uma hora do relogio da
parede do restaurante, todo dia.

Faixa que vira a noite (22h as 2h) e valida e pertence ao dia em que comeca,
a mesma regra do horario de funcionamento (armadilha 10). Inicio inclusivo,
fim exclusivo: "ate as 18h" acaba as 18:00:00.

O CHECK cobra as duas metades juntas (uma sem a outra nao descreve faixa
nenhuma) e recusa inicio igual ao fim (faixa de zero minutos: ninguem sabe
quando ela vale).

Nenhum UPDATE: nulo nas tres colunas e a verdade sobre todo cupom de hoje —
vale em qualquer forma, o dia inteiro.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260904_0046"
down_revision: Union[str, None] = "20260903_0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A mesma lista de `src/core/constants.PAYMENT_METHODS`, repetida como texto
# porque migracao nao importa codigo da aplicacao: a constante de hoje pode
# mudar, e a revisao tem que continuar descrevendo o que ela aplicou.
FORMAS = "'pix', 'credit_card', 'debit_card', 'cash', 'voucher', 'meal_voucher', 'other'"


def upgrade() -> None:
    op.add_column(
        "restaurant_coupons",
        sa.Column("allowed_payment_methods", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_restaurant_coupons_allowed_payment_methods",
        "restaurant_coupons",
        "allowed_payment_methods IS NULL OR ("
        "cardinality(allowed_payment_methods) > 0 "
        f"AND allowed_payment_methods <@ ARRAY[{FORMAS}]::text[])",
    )

    op.add_column("restaurant_coupons", sa.Column("valid_hours_from", sa.Time(), nullable=True))
    op.add_column("restaurant_coupons", sa.Column("valid_hours_until", sa.Time(), nullable=True))
    op.create_check_constraint(
        "ck_restaurant_coupons_valid_hours",
        "restaurant_coupons",
        "((valid_hours_from IS NULL) = (valid_hours_until IS NULL)) "
        "AND (valid_hours_from IS NULL OR valid_hours_from <> valid_hours_until)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_restaurant_coupons_valid_hours", "restaurant_coupons", type_="check")
    op.drop_column("restaurant_coupons", "valid_hours_until")
    op.drop_column("restaurant_coupons", "valid_hours_from")
    op.drop_constraint(
        "ck_restaurant_coupons_allowed_payment_methods", "restaurant_coupons", type_="check"
    )
    op.drop_column("restaurant_coupons", "allowed_payment_methods")
