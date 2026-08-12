"""prende o usuario do agente de impressao a uma filial

Revision ID: 20260812_0015
Revises: 20260812_0014
Create Date: 2026-08-12

O QUE ESTAVA ERRADO. `impressora.junior@pederapidex.com` e o usuario que o
agente de impressao do balcao usa para logar. Ele foi criado sem
`--branch-id`, entao `admin_users.branch_id` ficou nulo — e nulo, em
`src/api/dependencies/admin_scope.py`, significa **todas as filiais do
restaurante**. O Junior da Picanha tem duas.

O estrago nao e vazamento de dado: e producao errada. O agente roda numa
maquina so, com as impressoras de uma loja so, e recebe pelo stream os
pedidos das DUAS. A comanda da outra unidade sai na chapa desta, alguem
prepara, e o pedido de verdade daquela loja nunca e impresso la.

COMO A FILIAL E ESCOLHIDA AQUI. Nao ha, no banco, campo que diga "esta
maquina fica nesta loja". O sinal mais proximo e o setor de impressao:
`printing_sectors` pende de `branch_id` justamente porque a impressora e um
objeto fisico da filial (ver a revisao 0011). Entao a regra e:

    a filial e aquela que tem setor de impressao ativo — se for exatamente
    uma.

ZERO OU DUAS FILIAIS COM SETOR: NAO ADIVINHA. Empatado, a revisao nao
escreve nada e deixa um NOTICE no log do `alembic upgrade`. Escolher errado
seria pior que o defeito atual: com nulo o agente ao menos imprime as
proprias comandas junto com as alheias; com a filial errada ele para de
imprimir as proprias, em silencio, que e o modo de falha caro (armadilha 13).

NAO DERRUBA O DEPLOY. `docker-entrypoint.sh` roda `alembic upgrade head` com
`set -e` antes do Uvicorn (armadilha 5): uma excecao aqui viraria loop de
restart da API inteira por causa de um usuario. Por isso NOTICE, nunca
EXCEPTION — inclusive quando o usuario nao existe, que e o caso de todo
banco que nao seja o de producao.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260812_0015"
down_revision: Union[str, None] = "20260812_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMAIL_DO_AGENTE = "impressora.junior@pederapidex.com"

# O SQL fica em constante, e nao embutido no `upgrade`, para o teste da suite
# `db` poder executar exatamente o mesmo texto contra um banco descartavel. A
# regra de escolha da filial e a parte que precisa de prova; ler o NOTICE do
# `alembic upgrade` de producao nao e prova de nada.
SQL_PRENDE_A_FILIAL = f"""
        DO $$
        DECLARE
          usuario_id     uuid;
          restaurante_id uuid;
          filial_id      uuid;
          quantas        integer;
        BEGIN
          SELECT id, restaurant_id INTO usuario_id, restaurante_id
            FROM admin_users
           WHERE email = '{EMAIL_DO_AGENTE}'
             AND branch_id IS NULL;

          IF usuario_id IS NULL THEN
            RAISE NOTICE 'Agente de impressao: % nao existe ou ja tem filial. Nada a fazer.',
                         '{EMAIL_DO_AGENTE}';
            RETURN;
          END IF;

          SELECT count(*) INTO quantas
            FROM (SELECT DISTINCT s.branch_id
                    FROM printing_sectors s
                    JOIN branches b ON b.id = s.branch_id
                   WHERE b.restaurant_id = restaurante_id
                     AND b.is_active IS NOT FALSE
                     AND s.is_active) AS filiais_que_imprimem;

          IF quantas <> 1 THEN
            RAISE NOTICE 'Agente de impressao: % filiais com setor de impressao ativo. '
                         'Nao da para escolher; branch_id continua nulo. '
                         'Rode o UPDATE a mao com o id da filial certa.', quantas;
            RETURN;
          END IF;

          SELECT DISTINCT s.branch_id INTO filial_id
            FROM printing_sectors s
            JOIN branches b ON b.id = s.branch_id
           WHERE b.restaurant_id = restaurante_id
             AND b.is_active IS NOT FALSE
             AND s.is_active;

          UPDATE admin_users SET branch_id = filial_id WHERE id = usuario_id;
          RAISE NOTICE 'Agente de impressao preso a filial %.', filial_id;
        END
        $$;
"""


def upgrade() -> None:
    op.execute(SQL_PRENDE_A_FILIAL)


def downgrade() -> None:
    # Volta ao nulo, que e o estado de onde esta revisao partiu. Nao restaura
    # "a filial que estava antes": antes nao havia filial nenhuma.
    op.execute(
        f"UPDATE admin_users SET branch_id = NULL WHERE email = '{EMAIL_DO_AGENTE}';"
    )
