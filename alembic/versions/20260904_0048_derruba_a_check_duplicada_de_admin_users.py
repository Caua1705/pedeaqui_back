"""derruba a CHECK duplicada de admin_users.role

Revision ID: 20260904_0048
Revises: 20260904_0047
Create Date: 2026-09-04

## As duas constraints, e por que ninguem as viu

`admin_users` tem DUAS CHECK identicas sobre `role`, as duas no
`schema_baseline.sql` — quer dizer, no banco de verdade:

    CONSTRAINT admin_users_role_check CHECK (role = ANY (ARRAY[...]))
    CONSTRAINT ck_admin_users_role    CHECK (role = ANY (ARRAY[...]))

A primeira e o nome que o Postgres gera sozinho quando o CHECK e escrito
inline no `CREATE TABLE`; a segunda foi criada a mao pela revisao
`20260726_0003`. As duas dizem exatamente a mesma coisa, e o Postgres avalia
as DUAS em todo INSERT e em todo UPDATE de `admin_users`.

**E a armadilha 4 na forma de constraint, e ela escapou por um motivo
concreto: `scripts/audit_indexes.py` nao olha constraint.** Foi
`scripts/espelhos_de_enum.py` — que le `pg_constraint` para conferir as
listas de enum contra o codigo — que a encontrou, na primeira execucao, em
04/09/2026.

A revisao `20260814_0020` ja sabia das duas e tratou disso do jeito certo
para o que ela fazia: acrescentar `print_agent` a UMA delas nao daria erro
nenhum na migracao, e o INSERT do usuario do agente morreria depois citando a
constraint que ficou para tras. Ela recria as duas, em sincronia. O que ela
nao fez — e nao era o assunto dela — foi tirar a sobra.

## O que esta revisao faz, e o que ela NAO faz

Derruba `admin_users_role_check` e mantem `ck_admin_users_role`. O nome
canonico e o segundo por duas razoes que apontam para o mesmo lado: e o
padrao de nome do repositorio (`ck_<tabela>_<coluna>`), e e o que uma revisao
do Alembic criou explicitamente — o outro e um efeito colateral de como a
tabela nasceu no painel do Supabase (armadilha 33).

**Nao ha perda de garantia.** As duas tem a mesma definicao, conferida no
banco antes de escrever esta revisao: quem sobra recusa exatamente o que as
duas recusavam.

`DROP CONSTRAINT` toma `ACCESS EXCLUSIVE` e e INSTANTANEO — nao varre a
tabela, ao contrario do `ADD CONSTRAINT ... CHECK` sem `NOT VALID`
(armadilha 53). E `admin_users` tem dezenas de linhas, nao milhoes.

`IF EXISTS` no DROP pelo mesmo motivo da `0020`: a baseline e uma foto de
producao, e um banco criado so pelas revisoes pode nao ter o nome gerado
pelo Postgres.

## A pegadinha da ORDEM, para quem for mexer no CHECK de `role` depois

A `0020` continua com `NOMES_DA_CONSTRAINT = ("admin_users_role_check",
"ck_admin_users_role")` e continua RECRIANDO as duas. Isso esta certo e nao
deve ser editado — revisao aplicada nao se reescreve. O que acontece e que a
duplicata volta a existir por um instante em quem reconstruir o banco do
zero, e esta revisao, que roda depois, a derruba de novo. O estado final e o
mesmo pelos dois caminhos.

**O que muda para a proxima:** revisao que mexer nos papeis daqui em diante
usa `ck_admin_users_role` e mais nenhum nome. Recriar o par seria desfazer
isto sem perceber.

## O downgrade devolve a duplicata

"Desfaca o que ESTA revisao fez", e nao "termine sem a constraint" — a mesma
regra que separou o downgrade da `20260810_0012` do da `20260823_0036`. Ele
recria `admin_users_role_check` com a lista de HOJE (a de quatro papeis, com
`print_agent`), lida da `0020`: recria-la com tres derrubaria o proprio
usuario do agente de impressao na volta.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260904_0048"
down_revision: Union[str, None] = "20260904_0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINT_DUPLICADA = "admin_users_role_check"

# A MESMA lista da `20260814_0020`, e ela e repetida aqui de proposito: o
# downgrade tem que devolver a constraint com os papeis de hoje, e importar de
# outra revisao amarraria as duas para sempre.
PAPEIS_COM_MAQUINA = "'owner', 'manager', 'attendant', 'print_agent'"


def upgrade() -> None:
    op.execute(f"ALTER TABLE admin_users DROP CONSTRAINT IF EXISTS {CONSTRAINT_DUPLICADA}")


def downgrade() -> None:
    op.execute(
        f"ALTER TABLE admin_users ADD CONSTRAINT {CONSTRAINT_DUPLICADA} "
        f"CHECK (role = ANY (ARRAY[{PAPEIS_COM_MAQUINA}]::text[]))"
    )
