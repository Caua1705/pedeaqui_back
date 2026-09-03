"""Acha teste que dubla DADO com objeto de atributos livres.

O CLAUDE.md proibe dublar schema ou model deste repositorio com
`SimpleNamespace`, e a razao esta escrita la: **um objeto de atributos livres
nao tem o contrato que o teste diz estar verificando.** Ele responde qualquer
atributo que o teste escrever e nenhum que o teste esquecer, e o resultado e
um teste verde descrevendo um objeto que a aplicacao nunca produz.

O caso que fechou a porta: `serves_people` entrou em `products`, no
`AdminProductResponse` e na voz, e NAO no `ProductResponse` — que e o que a
hidratacao entrega a ferramenta de voz. Os testes rapidos montavam o produto
com `SimpleNamespace(..., serves_people=None)`; o atributo existia porque o
teste o escreveu. Suite verde, e `buscar_no_cardapio` levantando
`AttributeError` em toda busca falada em producao.

## Como ele decide

Para cada `SimpleNamespace(...)` da suite, compara as CHAVES passadas com:

- as colunas de cada tabela do `Base.metadata`, mais os `relationship` de cada
  model (`option_groups` e atributo legitimo de `Product` e nao e coluna);
- os campos de todo `BaseModel` de `src/` — inclusive `src/ai/schemas/`;
- os campos de toda `dataclass` de `src/` — e onde moram `PaymentIntent`,
  `PaymentWebhookEvent` e `DeliveryEstimateResult`.

Casando `LIMIAR` ou mais das chaves com algum desses tipos, e um dublê de
DADO: existe um tipo de verdade com aquele contrato, e e ele que o teste
deveria construir.

**`SimpleNamespace` continua certo para dublar COLABORADOR** — um repositorio,
um servico, um cliente HTTP, o `db`. Nesses nao ha contrato nosso a respeitar,
e por isso a suite continua cheia deles. A regra e sobre dado.

## Por que 0.6 e nao 1.0

Dublê costuma ser PARCIAL: o teste escreve so os campos que a funcao sob teste
le. Exigir casamento total nao acharia nenhum. E o inverso — exigir pouco —
acusaria `SimpleNamespace(id=..., name=...)` de qualquer coisa. Em 0.6, a
suite deste repositorio saiu de 141 achados para 0 sem nenhum falso positivo
sobrando.

Somente leitura, e nao precisa de banco:

    python scripts/dubles_de_dado.py
    python scripts/dubles_de_dado.py --limiar 0.5    # mais sensivel

Quem o roda a cada commit e `tests/test_dubles_de_dado.py`.
"""

import argparse
import ast
import dataclasses
import importlib
import pkgutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pydantic import BaseModel

import src
import src.models  # noqa: F401
from src.db.base import Base


LIMIAR_PADRAO = 0.6

# Abaixo disto, o casamento nao diz nada: `SimpleNamespace(id=..., x=...)` casa
# com meio repositorio por acidente.
MINIMO_DE_CHAVES = 2


def tipos_do_repositorio() -> dict[str, set[str]]:
    """Nome legivel -> conjunto de atributos, para todo tipo de dado do `src/`."""
    # `walk_packages` importa tudo: um schema em modulo que ninguem importou
    # ainda nao existiria para o `__subclasses__`, e seria justamente o que
    # falta conferir.
    for info in pkgutil.walk_packages(src.__path__, prefix="src."):
        try:
            importlib.import_module(info.name)
        except Exception:
            # Modulo que nao importa sozinho nao impede a varredura do resto.
            # Ele so nao contribui com tipos.
            continue

    tipos: dict[str, set[str]] = {}

    for tabela in Base.metadata.tables.values():
        tipos[f"model:{tabela.name}"] = {coluna.name for coluna in tabela.columns}
    for nome in dir(src.models):
        classe = getattr(src.models, nome)
        if isinstance(classe, type) and hasattr(classe, "__tablename__"):
            chave = f"model:{classe.__tablename__}"
            if chave in tipos:
                tipos[chave] |= {a for a in vars(classe) if not a.startswith("_")}

    vistos: set[type] = set()
    for nome_do_modulo, modulo in list(sys.modules.items()):
        if not nome_do_modulo.startswith("src.") or modulo is None:
            continue
        for nome in dir(modulo):
            objeto = getattr(modulo, nome, None)
            if not isinstance(objeto, type) or objeto in vistos:
                continue
            if issubclass(objeto, BaseModel) and objeto is not BaseModel:
                vistos.add(objeto)
                tipos[f"schema:{objeto.__name__}"] = set(objeto.model_fields)
            elif dataclasses.is_dataclass(objeto):
                vistos.add(objeto)
                campos = {campo.name for campo in dataclasses.fields(objeto)}
                if campos:
                    tipos[f"dataclass:{objeto.__name__}"] = campos
    return tipos


def achados(diretorio: Path, tipos: dict[str, set[str]], limiar: float):
    resultado = []
    for arquivo in sorted(diretorio.glob("test_*.py")):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            alvo = no.func
            nome = alvo.id if isinstance(alvo, ast.Name) else getattr(alvo, "attr", None)
            if nome != "SimpleNamespace":
                continue
            chaves = {palavra.arg for palavra in no.keywords if palavra.arg}
            if len(chaves) < MINIMO_DE_CHAVES:
                continue
            melhor, pontuacao = None, 0.0
            for nome_do_tipo, campos in tipos.items():
                comuns = chaves & campos
                if not comuns:
                    continue
                atual = len(comuns) / len(chaves)
                # Empate vai para o tipo MENOR: entre uma tabela de 48 colunas
                # e um schema de 4 que casem igual, o schema descreve melhor o
                # que o teste esta dublando.
                if atual > pontuacao or (atual == pontuacao and melhor and len(campos) < len(tipos[melhor])):
                    melhor, pontuacao = nome_do_tipo, atual
            if pontuacao >= limiar:
                # `relative_to` so vale para arquivo DENTRO do repositorio; a
                # varredura tambem roda sobre um diretorio temporario, no
                # teste que planta um dublê para provar que o vermelho existe.
                try:
                    caminho = str(arquivo.relative_to(ROOT_DIR))
                except ValueError:
                    caminho = str(arquivo)
                resultado.append(
                    (
                        caminho.replace("\\", "/"),
                        no.lineno,
                        melhor,
                        round(pontuacao, 2),
                        sorted(chaves),
                    )
                )
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acha SimpleNamespace que dubla model, schema ou dataclass do src/."
    )
    parser.add_argument("--limiar", type=float, default=LIMIAR_PADRAO)
    parser.add_argument("--testes", default="tests", help="Diretorio a varrer (padrao: tests).")
    args = parser.parse_args()

    encontrados = achados(ROOT_DIR / args.testes, tipos_do_repositorio(), args.limiar)

    if not encontrados:
        print("Nenhum dublê de dado. Todo SimpleNamespace da suite dubla colaborador.")
        return 0

    print(f"{len(encontrados)} dublê(s) de dado — construa o tipo real no lugar:")
    print()
    for arquivo, linha, tipo, pontuacao, chaves in encontrados:
        print(f"  {arquivo}:{linha}")
        print(f"      parece {tipo}  ({pontuacao:.0%} das chaves)")
        print(f"      chaves: {', '.join(chaves)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
