"""o papel print_agent entra no CHECK de admin_users.role

Revision ID: 20260814_0020
Revises: 20260812_0019
Create Date: 2026-08-14

## Por que existe um papel de maquina

A senha do usuario do agente de impressao fica em TEXTO PURO no `config.ini`
da maquina do balcao de um restaurante de verdade (`print-agent/config.ini`).
Hoje esse usuario e `attendant`, e `tests/test_auditoria_papeis.py` mediu o
que isso vale para quem ler o arquivo: editar preco de produto, a lista de
clientes com telefone, e o relatorio de faturamento e comissao do restaurante
INTEIRO — sem recorte de filial.

O agente usa oito rotas e mais nenhuma. Um papel que autoriza exatamente
essas oito transforma o `config.ini` de "acesso ao faturamento das duas
lojas" em "acesso as comandas de uma filial".

`print_agent` nao e `attendant` com menos: e papel de MAQUINA. Ele nao deve
aparecer no seletor de usuarios do painel, e o painel deve recusar o login
dele — a conta nao pertence a uma pessoa.

## A armadilha desta revisao: sao DUAS constraints, com a mesma regra

O `pg_dump` da baseline mostra as duas na mesma tabela:

    CONSTRAINT admin_users_role_check CHECK (role = ANY (ARRAY['owner', 'manager', 'attendant']))
    CONSTRAINT ck_admin_users_role    CHECK (role = ANY (ARRAY['owner', 'manager', 'attendant']))

A primeira e o nome que o Postgres gera sozinho; a segunda foi escrita a mao
quando o schema nasceu no painel do Supabase (armadilha 33). Elas dizem
exatamente a mesma coisa, e por isso a duplicata nunca incomodou ninguem.

**Incomoda agora.** Trocar so uma das duas nao produz erro nenhum na
migracao: o `ALTER TABLE` roda, o `alembic upgrade head` termina limpo, e o
INSERT do usuario novo morre depois com "violates check constraint" citando a
constraint que ficou para tras. As duas saem e as duas voltam, com a lista
nova.

`IF EXISTS` no DROP porque nao ha garantia de que os dois nomes existam em
todo banco: a baseline e uma foto de producao, e um banco criado so pelas
revisoes pode ter so um deles.

## O downgrade rebaixa para attendant

Nao ha como manter uma linha `print_agent` sob um CHECK que so aceita tres
papeis, e derrubar a constraint junto seria abrir a tabela para qualquer
texto. Rebaixar para `attendant` devolve o agente ao papel em que ele
funcionava antes desta frente — o agente volta a imprimir, com o acesso
largo que esta revisao existe para fechar. E o unico downgrade que nao para a
cozinha.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260814_0020"
down_revision: Union[str, None] = "20260812_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PAPEIS_DE_PESSOA = "'owner', 'manager', 'attendant'"
PAPEIS_COM_MAQUINA = "'owner', 'manager', 'attendant', 'print_agent'"

# Os dois nomes que a mesma regra tem neste banco. Ver o docstring.
NOMES_DA_CONSTRAINT = ("admin_users_role_check", "ck_admin_users_role")


def upgrade() -> None:
    _recriar_check(PAPEIS_COM_MAQUINA)


def downgrade() -> None:
    # Antes de reapertar o CHECK: sem isto, uma base que ja tenha o usuario do
    # agente rejeita a propria constraint que esta sendo criada, e o downgrade
    # morre no meio deixando a tabela sem CHECK nenhum.
    op.execute("UPDATE admin_users SET role = 'attendant' WHERE role = 'print_agent'")
    _recriar_check(PAPEIS_DE_PESSOA)


def _recriar_check(papeis: str) -> None:
    for nome in NOMES_DA_CONSTRAINT:
        op.execute(f"ALTER TABLE admin_users DROP CONSTRAINT IF EXISTS {nome}")
        op.execute(
            f"ALTER TABLE admin_users ADD CONSTRAINT {nome} "
            f"CHECK (role = ANY (ARRAY[{papeis}]::text[]))"
        )
