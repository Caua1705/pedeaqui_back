"""visibilidade em tres valores, cupom sem codigo, e o resgate sem sacola

Revision ID: 20260828_0043
Revises: 20260825_0042
Create Date: 2026-08-28

Tres mudancas que so fazem sentido juntas, porque as tres respondem a mesma
pergunta — **quem enxerga este cupom, e como ele chega na sacola** — e uma
sozinha deixa o codigo com dois jeitos de responder.

## 1. `is_public` sai; entra `visibility`

O booleano tinha dois valores para uma pergunta de tres:

    is_public = true    aparece para todo mundo
    is_public = false   nao aparece para ninguem, so quem digita o codigo

Nao havia como dizer **"aparece so para quem se encaixa"** — que e a campanha
de reativacao ("some ha dois meses, volte com R$ 15") e a de primeira compra.
Sem o terceiro valor, o lojista tinha duas saidas e as duas erradas: publicar
para todo mundo (e dar desconto de reativacao a quem pede toda semana) ou
deixar privado (e depender de o cliente sumido receber o codigo por um canal
que nao existe).

    visibility = 'public'    | aparece para todos
                 'segment'   | aparece so para o `target_segment`
                 'private'   | nao aparece; existe para quem digita o codigo

**A coluna antiga SAI na mesma revisao, e nao fica convivendo.** Duas colunas
respondendo "quem ve este cupom" e a divergencia que este repositorio ja
pagou caro em outros lugares: a leitura escolhe uma, a escrita atualiza a
outra, e o sintoma e um cupom visivel para quem nao devia sem nada no log.

O `downgrade` recria `is_public` (`private` -> false, o resto -> true) e
**perde a distincao entre `public` e `segment`**: os dois voltam como `true`.
Nao ha como nao perder — a informacao nao cabe num booleano, e foi por isso
que a coluna mudou. Quem voltar esta revisao com campanha de segmento no ar
publica essa campanha para a base inteira; e o mesmo modo de falha da
armadilha 36 (rollback de imagem sem downgrade), e aqui esta escrito.

**Custo de contrato, sabido:** o painel le `is_public` do `/openapi.json`
(armadilha 16). O campo some do `CouponCreate`, do `CouponUpdate` e do
`CouponAdminResponse` no mesmo deploy, e a tela de cupom precisa trocar para
`visibility` — nao ha alias, de proposito. Um alias que aceitasse os dois
seria a segunda fonte de verdade voltando pela porta do schema.

## 2. `target_segment`, e por que sao os rotulos do RFV

Os cinco valores sao os de `CustomerSegment` (`novo`, `ocasional`, `fiel`,
`em_risco`, `perdido`), os mesmos que o painel ja pinta na lista de clientes
e que `src/services/customer_segment.py` produz em SQL.

Nao ha vocabulario proprio do cupom. Um `never_ordered`/`lapsed` so daqui
seria a **segunda definicao de "sumiu"** no sistema, e o cabecalho daquele
modulo registra o que custou a primeira duplicata: uma janela de ate 24h por
cliente em que as duas implementacoes discordavam, invisivel em leitura de
codigo. O alvo do cupom passa a ser a mesma escada, medida com as mesmas
expressoes.

O CHECK amarra os dois campos nos DOIS sentidos:

    (visibility = 'segment') = (target_segment IS NOT NULL)

Ou seja: cupom de segmento sem alvo nao entra, e alvo preenchido num cupom
publico ou privado tambem nao. O segundo lado importa mais do que parece —
sem ele, um cupom `public` guardaria um `target_segment` que ninguem le, e o
lojista veria na tela um alvo que nao filtra nada.

## 3. `code` passa a ser nullable — cupom que aplica sozinho

Cupom SEM codigo e o desconto que a casa da sem pedir nada em troca: ele
entra na sacola no checkout quando ela permite. Cupom COM codigo continua
exigindo que a pessoa digite.

**Os tres indices unicos continuam intactos, e isso nao e sorte.** O UNIQUE
do Postgres trata NULL como distinto de qualquer outro NULL, entao N cupons
sem codigo no mesmo restaurante convivem sem colidir em
`restaurant_coupons_restaurant_code_unique` nem em
`uq_restaurant_coupons_restaurant_code_ci` (`lower(NULL)` e NULL).

E o CHECK `restaurant_coupons_code_not_blank` (`length(trim(code)) > 0`)
tambem fica como esta: com `code` nulo a expressao e NULL, e CHECK so recusa
o que e FALSE. Ele continua barrando `'   '`, que e o que ele existe para
barrar.

## 4. `coupon_claims` — RESGATE nao e USO

`coupon_redemptions` tem `order_id NOT NULL`: ela so consegue registrar
cupom que ja virou pedido. Guardar resgate ali exigiria relaxar esse NOT
NULL, e ai o contador de "quantos ja usaram" — que e o que barra o proximo
cliente — passaria a contar gente que so digitou um codigo.

    coupon_redemptions   USO      | tem pedido, tem desconto, conta no teto
    coupon_claims        RESGATE  | nao tem pedido, nao tem valor,
                                  | so concede VISIBILIDADE

O resgate nao antecipa regra nenhuma: janela, minimo, teto total, teto por
cliente, cooldown e primeira-compra continuam sendo conferidos no checkout,
sobre a sacola daquele momento. Resgatar um cupom vencido e possivel e
inutil — ele nunca aparece na lista.

`UNIQUE (coupon_id, customer_id)` porque resgatar duas vezes e a mesma coisa
que resgatar uma: a rota e idempotente e nao ha contador aqui.

**A tabela nasce vazia, entao os indices sao ESTRITOS** (armadilha 4): nao
ha colisao de nome possivel, e `if_not_exists` so esconderia erro. Os
indices sobre `restaurant_coupons`, que e tabela do baseline, continuam com
a permissividade que a armadilha manda.

**LGPD:** `customer_id` e NOT NULL e sem `ON DELETE`, igual a
`coupon_redemptions` — e pelo mesmo motivo. A anonimizacao da conta nao faz
DELETE em `customers` (docs/lgpd-fase2-exclusao-de-conta.md), entao a linha
sobrevive apontando para um cliente que ja nao e pessoa nenhuma. Ela nao
guarda dado pessoal: e um par de ids e uma data.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260828_0043"
down_revision = "20260825_0042"
branch_labels = None
depends_on = None


# Os mesmos cinco de `CustomerSegment`. Repetidos aqui como texto porque
# migracao nao importa codigo da aplicacao: o enum de hoje pode mudar, e a
# revisao tem que continuar descrevendo o que ela de fato aplicou.
SEGMENTOS = "'novo', 'ocasional', 'fiel', 'em_risco', 'perdido'"


def upgrade() -> None:
    op.add_column(
        "restaurant_coupons",
        sa.Column("visibility", sa.Text(), nullable=False, server_default="public"),
    )
    op.add_column("restaurant_coupons", sa.Column("target_segment", sa.Text(), nullable=True))

    # O backfill roda DEPOIS do add_column e ANTES dos CHECK: as linhas que
    # ja existem nasceram todas com o default 'public', e as privadas
    # precisam virar antes de qualquer constraint olhar para elas.
    op.execute("UPDATE restaurant_coupons SET visibility = 'private' WHERE is_public IS FALSE")

    op.create_check_constraint(
        "ck_restaurant_coupons_visibility",
        "restaurant_coupons",
        "visibility IN ('public', 'segment', 'private')",
    )
    op.create_check_constraint(
        "ck_restaurant_coupons_target_segment",
        "restaurant_coupons",
        f"target_segment IS NULL OR target_segment IN ({SEGMENTOS})",
    )
    op.create_check_constraint(
        "ck_restaurant_coupons_segment_needs_target",
        "restaurant_coupons",
        "(visibility = 'segment') = (target_segment IS NOT NULL)",
    )

    # O indice da vitrine acompanha a coluna que ele filtra. Derrubado
    # explicitamente antes do DROP COLUMN — o Postgres o levaria junto de
    # qualquer jeito, e deixar isso implicito esconderia do proximo leitor
    # que a vitrine perdeu e reganhou o indice nesta revisao.
    op.drop_index(
        "ix_restaurant_coupons_public_window",
        table_name="restaurant_coupons",
        if_exists=True,
    )
    op.drop_column("restaurant_coupons", "is_public")
    op.create_index(
        "ix_restaurant_coupons_visibility_window",
        "restaurant_coupons",
        ["restaurant_id", "visibility", "is_active", "valid_from", "valid_until"],
        if_not_exists=True,
    )

    op.alter_column("restaurant_coupons", "code", existing_type=sa.Text(), nullable=True)

    op.create_table(
        "coupon_claims",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "coupon_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurant_coupons.id"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "claimed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("coupon_id", "customer_id", name="uq_coupon_claims_coupon_customer"),
    )
    # A lista do cliente comeca por "quais cupons eu resguei", e o UNIQUE
    # acima e (coupon_id, customer_id) — prefixo errado para essa pergunta.
    op.create_index("ix_coupon_claims_customer_id", "coupon_claims", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_coupon_claims_customer_id", table_name="coupon_claims")
    op.drop_table("coupon_claims")

    # Cupom sem codigo nao cabe na coluna NOT NULL que volta. O UPDATE nao e
    # cosmetico: sem ele o `alter_column` falha e o downgrade morre no meio,
    # com metade das mudancas revertidas.
    #
    # O codigo gerado e feio de proposito. Ele nao pode colidir com codigo
    # de campanha existente, e tem que ser obviamente automatico para quem
    # o encontrar na tela do painel depois de um rollback.
    op.execute(
        "UPDATE restaurant_coupons "
        "SET code = 'AUTO-' || upper(substring(id::text, 1, 8)) "
        "WHERE code IS NULL"
    )
    op.alter_column("restaurant_coupons", "code", existing_type=sa.Text(), nullable=False)

    op.add_column(
        "restaurant_coupons",
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    # `segment` volta como PUBLICO, e essa e a perda que o docstring anuncia:
    # o booleano nao tem onde guardar "so para quem se encaixa". Quem voltar
    # esta revisao com campanha de segmento no ar esta publicando aquela
    # campanha para a base inteira.
    op.execute("UPDATE restaurant_coupons SET is_public = false WHERE visibility = 'private'")

    op.drop_index(
        "ix_restaurant_coupons_visibility_window",
        table_name="restaurant_coupons",
        if_exists=True,
    )
    op.drop_constraint("ck_restaurant_coupons_segment_needs_target", "restaurant_coupons")
    op.drop_constraint("ck_restaurant_coupons_target_segment", "restaurant_coupons")
    op.drop_constraint("ck_restaurant_coupons_visibility", "restaurant_coupons")
    op.drop_column("restaurant_coupons", "target_segment")
    op.drop_column("restaurant_coupons", "visibility")
    op.create_index(
        "ix_restaurant_coupons_public_window",
        "restaurant_coupons",
        ["restaurant_id", "is_public", "is_active", "valid_from", "valid_until"],
        if_not_exists=True,
    )
