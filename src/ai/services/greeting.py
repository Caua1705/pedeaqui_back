"""As mensagens que nao precisam do cardapio, e por que a lista e fechada.

O QUE ISTO RESOLVE. `/chat` chamava a busca vetorial em TODO turno, sem
excecao. "oi" pagava um embedding na OpenAI (mediana de ~400 ms, medido em
24/08/2026) e voltava com um produto anexado — foi assim que uma saudacao foi
respondida com "temos H2O R$ 7,05". O modelo nao buscou quando nao devia: nos
entregamos a agua a ele no prompt, e o prompt manda oferecer o que chegar.

**A REGRA E: NA DUVIDA, BUSCA.** Os dois erros nao custam o mesmo.

- Buscar sem precisar custa ~400 ms e um produto ignorado no prompt.
- NAO buscar quando precisava faz o Rapi dizer "nao temos" sobre um item que
  esta no cardapio — sem erro, sem log, e o cliente vai embora achando que a
  loja nao vende. E o mesmo modo de falha que
  `docs/busca-vetorial-e-indice-ann.md` recusa na secao do ANN.

Dai as tres decisoes abaixo, que nao sao estilo:

**1. A comparacao e com a MENSAGEM INTEIRA, nunca por substring.** "oi, tem
picanha?" comeca com "oi" e precisa do cardapio. Substring transformaria toda
pergunta educada numa saudacao.

**2. NUNCA por tamanho de mensagem.** E a heuristica que parece obvia e e uma
armadilha: "oi" tem 2 caracteres e "tem?" tem 4; "sushi?" tem 6 e "boa noite"
tem 9. Nao existe corte de comprimento que separe os dois grupos, e um corte
errado mata pergunta curta de cardapio — que e o jeito mais comum de
perguntar.

**3. A lista e FECHADA e curta.** Variacao que nao esta aqui ("oii", "bom
diaa", "salve salve") simplesmente cai no lado de buscar. Isso e o desenho
funcionando, nao um buraco: cada entrada nova e ~400 ms a menos num caso e
uma chance a mais de engolir uma pergunta de verdade.

**Despedida ficou de fora de proposito.** "obrigado", "valeu" e "tchau"
tambem dispensam o cardapio e seriam seguros pelo mesmo criterio — mas sao
outra categoria, e misturar as duas numa lista chamada "saudacao" faz a
proxima pessoa nao saber mais qual e o criterio de entrada. Se valer a pena,
entra como lista propria.
"""

import re

from src.utils.normalization import fold_for_match


# So letras, digitos e espaco sobrevivem: a pontuacao das pontas ("oi!",
# "bom dia?") e a do meio ("oi, tudo bem") nao muda o que a mensagem e.
_ONLY_WORDS_RE = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE_RE = re.compile(r"\s+")

GREETINGS = frozenset(
    {
        "oi",
        "ola",
        "opa",
        "eae",
        "eai",
        "e ai",
        "salve",
        "bom dia",
        "boa tarde",
        "boa noite",
        "tudo bem",
        "tudo bom",
        "oi tudo bem",
        "ola tudo bem",
    }
)


def is_greeting(message: str) -> bool:
    """True so quando a mensagem INTEIRA e uma das saudacoes da lista.

    `fold_for_match` achata acento e caixa ("Olá" e "OI" chegam como "ola" e
    "oi"), e o resto some a pontuacao. Qualquer outra coisa devolve False, que
    e o lado de buscar.
    """
    folded = fold_for_match(message)
    words_only = _ONLY_WORDS_RE.sub(" ", folded)
    return _WHITESPACE_RE.sub(" ", words_only).strip() in GREETINGS
