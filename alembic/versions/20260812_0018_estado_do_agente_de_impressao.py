"""estado do agente de impressao, impressoras da maquina, canal de comando

Revision ID: 20260812_0018
Revises: 20260812_0017
Create Date: 2026-08-12

A tela de Impressao do painel existe, mas so o bloco de setores tem backend.
Esta revisao acrescenta o schema dos outros tres blocos.

## `printing_sectors.printer_name` — o defeito que ela conserta

Hoje o mapeamento setor -> impressora existe SO no `config.ini` da maquina do
balcao, casado pelo NOME do setor (`[printers] cozinha = EPSON TM-T20`).
Renomear "Cozinha" para "Cozinha Quente" no painel nao da erro em lugar
nenhum: a via deixa de casar, cai na impressora `default` e a comanda da
cozinha comeca a sair no balcao. Ninguem liga uma coisa a outra.

Com o nome da impressora gravado no setor, o vinculo passa a ser por ID e o
rename deixa de quebrar nada. O `config.ini` continua funcionando como
fallback — instalacao antiga nao pode parar de imprimir por causa de uma
coluna nova.

## `print_agents` — uma linha por filial

Uma filial, uma maquina de balcao, um agente. `branch_id` e UNIQUE por isso.
Se um dia houver duas maquinas na mesma loja, esta e a decisao que muda — e
e melhor que ela quebre visivelmente (409 no heartbeat) do que duas maquinas
sobrescreverem o `last_seen_at` uma da outra e o painel mostrar "online"
quando so uma esta.

## `print_agent_printers` — o que o painel precisa para montar a lista

Sem isto, o lojista digita o nome da impressora a mao no painel e um espaco
a mais faz a via sumir. O agente reporta o que o Windows enxerga e a tela
vira um seletor.

## `print_agent_commands` — o canal painel -> agente

Nao existia: o agente so tem o stream de pedidos, que e de entrada. O
comando NAO ganha um canal novo — ele entra no MESMO stream SSE, como mais
um tipo de evento. O motivo e a armadilha 20: qualquer fila em memoria
morreria com mais de um worker e no deploy do meio do almoco. O stream ja
resolve isso com cursor no banco, replay por `Last-Event-ID` e reconexao
automatica; um segundo mecanismo seria um segundo lugar para errar.

Por isso a tabela tem `created_at` indexado junto de `branch_id`: e por ele
que o poll do stream pergunta "o que mandaram depois do cursor".

Nao ha `delivered_at`. Entrega e responsabilidade do cursor, e marcar
entrega exigiria o agente escrever de volta — mais uma rota, para saber uma
coisa que o `last_seen_at` do heartbeat ja aproxima. A repeticao (o replay
de ate uma hora) e tratada no agente, que lembra os comandos que ja
executou no mesmo arquivo de estado dos pedidos ja impressos.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260812_0018"
down_revision: Union[str, None] = "20260812_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, e sem backfill: instalacao que ja funciona pelo config.ini
    # continua funcionando. Nulo aqui significa "resolva pelo config.ini",
    # que e exatamente o comportamento de hoje.
    op.add_column("printing_sectors", sa.Column("printer_name", sa.Text(), nullable=True))

    op.create_table(
        "print_agents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("agent_version", sa.Text(), nullable=True),
        # NOT NULL: uma linha aqui nasce de um heartbeat, e heartbeat sem
        # instante nao e sinal nenhum.
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "print_agent_printers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # A impressora marcada como padrao no Windows. O painel a destaca:
        # e nela que a via cai quando o setor nao tem impressora escolhida.
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reported_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_print_agent_printers_branch_name",
        "print_agent_printers",
        ["branch_id", "name"],
    )

    op.create_table(
        "print_agent_commands",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column("command_type", sa.Text(), nullable=False),
        sa.Column("printer_name", sa.Text(), nullable=True),
        sa.Column(
            "printing_sector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("printing_sectors.id"),
            nullable=True,
        ),
        # Quem mandou. Um teste de impressao gasta bobina e chama atencao no
        # balcao; sem esta coluna nao ha como responder "quem apertou".
        sa.Column(
            "created_by_admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # A consulta do poll do stream, inteira: "comandos desta filial depois do
    # cursor". Sem indice, cada poll de cada agente varre a tabela.
    #
    # Estrito, sem if_not_exists: a tabela nasce nesta revisao, colisao de
    # nome aqui seria bug e deve estourar (convencao da armadilha 4).
    op.create_index(
        "ix_print_agent_commands_branch_created_at",
        "print_agent_commands",
        ["branch_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_print_agent_commands_branch_created_at", table_name="print_agent_commands"
    )
    op.drop_table("print_agent_commands")
    op.drop_constraint(
        "uq_print_agent_printers_branch_name", "print_agent_printers", type_="unique"
    )
    op.drop_table("print_agent_printers")
    op.drop_table("print_agents")
    op.drop_column("printing_sectors", "printer_name")
