"""alinhamento ORM x schema, ETAPA 1: a promessa, ainda sem cobranca

Revision ID: PREPARADA_alinhamento_etapa_1
Revises: PREENCHER_COM_O_HEAD_DO_DIA
Create Date: (ainda nao criada)

**ESTA REVISAO NAO ESTA APLICADA E NAO ESTA NA CADEIA.** Ela mora em
`alembic/preparadas/`, que o Alembic nao le. Antes de move-la para
`alembic/versions/`, leia `docs/alinhamento-orm-schema.md` inteiro e rode a
conferencia da secao "Etapa 0". O `down_revision` acima e um marcador de
proposito: mover sem acertar o head do dia quebra a cadeia, e e melhor quebrar
alto.

## O que esta etapa faz, e o que ela nao faz

Para cada uma das 15 colunas em que o ORM declara `nullable=False` e o banco
aceita `NULL`, ela cria uma restricao:

    ALTER TABLE t ADD CONSTRAINT ck_t_col_nao_nula CHECK (col IS NOT NULL) NOT VALID

`NOT VALID` e a palavra inteira desta etapa. Com ela, o Postgres:

- **nao varre a tabela** — a operacao e uma escrita no catalogo, com
  `ACCESS EXCLUSIVE` por milissegundos em vez de pela duracao de um scan;
- **passa a cobrar a regra das linhas NOVAS**, imediatamente. Todo INSERT e
  todo UPDATE a partir daqui obedece;
- **nao diz nada sobre as linhas ANTIGAS.** Elas continuam como estao, e a
  restricao fica marcada como nao validada no `pg_constraint`.

Ou seja: a partir do commit desta etapa, **o buraco para de crescer**. Fechar o
que ja existe e a etapa 2.

## Por que nao `SET NOT NULL` direto

`ALTER COLUMN ... SET NOT NULL` toma `ACCESS EXCLUSIVE` **e varre a tabela
inteira** para provar que nao ha nulo. `ACCESS EXCLUSIVE` bloqueia `SELECT`
tambem (armadilha registrada no plano do `tracking_token`), e `orders` /
`customers` sao as tabelas que a operacao inteira le. Fazer isso em uma tacada
significa a plataforma parada pelo tempo de 16 varreduras.

O caminho `NOT VALID` -> `VALIDATE` -> `SET NOT NULL` troca a varredura
bloqueante por uma varredura que **nao bloqueia leitura nem escrita**
(`VALIDATE CONSTRAINT` toma apenas `SHARE UPDATE EXCLUSIVE`), e faz o
`SET NOT NULL` final ser instantaneo — desde o Postgres 12 ele aceita uma CHECK
valida como prova e pula o scan. Producao e Supabase PG 17.

## O que acontece se houver linha nula hoje

Nada. Esta etapa **nao repara** — e essa e a propriedade que a torna segura de
aplicar sozinha. Se houver nulo, ela e aceita do mesmo jeito, a restricao fica
`NOT VALID` para sempre, e quem falha e a etapa 2. Por isso a etapa 0 (contar)
vem antes: nao para esta etapa passar, mas para voce saber se a proxima vai.

## O downgrade

Derruba as 16 restricoes. Volta exatamente ao estado anterior — nenhum dado foi
tocado, nenhuma coluna mudou de tipo. E o unico downgrade das duas etapas que e
completo de verdade.
"""

from alembic import op


revision = "PREPARADA_alinhamento_etapa_1"
down_revision = "PREENCHER_COM_O_HEAD_DO_DIA"
branch_labels = None
depends_on = None


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
        # `op.create_check_constraint` nao expoe NOT VALID, e NOT VALID e a
        # razao de existir desta etapa. Por isso o DDL cru.
        op.execute(
            f'ALTER TABLE "{tabela}" '
            f'ADD CONSTRAINT "{nome_da_restricao(tabela, coluna)}" '
            f'CHECK ("{coluna}" IS NOT NULL) NOT VALID'
        )


def downgrade() -> None:
    for tabela, coluna in reversed(COLUNAS):
        op.execute(
            f'ALTER TABLE "{tabela}" DROP CONSTRAINT "{nome_da_restricao(tabela, coluna)}"'
        )
