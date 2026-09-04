"""A frase de cada colisao de unicidade, e a traducao do `IntegrityError`.

## O buraco que este modulo fecha

O cardapio e a impressao usam o desenho **confere-depois-insere**: uma consulta
pergunta se o nome ja existe e responde 409 com frase; o indice unico do banco
segura o resto. As duas metades sao necessarias — a trava protege a tabela, a
conferencia explica ao lojista — e e assim que o repositorio inteiro trabalha.

O que faltava era a trava saber falar quando **ela** e quem pega. Entre a
conferencia e o `INSERT` nao ha nada segurando a linha: dois cliques no mesmo
botao passam os dois pela consulta antes de qualquer commit, o segundo `INSERT`
bate no indice, e nao havia **um unico** `except IntegrityError` na aplicacao.
O lojista recebia **500** — sem saber se o produto foi criado. Ele clicava de
novo e ai recebia o 409, que era a resposta certa desde o primeiro clique.

Nada de errado acontecia com os dados: o indice fez o trabalho dele. O que
estava quebrado era a frase.

## A frase mora AQUI, e nao nas duas pontas

Este arquivo existe para que a conferencia e a traducao digam **a mesma coisa**.
Escritas separadas, elas divergem no dia em que alguem melhorar uma — e ai a
mesma colisao passa a ter dois textos, dependendo de qual das duas pegou, que e
justamente a diferenca que o lojista nao consegue enxergar (uma corrida).

E a mesma razao de `canal_utilizavel` morar ao lado da versao SQL dele
(armadilha 54): duas formas da mesma pergunta em arquivos diferentes divergem.

## Restricao desconhecida NAO vira 409

`traduzir` devolve `None` para o que nao esta na tabela, e quem chama levanta o
erro original. Traduzir tudo seria pior que nao traduzir nada: uma FK violada —
que e defeito nosso — chegaria ao lojista como "ja existe um produto com esse
nome", e ele passaria a tarde procurando um produto que nao existe. O 500 e
feio e e honesto; o 409 errado e mentira.
"""

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError


# Nome da restricao no banco -> a frase que o lojista le.
#
# **Os nomes sao os do BANCO**, e `tests/test_conflito_de_unicidade_db.py` cobra
# que cada um exista de verdade. Sem essa trava, uma revisao que renomeasse um
# indice deixaria a traducao muda: a colisao voltaria a ser 500, e o teste
# continuaria verde porque a corrida nao acontece na suite.
FRASE_POR_RESTRICAO = {
    "uq_categories_branch_slug": "Já existe uma categoria com esse nome nesta filial",
    "uq_products_branch_slug": "Já existe um produto com esse nome nesta filial",
    "uq_products_branch_catalog_key": (
        "Já existe um produto com essa chave de catálogo nesta filial"
    ),
    "uq_printing_sectors_branch_name": "Já existe um setor com esse nome nesta filial",
}


def conflito(restricao: str) -> HTTPException:
    """O 409 daquela colisao. Usado pela CONFERENCIA, antes do INSERT."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=FRASE_POR_RESTRICAO[restricao]
    )


def traduzir(erro: IntegrityError) -> HTTPException | None:
    """O 409 daquela colisao. Usado quando o INDICE e quem pegou.

    `None` quer dizer "nao sei o que e isto" — e quem chama levanta o erro
    original, que e o comportamento certo. Ver o cabecalho.
    """
    restricao = _nome_da_restricao(erro)
    if restricao is None or restricao not in FRASE_POR_RESTRICAO:
        return None
    return conflito(restricao)


def _nome_da_restricao(erro: IntegrityError) -> str | None:
    """O nome que o Postgres carimbou no erro.

    Vem de `psycopg`, em `erro.orig.diag.constraint_name`. Para indice unico
    (e nao `CONSTRAINT`) o Postgres carimba o nome do INDICE ali, que e o que
    permite `uq_products_branch_slug` — um `CREATE UNIQUE INDEX` — ser
    encontrado na tabela junto com `uq_printing_sectors_branch_name`, que e um
    `UNIQUE` de verdade.

    Os `getattr` sao defesa contra driver sem `diag`, nao contra ausencia do
    nome: sem eles, um erro de integridade de outra origem viraria
    `AttributeError` dentro do tratamento do erro — o pior lugar para uma
    excecao nova aparecer.
    """
    original = getattr(erro, "orig", None)
    diagnostico = getattr(original, "diag", None)
    return getattr(diagnostico, "constraint_name", None)
