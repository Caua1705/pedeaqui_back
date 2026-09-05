# Revisões escritas e **não aplicadas**

**Hoje este diretório está vazio.** As duas que moravam aqui — as etapas do
alinhamento ORM × schema — foram para `versions/` em 04/09/2026, como
`20260905_0055` e `20260905_0056`. O diretório fica porque o mecanismo fica.

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

| Revisão | O que faz | Plano | Recomendação |
|---|---|---|---|
| `20260905_0058` | `branches` perde as 5 colunas de endereço mortas | `docs/modelo-de-dados.md`, "PENDENTE: quebrar a tabela `branches`" | **fazer** — não move dado, não muda comportamento |
| `20260905_0059` | a tarifa de entrega sai de `branches` para uma tabela 1:1 opcional | idem | **decidir** — é a única das quatro divisões candidatas que paga |

`tests/test_revisoes_preparadas_de_branches.py` cobra as três coisas da
armadilha 53 nas duas, e a `0059` é exercitada **com linha na mesa**: um
`INSERT ... SELECT` sobre tabela vazia é no-op, e provar só ele seria não provar
nada.

**As duas têm código acoplado**, e é por isso que estão aqui sozinhas: com as
colunas fora do banco e `src/models/branch_model.py` ainda mapeando, todo
`SELECT` de filial quebra. O `git mv` e as linhas do model vão no mesmo commit.

O `Revises` das duas é **marcador**: é o head do dia em que foram escritas.

## O que as anteriores deixaram de lição

As duas etapas do alinhamento saíram em 04/09/2026, quando o dono decidiu
aplicar o alinhamento inteiro na mesma janela: `20260905_0055` e
`20260905_0056`.

Duas revisões que não podem ir no mesmo `alembic upgrade` continuam sendo duas
execuções depois de entrarem na cadeia. O que as separa então não é mais o
diretório — é `ALEMBIC_TARGET`, e ele é a única parte do roteiro que não avisa
quando é esquecida. Está em `docs/alinhamento-orm-schema.md`.
