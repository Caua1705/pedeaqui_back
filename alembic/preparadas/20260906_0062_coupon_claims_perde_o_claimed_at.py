"""coupon_claims perde `claimed_at`, que grava o mesmo instante de `created_at`

Revision ID: 20260906_0062
Revises: 20260905_0058
Create Date: 2026-09-06

**ESCRITA E NAO APLICADA.** Este arquivo mora em `alembic/preparadas/`, que o
Alembic nao le (`script_location = alembic` faz ele enxergar so `versions/`).
Nada aqui entra em `alembic upgrade head` — nem no CI, nem na suite `db`, nem
no container. Ver `alembic/preparadas/LEIA-ME.md` e a armadilha 53.

Auditoria de 05/09/2026, §2.2.

---

## As duas colunas gravam a mesma coisa, no mesmo instante

A tabela nasceu na revisao `20260828_0043` com as duas:

    claimed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()

Nenhuma das duas e escrita pelo codigo — as duas caem no `DEFAULT now()` do
mesmo INSERT. **Nao ha caminho em que elas divirjam**, e nao ha caminho que
faca `claimed_at` significar outra coisa: a linha existe porque o cliente
resgatou, e o instante do resgate e o instante da linha.

## `claimed_at` e a que sai, e `created_at` e a que fica

Varredura em `src/`, `scripts/`, `tools/` e `tests/`: `claimed_at` aparece
**uma vez**, na definicao do model. Nenhuma consulta, nenhum schema, nenhum
relatorio.

O criterio nao e antiguidade — as duas nasceram no mesmo `CREATE TABLE`. E que
`created_at` e o nome que o resto do repositorio usa, em toda tabela, e o que
as varreduras de retencao procuram (`cleanup_idempotency_keys.py`,
`expire_cashback.py`). Ficar com `claimed_at` seria a tabela do resgate falando
uma lingua que so ela fala.

## O docstring do model ja tinha a regra, e esta coluna escapou dela

Ele diz, sobre esta mesma tabela:

> *"Sem coluna de status e sem coluna de valor, de proposito: nao ha estado a
> percorrer aqui."*

A disciplina estava escrita e `claimed_at` passou por baixo dela oito dias
antes desta revisao. E o tipo de coisa que so aparece quando alguem varre —
nada quebra, nada fica lento, e a coluna e escrita em todo resgate para
sempre.

## O custo de aplicar, e o que se perde

`ALTER TABLE ... DROP COLUMN` **nao reescreve a tabela** no Postgres: marca a
coluna como apagada no catalogo. Toma `ACCESS EXCLUSIVE` por milissegundos.
`coupon_claims` e nova (28/08/2026) e pequena.

**O que se perde e nada**, e isso e verificavel — as duas colunas tem que ser
iguais linha a linha. **Confira antes de aplicar:**

```sql
SELECT count(*)                                          AS linhas,
       count(*) FILTER (WHERE claimed_at <> created_at)  AS divergentes
  FROM coupon_claims;
```

`divergentes` tem que ser **0**. Se nao for, alguem escreveu uma das duas por
fora (armadilha 33) e elas passaram a significar coisas diferentes — e ai a
premissa desta revisao caiu, porque o que ela apaga deixaria de ser copia.

O `downgrade` recria a coluna com o mesmo `DEFAULT now()`; as linhas antigas
voltam com o instante do downgrade, e nao com o do resgate. **Voltar nao
devolve o dado** — devolve a forma. Como as duas eram iguais, o dado de
verdade continua em `created_at`, que esta revisao nao toca; quem precisar
reconstruir `claimed_at` copia dela:

```sql
UPDATE coupon_claims SET claimed_at = created_at;
```

## Ordem com as outras preparadas

Nenhuma das outras toca em `coupon_claims`. Esta pode ir sozinha, em qualquer
ordem, e o `Revises` e **marcador** (o head do dia em que ela foi escrita) —
ver o passo 2 do `LEIA-ME.md`.

## O codigo acoplado

Uma linha: `claimed_at` em `src/models/coupon_claim_model.py`. Ela sai no
MESMO commit do `git mv` — com a coluna fora do banco e o model ainda
mapeando, **todo `SELECT` de resgate quebra**. E o mesmo motivo da `0058` e da
`0059`.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0062"
down_revision: Union[str, None] = "20260905_0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("coupon_claims", "claimed_at")


def downgrade() -> None:
    """Recria a coluna com o mesmo default de antes.

    `NOT NULL` com `server_default now()` como no `CREATE TABLE` da
    `20260828_0043`: sem o default, o `ADD COLUMN NOT NULL` falharia sobre as
    linhas ja gravadas. As linhas antigas voltam com o instante do DOWNGRADE —
    ver o cabecalho para como devolver o instante de verdade a partir de
    `created_at`.
    """
    op.add_column(
        "coupon_claims",
        sa.Column(
            "claimed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
