"""o cpf sai do schema: a coluna vazia e o indice unico parcial dela

Revision ID: 20260906_0061
Revises: 20260905_0058
Create Date: 2026-09-06

**ESCRITA E NAO APLICADA.** Este arquivo mora em `alembic/preparadas/`, que o
Alembic nao le (`script_location = alembic` faz ele enxergar so `versions/`).
Nada aqui entra em `alembic upgrade head` — nem no CI, nem na suite `db`, nem
no container. Ver `alembic/preparadas/LEIA-ME.md` e a armadilha 53.

Auditoria de 05/09/2026, §2.1.

---

## O ciclo que a revisao `20260812_0019` pediu ja passou

Aquela revisao anulou todos os CPFs e escreveu, com todas as letras:

> *"A coluna vazia deve ser derrubada numa revisao proxima, depois de um ciclo
> confirmando que nada a le."*

Ela e de 12/08/2026. **Vinte e cinco dias depois, nada a le.** A varredura da
auditoria comparou as 585 colunas do schema real contra todo o `src/` e
`cpf` tem ZERO ocorrencias como atributo, kwarg ou literal — nem no model,
que a mapeia e nunca a usa.

## O que se derruba, e por que sao DUAS coisas

| Objeto | O que e |
|---|---|
| `customers.cpf` | `text`, nullable, sem default. Vazia |
| `idx_customers_cpf_unique` | `UNIQUE ... WHERE cpf IS NOT NULL`. Vazio, e mantido em toda escrita de `customers` |

O indice cai junto **porque ele cai junto de qualquer jeito**: o Postgres
derruba indice de coluna removida sozinho. Ele esta escrito aqui de propósito,
antes do `drop_column`, por duas razoes — o `downgrade` precisa recria-lo na
ordem certa, e um `DROP` implicito e um `DROP` que ninguem revisa.

## O que isto NAO derruba

**`is_valid_cpf`, em `src/utils/normalization.py`, fica.** Ela nao tem
consumidor em `src/` hoje (so testes), e mesmo assim fica: a `0019` registrou
que "se um dia houver [nota fiscal], o CPF volta pedido no checkout de quem
quer nota". O algoritmo dos dois digitos verificadores e a parte cara de
reescrever, esta testado, e nao custa nada em runtime.

Isso e uma escolha, e ela contradiz de leve o §7.1 da auditoria ("nao ha
funcao sem consumidor em `src/`"): ha uma, e e esta. Fica anotado aqui em vez
de virar um `TODO` que ninguem le.

## O custo de aplicar, e o que nao volta

`ALTER TABLE ... DROP COLUMN` **nao reescreve a tabela** no Postgres — ele
marca a coluna como apagada no catalogo. Toma `ACCESS EXCLUSIVE`, entao espera
a transacao em curso terminar, mas nao segura o lock varrendo nada.
`customers` e uma das tabelas grandes do banco e isso continua sendo
instantaneo.

**O downgrade recria a coluna e o indice VAZIOS.** Nao ha o que devolver: a
`0019` ja anulou os valores em 12/08/2026, e o que esta revisao apaga e uma
coluna sem dado dentro. **Confirme isso antes de aplicar** — e a unica
conferencia que este arquivo pede:

```sql
SELECT count(*) AS clientes,
       count(cpf) AS com_cpf          -- tem que ser 0
  FROM customers;
```

Se `com_cpf` nao for zero, **pare**: alguem escreveu CPF por fora depois da
`0019` (armadilha 33), e essa linha e dado pessoal que esta revisao apagaria
sem registro nenhum. O caminho, nesse caso, e descobrir quem escreveu antes de
derrubar.

## Ordem com as outras preparadas

Nenhuma das outras toca em `customers`. Esta pode ir sozinha, em qualquer
ordem, e o `Revises` e **marcador** (o head do dia em que ela foi escrita) —
ver o passo 2 do `LEIA-ME.md`.

## O codigo acoplado

Uma linha: `cpf` em `src/models/customer_model.py`. Ela sai no MESMO commit do
`git mv`, pelo motivo da `0058` e da `0059` — com a coluna fora do banco e o
model ainda mapeando, **todo `SELECT` de cliente quebra**.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260906_0061"
down_revision: Union[str, None] = "20260905_0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NOME_DO_INDICE = "idx_customers_cpf_unique"


def upgrade() -> None:
    # Explicito antes do `drop_column`, apesar de o Postgres derrubar o indice
    # de uma coluna removida sozinho: assim o objeto que some aparece no
    # arquivo, e o downgrade tem o par dele escrito ao lado.
    op.drop_index(NOME_DO_INDICE, table_name="customers")
    op.drop_column("customers", "cpf")


def downgrade() -> None:
    """Recria a coluna e o indice, os dois VAZIOS.

    Nao ha dado a devolver: a revisao `20260812_0019` anulou os valores em
    12/08/2026, e o upgrade acima derruba uma coluna sem nada dentro.
    """
    op.add_column("customers", sa.Column("cpf", sa.Text(), nullable=True))
    op.create_index(
        NOME_DO_INDICE,
        "customers",
        ["cpf"],
        unique=True,
        postgresql_where=sa.text("cpf IS NOT NULL"),
    )
