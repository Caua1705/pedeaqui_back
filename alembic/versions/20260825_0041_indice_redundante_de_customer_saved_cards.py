"""remove o indice redundante de customer_saved_cards.payment_profile_id

Revision ID: 20260825_0041
Revises: 20260825_0040
Create Date: 2026-08-25

## O que era redundante, e por que nao era obvio

A revisao 20260825_0040 criou DOIS objetos sobre a mesma coluna:

    ix_customer_saved_cards_payment_profile_id   btree (payment_profile_id)
    uq_customer_saved_cards_profile_card         btree (payment_profile_id,
                                                        provider_card_id)

O segundo e o UNIQUE que impede o mesmo cartao aparecer duas vezes no mesmo
perfil, e o Postgres o implementa como um btree comum. **Btree serve consulta
pelo PREFIXO das colunas dele** — entao a busca por `payment_profile_id`
sozinho, que e a do checkout ("os cartoes deste perfil"), ja estava atendida
pelo UNIQUE. O indice de uma coluna nao respondia nenhuma pergunta a mais.

O que ele custava: uma segunda estrutura mantida em todo INSERT de cartao,
sem nenhuma leitura em troca.

## Por que o audit nao acusou

`scripts/audit_indexes.py` procura as duas coisas da armadilha 4 — colisao de
NOME e duplicata de DEFINICAO exata. Este par nao e nenhuma das duas: as
definicoes sao diferentes (uma coluna contra duas), e so a relacao de prefixo
entre elas torna uma dispensavel. Rodado contra o banco com a 0040 aplicada,
o unico achado nessas tabelas foi o falso positivo conhecido do `pkey`
repetido uma vez por FK.

Fica registrado porque e o proximo lugar onde isto vai passar despercebido:
**indice de uma coluna que e prefixo de um UNIQUE composto e sempre
redundante**, e nenhuma ferramenta deste repositorio o aponta hoje.

## Por que AGORA, e nao depois

`customer_saved_cards` nasceu na 0040 e **esta vazia em producao**: nenhum
service escreve nela ainda. O DROP e instantaneo, nao ha janela em que a
consulta do checkout fique sem indice (o UNIQUE nunca sai), e nao ha o custo
da armadilha 5 — que e de CREATE INDEX sobre tabela grande, nao de DROP.

Feito depois, com cartao dentro, seria a mesma decisao com mais medo.

## O DROP e estrito, sem `if_exists`

Quem criou o indice foi a 0040, neste mesmo repositorio, e ele existe
exatamente quando ela esta aplicada. `if_exists=True` aqui nao protegeria de
nada — so esconderia um banco que divergiu do historico, que e justamente o
que a armadilha 33 quer que apareca alto.

## O downgrade recria

Regra da armadilha 4: **desfaca o que ESTA revisao fez.** Foi ela que
derrubou o indice, entao e ela que o devolve — e nao "termine sem o indice
porque ele era inutil". Quem voltar para a 0040 tem que encontrar o schema
que a 0040 descreve, inclusive na parte que esta revisao considerou
dispensavel.

O `CREATE INDEX` do downgrade fica sem `CONCURRENTLY` pelo mesmo motivo que o
da 0040 ficou: `CONCURRENTLY` nao roda dentro de transacao, e a tabela e
pequena o bastante para o lock nao importar. Se um dia ela crescer e este
downgrade for necessario em producao, o caminho e o da armadilha 5 — a mao,
com `CONCURRENTLY`, e `alembic stamp` depois.
"""

from alembic import op


revision = "20260825_0041"
down_revision = "20260825_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_customer_saved_cards_payment_profile_id",
        table_name="customer_saved_cards",
    )


def downgrade() -> None:
    op.create_index(
        "ix_customer_saved_cards_payment_profile_id",
        "customer_saved_cards",
        ["payment_profile_id"],
    )
