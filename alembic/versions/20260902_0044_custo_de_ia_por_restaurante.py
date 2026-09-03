"""o custo de cada chamada de IA, por restaurante

Revision ID: 20260902_0044
Revises: 20260828_0043
Create Date: 2026-09-02

## O que nao se sabia

Quanto o assistente custa por restaurante. A fatura da OpenAI chega com um
numero so para a plataforma inteira, e nao ha como saber de quem foi o gasto
— nem se a comissao daquele restaurante paga a conta dele.

O que existia antes desta revisao respondia meia pergunta cada:

- `ai_voice_sessions` guarda os tokens da VOZ, por sessao, e `scripts/
  voice_usage_report.py` os soma por restaurante. Tokens, nunca dinheiro: e
  a soma de quatro contadores com precos diferentes, e o relatorio nao sabe
  os precos;
- o `/chat` de TEXTO nao guardava nada. O `usage` sai numa linha de log
  (`[AI /chat usage]`) e morre no `docker logs`, que gira.

Esta tabela e uma linha por CHAMADA, com o custo ja calculado.

## Por que o custo e gravado, e nao calculado na leitura

Preco de modelo muda, e modelo trocado no `.env` muda sem deploy. Calcular na
leitura faria o relatorio de agosto mudar de valor em setembro, sem ninguem
ter mexido em nada — e ninguem confia num numero que se move sozinho.
Gravado, ele e a foto do que se pagou naquele dia.

`NUMERIC(12, 6)` e nao `NUMERIC(12, 2)`: um turno de `/chat` custa da ordem de
US$ 0,0005, e com duas casas todo turno seria zero.

**Nullable, e nulo significa "modelo sem preco na tabela".** Ver
`src/ai/custo.py`: modelo desconhecido grava `NULL`, nunca zero — zero e um
numero que soma, e faria um restaurante inteiro aparecer de graca no relatorio
que existe para dizer quanto ele custa. Os tokens ficam gravados do mesmo
jeito, entao da para reprocessar quando a tabela de precos for atualizada.

## Em DOLAR, e nao em real

O preco e publicado em dolar. Converter na gravacao exigiria uma cotacao —
que nao temos, que muda todo dia, e cuja escolha (do dia? do fechamento do
mes? do pagamento do cartao?) e decisao de contabilidade, nao de schema.
Gravar em dolar mantem o numero conferivel contra a fatura, que tambem vem em
dolar.

## `surface`, e por que os dois nao viram duas tabelas

`text` e `voice`. Duas tabelas separariam melhor os campos (voz tem audio,
texto nao) e obrigariam toda pergunta de custo a somar duas consultas e
esperar que ninguem esquecesse uma. A pergunta que se faz aqui e sempre
"quanto ESTE restaurante custou", com os dois dentro; a separacao que importa
e uma coluna, e ela esta aqui.

O detalhe por faixa continua onde ja estava: a voz guarda audio e texto,
entrada e saida, em `ai_voice_sessions`, e a linha daqui aponta para la.

## `voice_session_id` e o UNIQUE dele: idempotencia, nao decoracao

A voz nao reporta uso por turno — o backend nao participa da conversa (o
audio vai do navegador direto para a OpenAI). O numero chega uma vez so, no
`POST /voice/session/{id}/ended`, **e esse aviso pode chegar duas vezes**: ele
vai com `keepalive` e e reenviado durante o fechamento da aba.

Sem o UNIQUE, a segunda chegada dobraria o custo daquela sessao. Com ele, a
gravacao vira upsert e a segunda chegada corrige a primeira — que e o
comportamento certo, porque o segundo aviso pode trazer numero mais completo.

O indice e PARCIAL (`WHERE voice_session_id IS NOT NULL`): as linhas de texto
sao a maioria e todas teriam nulo ali dentro, ocupando disco para nada.

E o CHECK amarra os dois: linha de voz TEM sessao, linha de texto NAO TEM. Sem
ele, uma linha de voz sem sessao seria uma duplicata esperando acontecer, e
nada a impediria.

## O indice de leitura

`(restaurant_id, created_at)` e a consulta do relatorio, literalmente: "este
restaurante, neste periodo". Ele e estrito e nao `if_not_exists` porque a
tabela nasce nesta revisao — nao ha nome ocupado possivel, e a permissividade
so esconderia erro (armadilha 4).

## O que esta tabela NAO mede, de proposito

**Embedding.** Toda busca no cardapio paga um embedding, e ele NAO e gravado
aqui. O motivo e o tamanho: `text-embedding-3-small` custa US$ 0,02 por milhao
de tokens, e uma pergunta de cliente tem umas duas dezenas de tokens — um
milhao de buscas custa quarenta centavos de dolar, contra ~US$ 0,0005 por
turno de `/chat`. Instrumentar isso exigiria contar token com `tiktoken` no
caminho quente da busca, para medir um arredondamento. Se um dia o modelo de
embedding mudar de faixa de preco, a conta muda e este paragrafo tambem.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260902_0044"
down_revision: Union[str, None] = "20260828_0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id"),
            nullable=False,
        ),
        # Nulo na voz: a sessao e do restaurante, nao da filial (ver
        # `ai_voice_sessions`, que tambem nao tem filial). No texto vem
        # sempre preenchido, porque `/chat` exige `branch_id`.
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=True,
        ),
        sa.Column("surface", sa.Text(), nullable=False),
        # O nome exato do modelo, como ele estava no ambiente naquela chamada.
        # Sem isto, uma troca de `MODEL_NAME` tornaria o historico ilegivel.
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        # SUBCONJUNTO de `input_tokens`, nunca uma parcela a mais. Somar os
        # dois conta o cache duas vezes.
        sa.Column(
            "cached_input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column(
            "voice_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_voice_sessions.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_check_constraint(
        "ck_ai_usage_events_surface",
        "ai_usage_events",
        "surface IN ('text', 'voice')",
    )
    op.create_check_constraint(
        "ck_ai_usage_events_voice_has_session",
        "ai_usage_events",
        "(surface = 'voice') = (voice_session_id IS NOT NULL)",
    )

    op.create_index(
        "ix_ai_usage_events_restaurante_periodo",
        "ai_usage_events",
        ["restaurant_id", "created_at"],
    )
    op.create_index(
        "ux_ai_usage_events_voice_session",
        "ai_usage_events",
        ["voice_session_id"],
        unique=True,
        postgresql_where=sa.text("voice_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Os indices e os CHECK caem junto com a tabela; nao ha o que desfazer
    # antes. E o dado desta tabela e medicao — perde-se a serie historica de
    # custo, e nao ha dinheiro nem pedido dentro dela.
    op.drop_table("ai_usage_events")
