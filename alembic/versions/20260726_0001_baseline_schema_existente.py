"""baseline do schema existente

Revisao de marcacao, NAO de criacao. O schema de producao ja existe: foi
construido pelos 13 arquivos .sql de migrations/, aplicados a mao, e nunca
esteve sob controle do Alembic.

Reexecutar aqueles .sql quebraria o banco (CREATE TABLE em tabela que ja
existe, ALTER que ja foi aplicado). Por isso `upgrade()` e um no-op: ele
existe apenas para dar um ponto de partida ao qual as revisoes seguintes
possam se encadear.

Como adotar em um banco que JA tem o schema (producao e o banco atual de
dev):

    alembic stamp 20260726_0001

Isso grava a revisao em alembic_version sem executar DDL nenhum. A partir
dai `alembic upgrade head` aplica somente as revisoes posteriores.

Para um banco VAZIO (dev do zero, CI) esta revisao nao constroi nada. O
repositorio nao tem um dump completo do schema; restaure um dump do banco
atual antes de rodar as migracoes. Gerar esse dump versionado esta na
lista de pendencias da Fase 1.
"""

from typing import Sequence, Union


revision: str = "20260726_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Intencionalmente vazio. Veja o docstring do modulo.
    pass


def downgrade() -> None:
    # Nao ha o que desfazer: esta revisao nunca criou nada.
    pass
