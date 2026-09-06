"""o assistente de voz sai do banco: a tabela de sessoes e as duas colunas dela

Revision ID: 20260906_0060
Revises: 20260905_0056
Create Date: 2026-09-06

**ESCRITA E NAO APLICADA.** Este arquivo mora em `alembic/preparadas/`, que o
Alembic nao le (`script_location = alembic` faz ele enxergar so `versions/`).
Nada aqui entra em `alembic upgrade head` — nem no CI, nem na suite `db`, nem
no container. Ver `alembic/preparadas/LEIA-ME.md` e a armadilha 53.

**E ela e a unica das quatro preparadas que APAGA DADO DE PRODUCAO.** As outras
tres mexem em CHECK e em colunas mortas; esta derruba uma tabela com linhas
dentro. Leia a secao "O que se perde" antes de qualquer coisa.

O codigo do assistente de voz ja saiu, em 06/09/2026. O banco nao — de
proposito, e nesta ordem, porque **coluna e tabela apagadas nao voltam** e a
loja abre dia 15.

---

## Por que o codigo pode sair antes do schema

As tres coisas que esta revisao derruba continuam sendo aceitas pelo banco com
o codigo novo por cima, e nenhuma delas atrapalha uma escrita:

| O que fica no banco | Por que nao quebra |
|---|---|
| `restaurant_settings.voice_enabled` | `NOT NULL` **com `DEFAULT false`**: o INSERT do ORM, que nao a menciona mais, continua valendo |
| `ai_usage_events.voice_session_id` | nullable, e o ORM nao a mapeia — toda linha nova nasce com ela nula |
| a CHECK `(surface = 'voice') = (voice_session_id IS NOT NULL)` | com a coluna sempre nula, ela e verdadeira para toda linha de `surface = 'text'` |
| `ai_voice_sessions` | tabela sem escritor. Ninguem le, ninguem escreve |

A CHECK, alias, **passou a fazer uma coisa melhor do que fazia**: ela era a
trava da idempotencia do aviso de fim; hoje ela impede que uma linha
`surface = 'voice'` NASCA por engano, porque o ORM nao tem mais como preencher
a chave. Enquanto ela existir, custo historico falso e impossivel.

## O que esta revisao NAO faz, e cada "nao" e uma decisao

**Nao apaga as linhas de `ai_usage_events` com `surface = 'voice'`.** Elas sao
DINHEIRO que a plataforma pagou entre 15/08/2026 e 06/09/2026, e o relatorio de
`GET /internal/ai-usage` continua as somando (`voice_calls`, `voice_cost_usd`).
Apagar seria reescrever a conta de agosto.

**Nao mexe na CHECK `ck_ai_usage_events_surface`.** Ela continua aceitando
`('text', 'voice')`. Tirar `voice` de la e impossivel na pratica —
`ADD CONSTRAINT ... CHECK` valida a tabela inteira, e ela FALHARIA sobre as
linhas gravadas — e seria errado mesmo se fosse possivel: o valor existe no
dado. `AI_SURFACES`, o espelho declarado dela em `scripts/espelhos_de_enum.py`,
continua com os dois valores pelo mesmo motivo.

**Nao derruba `products.serves_people`.** Aquela coluna nasceu para a voz
(revisao `20260825_0039`) e sobreviveu a ela: hoje o lojista a preenche no
painel e o `ProductResponse` a publica no cardapio. E dado de lojista, ja
publicado; tirar seria mudanca de contrato (armadilha 16) para apagar o que
alguem digitou. Ver `src/models/product_model.py`.

## O que se perde ao aplicar, e nao volta

| O que some | O que sobra no lugar |
|---|---|
| `ai_voice_sessions` inteira: quem pediu, quando, quanto durou, os seis contadores de token por sessao | as linhas de `ai_usage_events`: restaurante, modelo, tokens somados e `cost_usd` — o DINHEIRO fica, o detalhe por sessao nao |
| `ai_usage_events.voice_session_id` | nada. O vinculo entre a linha de custo e a sessao deixa de existir |
| `restaurant_settings.voice_enabled` | nada. Qual restaurante teve a voz ligada deixa de ser consultavel |

**O downgrade recria a ESTRUTURA e nao devolve LINHA NENHUMA.** Ele existe para
o caso de a aplicacao falhar no meio, e nao para "voltar atras depois". Se um
dia a voz voltar, ela volta com tabela vazia.

**Antes de aplicar, meca.** O numero nao esta neste arquivo porque nao ha como
consulta-lo daqui:

```sql
SELECT count(*)                                        AS sessoes,
       count(*) FILTER (WHERE ended_at IS NOT NULL)     AS encerradas,
       count(*) FILTER (WHERE input_audio_tokens IS NOT NULL) AS com_token,
       min(issued_at), max(issued_at)
  FROM ai_voice_sessions;

SELECT count(*) AS linhas_de_custo_de_voz,
       sum(cost_usd) AS dolares
  FROM ai_usage_events WHERE surface = 'voice';

SELECT count(*) FILTER (WHERE voice_enabled) AS lojas_com_voz_ligada,
       count(*)                              AS lojas
  FROM restaurant_settings;
```

Se `sessoes` for grande e o detalhe por sessao importar para alguma conta que
ainda nao foi feita, **exporte antes** (`\\copy ai_voice_sessions TO ...`). A
segunda consulta e a que diz quanto do relatorio de custo sobrevive: **toda
ela**, porque `ai_usage_events` nao e tocada.

## Custo de aplicar

Nenhum bloqueio pratico, e por um motivo que vale conferir: **nenhuma das tres
operacoes varre tabela grande.**

- `DROP TABLE` e `DROP INDEX` sao instantaneos;
- `ALTER TABLE ... DROP COLUMN` no Postgres **nao reescreve a tabela** — ele
  marca a coluna como apagada no catalogo. Vale para as duas, inclusive a de
  `ai_usage_events`, que e a maior das tres;
- `DROP CONSTRAINT` e instantaneo.

Todas tomam `ACCESS EXCLUSIVE`, entao esperam a transacao em curso terminar —
mas nao seguram o lock varrendo nada. Nao ha aqui o caso das duas etapas da
armadilha 53. Rode fora do movimento assim mesmo, pela regra da armadilha 5: o
`docker-entrypoint.sh` roda `alembic upgrade head` antes do Uvicorn, com a API
fora do ar.

## A ORDEM entre esta revisao e a `20260905_0057`

As duas mexem em `ai_usage_events` e **nao colidem**, mas a ordem muda o texto
de uma delas:

- a `0057` acrescenta `indexing` a `ck_ai_usage_events_surface`. Esta aqui nao
  toca nessa CHECK;
- o cabecalho da `0057` diz "a outra CHECK nao muda" — e a outra CHECK e
  justamente a que ESTA derruba. **Aplicada esta primeiro, aquela frase fica
  descrevendo um objeto que nao existe mais**, sem consequencia nenhuma para o
  DDL dela. Quem tirar a `0057` de `preparadas/` depois desta corrige o
  paragrafo no mesmo commit.

O `Revises` daqui e **marcador** (`20260905_0056`, o head do dia em que ela foi
escrita), como o das outras tres. Quem tirar esta do diretorio acerta o valor
para o head da hora — ver o passo 2 do `LEIA-ME.md`.

## O codigo acoplado, e ele ja esta mesclado

Diferente das outras tres preparadas, **o codigo desta ja entrou** — foi o que
tirou o assistente de voz do projeto. O que falta acertar no dia da aplicacao e
o que ainda fala com estes objetos:

1. `tests/test_custo_de_ia_db.py::_linha_de_voz_ja_gravada` semeia a linha
   historica por SQL cru, com `INSERT INTO ai_voice_sessions`. Ele para de
   funcionar no minuto em que a tabela cair, e o que ele prova —
   "`calls` fecha com `text_calls` + `voice_calls`" — precisa ser provado de
   outro jeito, ou sair;
2. `tests/test_custo_de_ia_db.py::TestOsCheckDoBanco::test_linha_de_voz_sem_sessao_e_recusada`
   deixa de fazer sentido: sem a CHECK, uma linha `surface = 'voice'` passa a
   ser aceita de novo;
3. `docs/modelo-de-dados.md` fala da "42a tabela no BANCO que nao esta aqui" —
   passa a ser 41 dos dois lados;
4. `scripts/divergencias_orm_schema.py` volta a contar duas divergencias a
   menos (as duas colunas nao mapeadas). O `--limite` do `ci.yml` desce junto.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260906_0060"
down_revision: Union[str, None] = "20260905_0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Os seis contadores de consumo da sessao (revisao `20260815_0023`). Numa
#: lista so porque o downgrade tem que recria-los, e duas listas escritas a mao
#: seriam a chance de elas divergirem.
COLUNAS_DE_USO = (
    "input_audio_tokens",
    "input_text_tokens",
    "output_audio_tokens",
    "output_text_tokens",
    "cached_tokens",
    "duration_seconds",
)

NOME_DA_CHECK = "ck_ai_usage_events_voice_has_session"
NOME_DO_UNIQUE = "ux_ai_usage_events_voice_session"


def upgrade() -> None:
    # A ORDEM NAO E LIVRE: `ai_usage_events.voice_session_id` tem FK para
    # `ai_voice_sessions.id`, entao a coluna cai ANTES da tabela. Invertido, o
    # `DROP TABLE` falha com "other objects depend on it" — e falharia no
    # entrypoint, com a API fora do ar.
    op.drop_index(NOME_DO_UNIQUE, table_name="ai_usage_events")
    op.drop_constraint(NOME_DA_CHECK, "ai_usage_events", type_="check")
    op.drop_column("ai_usage_events", "voice_session_id")

    op.drop_column("restaurant_settings", "voice_enabled")

    op.drop_index("ix_ai_voice_sessions_abertas", table_name="ai_voice_sessions")
    op.drop_index("ix_ai_voice_sessions_customer_dia", table_name="ai_voice_sessions")
    op.drop_table("ai_voice_sessions")


def downgrade() -> None:
    """Recria a ESTRUTURA. Nao devolve uma linha sequer — ver o cabecalho.

    A tabela volta como a soma das tres revisoes que a construiram (`0021`, os
    seis contadores da `0023` e o cursor da `0042`), porque e assim que ela
    esta hoje: um downgrade que devolvesse so a forma da `0021` deixaria o
    banco num estado que nunca existiu.
    """
    op.create_table(
        "ai_voice_sessions",
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
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=True,
        ),
        sa.Column("openai_call_id", sa.Text(), nullable=True),
        sa.Column(
            "issued_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.Text(), nullable=True),
        sa.Column(
            "categories_offset",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        *[sa.Column(coluna, sa.Integer(), nullable=True) for coluna in COLUNAS_DE_USO],
    )
    op.create_index(
        "ix_ai_voice_sessions_customer_dia",
        "ai_voice_sessions",
        ["customer_id", "issued_at"],
    )
    op.create_index(
        "ix_ai_voice_sessions_abertas",
        "ai_voice_sessions",
        ["expires_at"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    op.add_column(
        "restaurant_settings",
        sa.Column(
            "voice_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )

    op.add_column(
        "ai_usage_events",
        sa.Column(
            "voice_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ai_voice_sessions.id"),
            nullable=True,
        ),
    )
    # A CHECK volta VALIDA sobre a tabela inteira, e volta porque a coluna
    # recriada nasce nula em toda linha: para as de `surface = 'text'` os dois
    # lados sao falsos, e para as de `surface = 'voice'` — que continuam la —
    # ela seria FALSA. Por isso o `NOT VALID`: recriar a CHECK estrita
    # falharia sobre as linhas historicas de voz, cuja chave o upgrade apagou.
    #
    # `NOT VALID` recusa linha NOVA e nao varre as antigas, que e exatamente o
    # que se quer aqui — e e a razao de o downgrade nao ser simetrico. Voltar
    # atras nunca devolve o vinculo que o upgrade destruiu.
    op.execute(
        f"ALTER TABLE ai_usage_events ADD CONSTRAINT {NOME_DA_CHECK} "
        "CHECK ((surface = 'voice') = (voice_session_id IS NOT NULL)) NOT VALID"
    )
    op.create_index(
        NOME_DO_UNIQUE,
        "ai_usage_events",
        ["voice_session_id"],
        unique=True,
        postgresql_where=sa.text("voice_session_id IS NOT NULL"),
    )
