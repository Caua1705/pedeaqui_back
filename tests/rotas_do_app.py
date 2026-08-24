"""As rotas do `app`, achatadas — o que `app.routes` deixou de entregar.

## Por que este arquivo existe

Ate o starlette 0.47, `include_router` COPIAVA as rotas do router para dentro
de `app.routes`. Ler as rotas do app era `for rota in app.routes`, e todo
teste de introspeccao fazia exatamente isso.

No starlette 1.6 ele parou de copiar: `app.routes` passou a guardar um
`_IncludedRouter` por chamada de `include_router` — 24 deles, mais os 4
`Route` do `/docs` —, e o `_IncludedRouter` **nao tem atributo `path`**. As
rotas de verdade vivem em `.original_router.routes`, com o prefixo ja
aplicado.

## O que isso custou, e por que nao foi um teste vermelho

`tests/test_papeis_das_rotas.py` e a auditoria que confere o papel exigido por
CADA rota `/admin`. Ela e parametrizada sobre a lista de rotas encontradas —
e com a lista vazia o pytest gera **zero casos**. Nao falha, nao avisa, nao
aparece: a trava que impede rota nova de nascer aberta ao `print_agent`
simplesmente deixa de existir, e o CI continua verde naquele teste.

Dois testes irmaos (`test_o_agente_de_impressao_alcanca_...` e
`test_a_tabela_nao_tem_linha_...`) ficaram vermelhos porque comparam com um
conjunto NAO VAZIO — foram eles que denunciaram. Se a auditoria tivesse so o
teste parametrizado, ninguem teria percebido.

**A licao que fica: teste que se parametriza sobre uma descoberta precisa de
um irmao que exija a descoberta nao ser vazia.** E o que
`test_a_auditoria_enxerga_rotas_admin` passou a fazer.

## O contrato desta funcao

Ela devolve o que `app.routes` devolvia ANTES da 1.6, nas duas versoes. Quem
introspecta rota no repositorio chama isto, e nao `app.routes` direto.
"""

from main import app


def rotas_com_caminho() -> list:
    """Toda rota do `app` que tem `path`, atravessando os agrupadores.

    A ordem e a da declaracao em `main.py`, como era antes: primeiro os
    `Route` do `/docs`, depois cada router na ordem dos `include_router`.
    """
    encontradas = []
    for rota in app.routes:
        encontradas.extend(_achatar(rota))
    return encontradas


def caminhos() -> set[str]:
    """So os caminhos, para o teste que pergunta "esta rota foi registrada?"."""
    return {rota.path for rota in rotas_com_caminho()}


def _achatar(rota) -> list:
    """As rotas finais dentro de um item de `app.routes`.

    Recursiva porque router incluido dentro de router aninharia
    `_IncludedRouter` — hoje nao acontece (todo `include_router` do
    `main.py` recebe um router folha), e a recursao custa tres linhas contra
    ter que descobrir isso de novo no dia em que acontecer.

    **Limite conhecido:** um `include_router(router, prefix="/x")` poe o
    prefixo no agrupador, e nao nas rotas de dentro — o caminho sairia daqui
    sem o `/x`. Nenhuma chamada do `main.py` usa `prefix=` (os prefixos moram
    no `APIRouter(...)` de cada modulo, e ai ja estao no `path` da rota), e
    e por isso que o achatamento simples reproduz os 126 de antes, um a um.
    """
    # starlette < 1.6 — e os `Route` do `/docs` em qualquer versao: o item ja
    # E a rota final.
    if hasattr(rota, "path"):
        return [rota]

    # starlette >= 1.6: o item e um `_IncludedRouter`, que guarda o router em
    # vez de copiar as rotas dele.
    interno = getattr(rota, "original_router", None)
    if interno is None:
        return []

    achatadas = []
    for filha in interno.routes:
        achatadas.extend(_achatar(filha))
    return achatadas
