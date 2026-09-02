# Revisões escritas e **não aplicadas**

Este diretório não é `alembic/versions/`, e isso é o ponto: o Alembic só
enxerga `versions/` (`script_location = alembic` no `alembic.ini`), então nada
daqui entra em `alembic upgrade head` — nem no CI, nem na suíte `db`, nem no
container. `tests/test_revisoes_preparadas.py` cobra essa propriedade a cada
execução.

O que mora aqui é migração **escrita, revisada e esperando uma decisão que não
é de código**. Sair daqui é um ato deliberado, em três passos:

1. rodar a conferência que o cabeçalho da revisão manda rodar;
2. `git mv` para `alembic/versions/`, **acertando `down_revision`** para o head
   do dia (o valor aqui é um marcador, não uma revisão de verdade);
3. commitar dizendo qual conferência autorizou.

## O que está aqui hoje

| Arquivo | Espera |
|---|---|
| `alinhamento_orm_schema_etapa_1.py` | a contagem de nulos em produção dar zero |
| `alinhamento_orm_schema_etapa_2.py` | a etapa 1 estar aplicada e assada |

As duas são as etapas do roteiro de `docs/alinhamento-orm-schema.md`. **Elas
não podem ir juntas para o mesmo `alembic upgrade`** — o motivo está lá, e é o
único detalhe deste conjunto que não é óbvio.
