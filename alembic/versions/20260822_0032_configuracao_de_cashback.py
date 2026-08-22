"""configuracao de cashback e integridade do razao

Revision ID: 20260822_0032
Revises: 20260822_0031
Create Date: 2026-08-22

O cashback existia pela metade: `cashback_transactions` era lida, listada e
somada, e NINGUEM escrevia nela (armadilha 26). Esta revisao prepara o schema
para o credito e o resgate. Desenho completo em `docs/cashback.md`.

Sao QUATRO coisas, e as duas primeiras sao conserto do que ja estava la.

## 1. Os indices tem outro nome no banco (armadilha 4)

Conferido no banco vivo em 22/08/2026: os indices de `cashback_transactions`
sao `idx_cashback_transactions_*`, criados a mao pelos `.sql` de
`migrations/`, e o model declara `ix_*`. Os nomes NAO batem.

Criar os do model com `if_not_exists` seria o pior caminho possivel: o
`IF NOT EXISTS` casa por NOME, entao o Postgres criaria uma SEGUNDA copia de
cada indice sobre as mesmas colunas — mantida em toda escrita, servindo a
nenhuma consulta a mais. E o caso do `idx_order_items_order_id`, que a
revisao 20260810_0012 resolveu ADOTANDO o indice existente.

Mesma saida aqui: DROP do nome antigo e CREATE do canonico, na mesma
transacao, DROP primeiro para nao manter as duas copias em disco.

`idx_cashback_transactions_customer_status` vira
`ix_cashback_transactions_customer_id_status` — o nome do model, e o unico
dos quatro em que a diferenca nao e so o prefixo.

## 2. Os dois CHECK nunca existiram

O model declara `ck_cashback_transactions_type` e `..._status` desde sempre.
O banco nao tem nenhum dos dois: a tabela de producao nasceu do
`20260704_ensure_cashback_transactions.sql`, cujo `CREATE TABLE IF NOT
EXISTS` nao os traz — e o `20260703`, que os tinha inline, foi no-op sobre a
tabela ja existente.

Na pratica dava para gravar `type='banana'`, e a leitura do extrato morreria
em `CashbackService.DESCRIPTIONS[transaction.type]` com KeyError — no app do
cliente, na tela de saldo.

**Criar CHECK sobre tabela com linha invalida FALHA**, e por isso a auditoria
veio antes: a tabela esta VAZIA (zero linhas em 22/08/2026, conferido). Nao
ha o que limpar e a criacao e instantanea. Se um dia esta revisao for
aplicada a um banco com dado sujo dentro, ela morre aqui — e morrer e o
comportamento certo.

## 3. A configuracao, que nao existia em lugar nenhum

`cashback_rules` segue o regime de TERMO COMERCIAL da revisao 20260818_0025:
padrao do restaurante, sobrescrita por filial. O motivo e o percentual por
dia da semana — ele existe para mover o dia fraco, e o dia fraco de uma
filial nao e o da outra.

**A heranca aqui e por LINHA, e nao por coluna como na 0025.** Nao e
descuido, e a diferenca precisa ser sabida: la `NULL` numa coluna da filial
significa "herda o valor do restaurante"; aqui a filial tem a regra INTEIRA
ou nao tem nenhuma. O que impede a heranca por coluna e o percentual por dia:
ele mora numa tabela filha, e "coluna nula" nao existe numa tabela filha.
Uma regra meio herdada — percentual base do restaurante, terca-feira da
filial — nao e explicavel para o lojista nem para quem for depurar as tres
da manha.

`branch_id IS NULL` e a linha do restaurante. O UNIQUE parcial existe porque
UNIQUE comum aceita varios NULL, e duas linhas de padrao para o mesmo
restaurante seriam duas respostas para a mesma pergunta.

A FK composta `(restaurant_id, branch_id)` aproveita o
`uq_branches_restaurant_id` da revisao 20260820_0026 e impede a regra de uma
filial de outro restaurante. Ela e MATCH SIMPLE (o padrao): com `branch_id`
nulo a checagem nao roda, que e exatamente o que a linha de padrao precisa.

### `cashback_rule_weekdays`: dia AUSENTE herda `default_percent`

E a decisao mais importante desta tabela, e ela e o oposto do `PUT` de
horarios (armadilha 3), onde dia ausente = dia fechado. Aqui dia ausente =
percentual padrao daquele mesmo nivel.

Se ausente valesse zero, o lojista que configurasse SO a terca de 10%
desligaria o cashback dos outros seis dias — sem erro, sem log, e com a tela
mostrando exatamente o que ele digitou. E o mesmo defeito da armadilha 3, com
dinheiro no lugar do horario.

`weekday` 0 = SEGUNDA, igual a `branch_business_hours` e ao Python
(armadilha 1). O `getDay()` do JavaScript e 0 = domingo: o painel que mandar
o numero do JS configura a terca de 10% na segunda.

Chave primaria composta `(rule_id, weekday)` e nao um `id` sintetico: a
chave natural ja e unica, e um surrogate aqui so criaria a chance de
existirem duas linhas para a mesma terca-feira.

## 4. `branch_payment_methods.earns_cashback`

"Quais formas de pagamento geram cashback" vai como coluna na tabela que JA
manda em forma de pagamento por filial, e nao como lista nova.

A armadilha 15 registra o preco de ter DUAS listas de forma de pagamento que
precisam mudar juntas (`PAYMENT_METHODS` e o CHECK da tabela). Uma terceira
lista — um array ou um jsonb de metodos que geram — seria o mesmo erro pela
terceira vez, e a revisao 20260820_0027 acabou de matar um jsonb de formas de
pagamento justamente por poder discordar da tabela.

`DEFAULT true` porque a chave que decide se ha cashback e
`cashback_rules.enabled`. Com ela desligada o default nao faz nada; ligada,
o lojista desmarca o que nao quer.

## A linha semeada nasce DESLIGADA

Todo restaurante ganha uma regra padrao com os numeros combinados (5%, saldo
minimo de R$ 5, validade de 60 dias) e `enabled = false`.

Semear desligado, em vez de nao semear, faz de "ligar o cashback" um UPDATE
de um booleano — em vez de um INSERT que alguem escreve a mao no Supabase as
pressas, com uma coluna faltando e o percentual em branco. E enquanto
`enabled` for falso nada acontece: nao credita, nao resgata, nao expira.

Nao ha percentual por dia semeado. O Junior comeca com o padrao em todos os
dias, e a terca de 10% e conversa a ter com ele antes.

## O que esta revisao NAO faz

Nao toca em `orders`: `cashback_redeemed_amount` e `discount_total` ja
existem, ja sao NOT NULL DEFAULT 0 e ja sao gravados (com zero). Ligar o
resgate nao muda o schema do pedido — muda o valor que entra naquela coluna.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260822_0032"
down_revision: Union[str, None] = "20260822_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Os quatro indices da tabela, com o nome que ELES tem no banco e o nome que
# o model declara. Uma lista so, para o upgrade e o downgrade nao poderem
# discordar sobre qual e qual.
#
# (nome no banco, nome canonico, colunas, unico, filtro parcial)
INDICES_DO_RAZAO = (
    (
        "idx_cashback_transactions_customer_id",
        "ix_cashback_transactions_customer_id",
        ["customer_id"],
        False,
        None,
    ),
    (
        "idx_cashback_transactions_customer_status",
        "ix_cashback_transactions_customer_id_status",
        ["customer_id", "status"],
        False,
        None,
    ),
    (
        "idx_cashback_transactions_created_at",
        "ix_cashback_transactions_created_at",
        ["created_at"],
        False,
        None,
    ),
    (
        "idx_cashback_transactions_idempotency_key",
        "ux_cashback_transactions_idempotency_key",
        ["idempotency_key"],
        True,
        "idempotency_key IS NOT NULL",
    ),
)


def upgrade() -> None:
    _adotar_indices_do_razao()

    # Estritos, sem `if_not_exists`: conferido que nao existem, e a tabela
    # esta vazia. Falhar aqui e o comportamento certo se alguem os tiver
    # criado por fora nesse meio tempo.
    op.create_check_constraint(
        "ck_cashback_transactions_type",
        "cashback_transactions",
        "type IN ('earned', 'redeemed', 'expired', 'cancelled', 'adjustment')",
    )
    op.create_check_constraint(
        "ck_cashback_transactions_status",
        "cashback_transactions",
        "status IN ('pending', 'available', 'used', 'cancelled', 'expired')",
    )

    op.create_table(
        "cashback_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL = a regra padrao da rede. Preenchido = a sobrescrita daquela
        # filial, que vale INTEIRA no lugar da padrao.
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("default_percent", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("min_redeem_balance", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("expiry_days", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "default_percent >= 0 AND default_percent <= 100",
            name="ck_cashback_rules_default_percent",
        ),
        sa.CheckConstraint("min_redeem_balance >= 0", name="ck_cashback_rules_min_redeem_balance"),
        # Validade tem que ser prazo, nao zero: `expiry_days = 0` faria o
        # saldo vencer no mesmo dia em que foi creditado.
        sa.CheckConstraint("expiry_days > 0", name="ck_cashback_rules_expiry_days"),
        # A filial tem que ser DAQUELE restaurante — mesma amarra composta da
        # revisao 20260820_0026, e MATCH SIMPLE nao a exige quando branch_id
        # e nulo, que e o caso da linha de padrao.
        sa.ForeignKeyConstraint(
            ["restaurant_id", "branch_id"],
            ["branches.restaurant_id", "branches.id"],
            name="fk_cashback_rules_branch_do_restaurante",
        ),
    )
    # Uma sobrescrita por filial...
    op.create_index(
        "ux_cashback_rules_branch",
        "cashback_rules",
        ["restaurant_id", "branch_id"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NOT NULL"),
    )
    # ...e UMA padrao por restaurante. Sao dois indices porque UNIQUE comum
    # aceita varios NULL, e duas linhas de padrao seriam duas respostas para
    # a mesma pergunta.
    op.create_index(
        "ux_cashback_rules_padrao_do_restaurante",
        "cashback_rules",
        ["restaurant_id"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NULL"),
    )

    op.create_table(
        "cashback_rule_weekdays",
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cashback_rules.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # 0 = SEGUNDA, como `datetime.weekday()` e como
        # `branch_business_hours` (armadilha 1).
        sa.Column("weekday", sa.SmallInteger(), primary_key=True),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_cashback_rule_weekdays_weekday"),
        sa.CheckConstraint(
            "percent >= 0 AND percent <= 100",
            name="ck_cashback_rule_weekdays_percent",
        ),
    )

    op.add_column(
        "branch_payment_methods",
        sa.Column("earns_cashback", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    # Regra padrao DESLIGADA para todo restaurante que ja existe. Ligar passa
    # a ser um UPDATE de um booleano, e nao um INSERT escrito a mao.
    op.execute(
        """
        INSERT INTO cashback_rules (restaurant_id, enabled, default_percent,
                                    min_redeem_balance, expiry_days)
        SELECT id, false, 5.00, 5.00, 60 FROM restaurants
        """
    )


def downgrade() -> None:
    op.drop_column("branch_payment_methods", "earns_cashback")
    op.drop_table("cashback_rule_weekdays")
    op.drop_index("ux_cashback_rules_padrao_do_restaurante", table_name="cashback_rules")
    op.drop_index("ux_cashback_rules_branch", table_name="cashback_rules")
    op.drop_table("cashback_rules")

    op.drop_constraint("ck_cashback_transactions_status", "cashback_transactions", type_="check")
    op.drop_constraint("ck_cashback_transactions_type", "cashback_transactions", type_="check")

    _devolver_indices_do_razao()


def _adotar_indices_do_razao() -> None:
    """Troca os nomes de mao pelos canonicos. DROP antes do CREATE."""
    for nome_no_banco, nome_canonico, colunas, unico, filtro in INDICES_DO_RAZAO:
        op.drop_index(nome_no_banco, table_name="cashback_transactions")
        op.create_index(
            nome_canonico,
            "cashback_transactions",
            colunas,
            unique=unico,
            postgresql_where=sa.text(filtro) if filtro else None,
        )


def _devolver_indices_do_razao() -> None:
    for nome_no_banco, nome_canonico, colunas, unico, filtro in INDICES_DO_RAZAO:
        op.drop_index(nome_canonico, table_name="cashback_transactions")
        op.create_index(
            nome_no_banco,
            "cashback_transactions",
            colunas,
            unique=unico,
            postgresql_where=sa.text(filtro) if filtro else None,
        )
