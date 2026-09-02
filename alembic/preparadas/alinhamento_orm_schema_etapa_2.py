"""alinhamento ORM x schema, ETAPA 2: a cobranca, e o NOT NULL de verdade

Revision ID: PREPARADA_alinhamento_etapa_2
Revises: PREPARADA_alinhamento_etapa_1
Create Date: (ainda nao criada)

**ESTA REVISAO NAO ESTA APLICADA E NAO ESTA NA CADEIA.** Ver
`alembic/preparadas/LEIA-ME.md` e `docs/alinhamento-orm-schema.md`.

## A ordem, e por que cada passo esta onde esta

Por coluna, tres comandos:

1. `VALIDATE CONSTRAINT ck_..._nao_nula` — varre a tabela **sem bloquear
   leitura nem escrita** (`SHARE UPDATE EXCLUSIVE`). Se houver uma linha nula,
   FALHA AQUI, e e o lugar certo de falhar: nada foi alterado ainda;
2. `ALTER COLUMN ... SET NOT NULL` — **instantaneo**, porque desde o Postgres
   12 ele aceita uma CHECK `(col IS NOT NULL)` ja validada como prova e pula o
   scan. Sem o passo 1, este comando varreria a tabela inteira segurando
   `ACCESS EXCLUSIVE`, que e o que esta separacao existe para evitar;
3. `DROP CONSTRAINT` — a CHECK cumpriu o papel. Mante-la seria pedir ao
   Postgres que verificasse duas vezes a mesma coisa em todo INSERT.

## POR QUE ESTA REVISAO NAO PODE IR JUNTO COM A ETAPA 1

`alembic/env.py` abre **uma transacao para o upgrade inteiro**
(`context.begin_transaction()` em volta de `run_migrations`, sem
`transaction_per_migration`). Num `upgrade` que aplicasse as duas etapas de uma
vez, o `VALIDATE` da etapa 2 rodaria na mesma transacao que ainda segura o
`ACCESS EXCLUSIVE` do `ADD CONSTRAINT` da etapa 1 — e o ganho de lock, que e a
razao de existir do desenho, evaporaria. A plataforma ficaria parada pelo tempo
das 16 varreduras, exatamente como no `SET NOT NULL` ingenuo.

As duas etapas sao **duas execucoes separadas**, com `ALEMBIC_TARGET` na
primeira. O roteiro esta em `docs/alinhamento-orm-schema.md`.

E ha um segundo motivo, mais barato de explicar: entre uma etapa e outra, a
etapa 1 ja esta cobrando a regra das linhas novas. Deixar assar significa que,
quando o `VALIDATE` rodar, o unico jeito de haver nulo e ele ja existir antes —
nao ha corrida com escrita concorrente.

## Se o VALIDATE falhar

A transacao inteira volta: nenhuma das 15 colunas fica alterada, e a restricao
`NOT VALID` da etapa 1 continua no lugar, cobrando as linhas novas. Nao ha
estado pela metade. O erro nomeia a restricao, e dai a coluna; a linha
ofensora sai com a consulta que `docs/alinhamento-orm-schema.md` traz.

## O downgrade, e o que ele NAO devolve

Ele desfaz o que esta revisao fez: `DROP NOT NULL` e recria a CHECK como
`NOT VALID`. O schema volta ao estado do fim da etapa 1.

O que nao volta e o que nunca saiu: **nenhum dado e tocado por esta revisao**.
Ela e a rara migracao cujo downgrade e honesto — ao contrario da `0017` do
`tracking_token`, que recria a coluna vazia.
"""

from alembic import op


revision = "PREPARADA_alinhamento_etapa_2"
down_revision = "PREPARADA_alinhamento_etapa_1"
branch_labels = None
depends_on = None


# A MESMA lista da etapa 1, repetida de proposito.
#
# Importar da outra revisao pareceria mais limpo e seria pior: revisao do
# Alembic e modulo carregado por caminho, nao pacote — o import so
# funcionaria enquanto os dois arquivos morassem no mesmo diretorio, e
# quebraria exatamente no momento de move-los para `versions/`. Alem disso,
# revisao aplicada tem que continuar descrevendo o que ela fez mesmo depois
# de a outra ser editada.
#
# Quem cobra que as duas nao divirjam e `tests/test_revisoes_preparadas.py`.
# As 15 colunas da primeira classe de `scripts/divergencias_orm_schema.py`:
# o ORM diz NOT NULL e o banco aceita NULL.
#
# A lista esta escrita AQUI, e nao lida do `Base.metadata` em tempo de
# migracao, e isso e deliberado: revisao que consulta o ORM alinha o que o ORM
# disser NO DIA EM QUE RODAR, e o mesmo `alembic upgrade` faria coisas
# diferentes em bancos diferentes. Migracao descreve UMA mudanca, sempre a
# mesma. Se a lista mudar, muda-se a revisao.
#
# `restaurant_coupons.valid_until` ESTAVA aqui e SAIU em 03/09/2026. Ela era a
# excecao da lista: nas outras o banco estava frouxo e o model certo; naquela o
# banco ja permitia a campanha sem prazo e o model e que mentia. Alinha-la
# apagaria uma possibilidade de produto. Quem cobrou a saida foi
# `tests/test_revisoes_preparadas.py`, comparando a lista com a divergencia
# real do schema — foi para isso que ele existe.
COLUNAS = [
    ("admin_users", "is_active"),
    ("ai_feedback", "assistant_message"),
    ("ai_feedback", "created_at"),
    ("ai_feedback", "selected_product_ids"),
    ("ai_feedback", "user_message"),
    ("ai_product_embeddings", "embedding"),
    ("ai_product_embeddings", "product_id"),
    ("coupon_redemptions", "idempotency_key"),
    ("customer_addresses", "neighborhood"),
    ("customer_addresses", "number"),
    ("customers", "birth_date"),
    ("customers", "email"),
    ("customers", "password_hash"),
    ("order_item_options", "option_group_id"),
    ("order_item_options", "option_id"),
]


def nome_da_restricao(tabela: str, coluna: str) -> str:
    return f"ck_{tabela}_{coluna}_nao_nula"


def upgrade() -> None:
    for tabela, coluna in COLUNAS:
        restricao = nome_da_restricao(tabela, coluna)
        op.execute(f'ALTER TABLE "{tabela}" VALIDATE CONSTRAINT "{restricao}"')
        op.execute(f'ALTER TABLE "{tabela}" ALTER COLUMN "{coluna}" SET NOT NULL')
        op.execute(f'ALTER TABLE "{tabela}" DROP CONSTRAINT "{restricao}"')


def downgrade() -> None:
    for tabela, coluna in reversed(COLUNAS):
        restricao = nome_da_restricao(tabela, coluna)
        op.execute(
            f'ALTER TABLE "{tabela}" '
            f'ADD CONSTRAINT "{restricao}" CHECK ("{coluna}" IS NOT NULL) NOT VALID'
        )
        op.execute(f'ALTER TABLE "{tabela}" ALTER COLUMN "{coluna}" DROP NOT NULL')
