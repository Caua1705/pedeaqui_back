"""apaga orders.tracking_token, a coluna em texto puro

Revision ID: 20260812_0017
Revises: 20260812_0016
Create Date: 2026-08-12

Segunda e ultima das duas revisoes do token de acompanhamento. **Esta e a que
nao tem volta.**

NAO APLIQUE JUNTO COM A 0016. As duas estao separadas exatamente para que a
0016 possa ir para producao sozinha, com o codigo novo, e ficar la o tempo que
o operador quiser antes de apagar o original. Depois desta revisao o token em
claro nao existe em lugar nenhum do banco, e sha-256 nao se desfaz: o
`downgrade` recria a coluna VAZIA. O unico caminho de volta de verdade e
restaurar backup.

Ordem de deploy, e a razao de cada passo:

    1. deploy do commit que termina na 0016 (revisao + codigo novo juntos)
    2. conferir em producao que a consulta de acompanhamento responde, com um
       link antigo de verdade — nao com um pedido criado depois do deploy
    3. so entao deploy do commit desta revisao

Entre 2 e 3 da para voltar atras a qualquer momento: a coluna em claro ainda
esta la, ainda preenchida, e o codigo antigo volta a le-la.

O BACKFILL RODA DE NOVO ANTES DO DROP, e nao e paranoia: se um container com o
codigo ANTIGO tiver criado pedido depois da 0016 (rollback de imagem, replica
que nao subiu junto, deploy pela metade), aquela linha tem token em claro e
hash nulo. Apagar a coluna sem varrer de novo transformaria esse pedido num
registro sem nenhuma credencial — o cliente perderia o acompanhamento em
silencio, e nao ha como devolver.

Depois do backfill a coluna do hash vira NOT NULL. E o momento certo: e a
primeira vez em que todo pedido, novo e velho, tem hash. Se sobrar alguma
linha sem hash a revisao FALHA aqui, antes do DROP — e falhar antes de apagar
e o comportamento desejado, porque nada foi perdido ainda.
"""

import importlib.util
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0017"
down_revision: Union[str, None] = "20260812_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_da_revisao_anterior():
    """A funcao de backfill da 0016, sem copia-la para ca.

    Duas copias da regra de "como o hash e calculado" seriam duas chances de
    divergir — e uma divergencia aqui nao da erro: grava o hash errado e o
    cliente descobre pelo 404.

    `import` normal nao serve: o nome do arquivo comeca com digito.
    """
    caminho = Path(__file__).resolve().parent / "20260812_0016_hash_do_token_de_acompanhamento.py"
    spec = importlib.util.spec_from_file_location("revisao_0016_backfill", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo.preencher_os_hashes_que_faltam


def upgrade() -> None:
    connection = op.get_bind()
    _backfill_da_revisao_anterior()(connection)

    sem_hash = connection.execute(
        sa.text("SELECT count(*) FROM orders WHERE tracking_token_hash IS NULL")
    ).scalar_one()
    if sem_hash:
        # Antes do DROP, com a coluna em claro ainda no lugar: da para
        # investigar e rodar de novo sem ter perdido nada.
        raise RuntimeError(
            f"{sem_hash} pedido(s) sem tracking_token_hash e sem tracking_token "
            "para derivar. A coluna em claro NAO foi apagada. Investigue antes "
            "de repetir o upgrade."
        )

    op.alter_column("orders", "tracking_token_hash", nullable=False)
    op.drop_column("orders", "tracking_token")


def downgrade() -> None:
    """Recria a coluna, VAZIA. Os tokens nao voltam.

    Ela nasce nullable — o NOT NULL original nao tem como ser restaurado sobre
    linhas sem valor. E o codigo antigo, que busca por `tracking_token`, nao
    volta a funcionar so por causa desta coluna: ele encontraria nulo em todo
    pedido. Este `downgrade` existe para o schema poder voltar, nao para o
    acesso voltar.
    """
    op.add_column("orders", sa.Column("tracking_token", sa.Text(), nullable=True))
    op.alter_column("orders", "tracking_token_hash", nullable=True)
