"""Em que formato cada campo de dinheiro sai da API — lido do documento GERADO.

## Por que existe

A armadilha 34 registra que `CreateOrderResponse` entrega `total` como NUMERO
(`52.9`) e `discount_total` como STRING (`"2.50"`), na mesma resposta. Ela
tambem registra por que isso nao foi consertado: **as duas direcoes mudam o
formato de fio**, o app do cliente consome essas respostas, e JSON nao tem
numero com casa decimal fixa — duas casas so existem como string. A decisao e
uma so, sobre a API inteira, tomada junto com o app.

O que faltava era o DADO para tomar essa decisao, e um jeito de saber que a
divida nao cresceu. Este script le o `/openapi.json` que o FastAPI gera — e nao
os `.py` dos schemas — porque e o documento que o front consome, e e nele que
`Decimal` ja apareceu como `string`.

## O achado que muda a conversa

A divisao NAO e aleatoria. Ela e quase inteira por AREA:

    relatorio do painel   string, sempre
    cardapio, cashback,   numero, sempre
    entregador, pedido
    os DESCONTOS do pedido  string, dentro de respostas que sao numero

Ou seja, o problema nao e "metade da API de cada jeito". E que **um punhado de
respostas mistura os dois no MESMO objeto** — e sao essas que fazem
`total + discount_total` levantar `TypeError` para quem escrever o primeiro
relatorio sobre elas.

`ESQUEMAS_QUE_MISTURAM` e a lista de hoje. O teste cobra que ela nao cresca:
converter um schema isolado ja foi tentado e revertido (`bffca0e`), e o que
nao existia era alguem cobrando o contrario — que e um schema NOVO nascer
misturado.

## O criterio de "campo de dinheiro", e por que `integer` fica de fora

Nome que casa com um termo de dinheiro E tipo `number` ou `string`. O tipo e o
que separa o dinheiro da CONTAGEM: `AdminProductListResponse.total` e
`ProductSalesItem.quantity_total` casam pelo nome e sao `integer`, porque
dinheiro neste projeto nunca e inteiro. Sem esse corte, metade do resultado
seria contador.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


# Termos que nomeiam dinheiro nos schemas deste projeto.
TERMOS_DE_DINHEIRO = (
    "subtotal",
    "delivery_fee",
    "service_fee",
    "total",
    "amount",
    "price",
    "fee",
    "balance",
    "commission",
    "revenue",
)

# Os schemas que hoje entregam os DOIS formatos no mesmo objeto.
#
# Nao e uma lista de tarefas: e o estado congelado, para o teste cobrar que ele
# nao cresca. A conversao dos tres acontece junto com a decisao sobre a API
# inteira — meia API de cada jeito e pior que qualquer uma das duas, e um
# schema convertido isolado ja foi revertido uma vez por isso.
ESQUEMAS_QUE_MISTURAM = frozenset(
    {
        "CreateOrderResponse",
        "CustomerOrderHistoryItem",
        "OrderDetailResponse",
    }
)


def _tipos(prop: dict) -> set[str]:
    """Os tipos possiveis do campo, achatando `anyOf` (que e o `| None`)."""
    if "type" in prop:
        return {prop["type"]}
    tipos = set()
    for chave in ("anyOf", "oneOf", "allOf"):
        for alternativa in prop.get(chave, []):
            if "type" in alternativa:
                tipos.add(alternativa["type"])
    return tipos


def _e_dinheiro(campo: str, tipos: set[str]) -> bool:
    if not any(termo in campo for termo in TERMOS_DE_DINHEIRO):
        return False
    # `integer` e contagem, `boolean` e chave liga/desliga (`service_fee_enabled`).
    return bool(tipos & {"number", "string"})


def formatos_por_esquema(spec: dict) -> dict[str, dict[str, set[str]]]:
    """Por schema, quais campos de dinheiro saem como numero e quais como string."""
    achados: dict[str, dict[str, set[str]]] = {}
    for nome, esquema in (spec.get("components", {}).get("schemas") or {}).items():
        numero, string = set(), set()
        for campo, prop in (esquema.get("properties") or {}).items():
            tipos = _tipos(prop)
            if not _e_dinheiro(campo, tipos):
                continue
            if "number" in tipos:
                numero.add(campo)
            elif "string" in tipos:
                string.add(campo)
        if numero or string:
            achados[nome] = {"numero": numero, "string": string}
    return achados


def esquemas_que_misturam(spec: dict) -> set[str]:
    """Os schemas que entregam os DOIS formatos no mesmo objeto.

    Sao os unicos em que a inconsistencia e visivel de dentro de uma resposta
    so — e os unicos em que `total + discount_total` e `TypeError`.
    """
    return {
        nome
        for nome, formatos in formatos_por_esquema(spec).items()
        if formatos["numero"] and formatos["string"]
    }


def main() -> int:
    from main import app

    spec = app.openapi()
    achados = formatos_por_esquema(spec)
    numero = sum(len(f["numero"]) for f in achados.values())
    string = sum(len(f["string"]) for f in achados.values())
    misturam = sorted(esquemas_que_misturam(spec))

    print(f"{numero} campo(s) de dinheiro como NUMERO, {string} como STRING")
    print(f"\n{len(misturam)} schema(s) misturam os dois no mesmo objeto:")
    for nome in misturam:
        formatos = achados[nome]
        print(f"  {nome}")
        print(f"    numero: {', '.join(sorted(formatos['numero']))}")
        print(f"    string: {', '.join(sorted(formatos['string']))}")

    novos = sorted(set(misturam) - ESQUEMAS_QUE_MISTURAM)
    if novos:
        print(f"\nNOVOS desde o congelamento: {novos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
