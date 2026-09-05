# Revisões escritas e **não aplicadas**

Este diretório não é `alembic/versions/`, e isso é o ponto: o Alembic só
enxerga `versions/` (`script_location = alembic` no `alembic.ini`), então nada
daqui entra em `alembic upgrade head` — nem no CI, nem na suíte `db`, nem no
container. `tests/test_alinhamento_orm_schema.py::test_nada_em_preparadas_esta_na_cadeia`
cobra essa propriedade a cada execução, varrendo o diretório inteiro; com ele
vazio, o teste passava sem afirmar nada, e voltou a ter substância em
05/09/2026.

O que mora aqui é migração **escrita, revisada e esperando uma decisão que não
é de código**. Sair daqui é um ato deliberado, em três passos:

1. rodar a conferência que o cabeçalho da revisão manda rodar;
2. `git mv` para `alembic/versions/`, **acertando `down_revision`** para o head
   do dia (o valor aqui é um marcador, não uma revisão de verdade);
3. commitar dizendo qual conferência autorizou.

## O que está aqui hoje

**`20260905_0057` — a indexação do cardápio entra em `ai_usage_events`.** Uma
CHECK, um valor a mais: `surface` passa a aceitar `indexing` ao lado de `text`
e `voice`. O plano de aplicação, com os passos de código que vão **junto**,
está em [`docs/custo-de-ia.md`](../../docs/custo-de-ia.md), seção 7.

**O que ela tem de diferente das duas anteriores, e é o que vale saber:** as
etapas do alinhamento eram schema puro — nenhuma linha de código mudava com
elas. Esta tem **código acoplado**, e o acoplamento é verificável em vez de
lembrado: `AI_SURFACES` é o espelho declarado desta CHECK em
`scripts/espelhos_de_enum.py`, e o portão compara valor a valor. Código antes
do schema deixa o portão vermelho; schema antes do código também. Os dois no
mesmo commit é a única ordem verde — e é por isso que a revisão está aqui
sozinha, sem o código do lado.

`tests/test_revisao_preparada_da_indexacao.py` cobra as três coisas da
armadilha 53: o Alembic não a conhece, ela ainda descreve o schema real, e ela
**roda** (contra o Postgres de teste, numa transação que volta).

## O que as anteriores deixaram de lição

As duas etapas do alinhamento saíram em 04/09/2026, quando o dono decidiu
aplicar o alinhamento inteiro na mesma janela: `20260905_0055` e
`20260905_0056`.

Duas revisões que não podem ir no mesmo `alembic upgrade` continuam sendo duas
execuções depois de entrarem na cadeia. O que as separa então não é mais o
diretório — é `ALEMBIC_TARGET`, e ele é a única parte do roteiro que não avisa
quando é esquecida. Está em `docs/alinhamento-orm-schema.md`.
