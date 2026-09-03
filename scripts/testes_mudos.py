"""Teste que existe no arquivo e nao roda — o portao verde porque nada rodou.

Em 02/09/2026 um arquivo novo nasceu com `class OPainelEscreveTests:` — sem
prefixo `Test` e sem herdar `unittest.TestCase`. O pytest coletou **zero**
casos dela: nao falhou, nao avisou, nao apareceu em lugar nenhum. Nove testes
escritos, revisados e commitados que nunca rodaram uma vez.

E a mesma familia do dublê de dado e da descoberta vazia de
`tests/rotas_do_app.py`: **o portao fica verde porque nada rodou**, e verde por
ausencia e indistinguivel de verde por acerto.

## Por que ele PERGUNTA ao pytest em vez de reimplementar as regras

A primeira tentativa foi so AST — "classe com metodo `test_*` que nao comeca
com `Test` e nao herda `TestCase`". Ela acusou **sete** classes, e as sete
estavam sendo coletadas: `DefaultDeliveryFeeFallbackTests(DeliveryEstimateTests)`
herda `TestCase` de AVO, e nenhum AST razoavel resolve isso sem virar um
interpretador de heranca.

As regras de verdade sao do pytest e mudam com a versao e com o `pytest.ini`
(`python_files`, `python_classes`, `python_functions`, `norecursedirs`,
`__init__` numa classe `Test*`, `@staticmethod`, marcadores). Reimplementa-las
seria manter uma segunda copia de uma regra de terceiro — e a copia estaria
errada exatamente nos casos raros, que sao os que passam despercebidos.

Entao a divisao e:

- **quem diz o que E coletado**: o proprio pytest, via `--collect-only`;
- **quem diz o que PARECE teste**: o AST, por um criterio grosso e explicito —
  alguem escreveu um metodo ou uma funcao chamada `test*`.

O achado e a diferenca. Nao ha regra de coleta reimplementada aqui.

## O que ele acusa

1. **teste nao coletado** — existe `def test_*` no arquivo e o pytest nao o
   listou;
2. **arquivo mudo** — arquivo com nome de teste e NADA que pareca teste
   dentro. Suite com arquivo mudo e pior que suite sem o arquivo: o segundo se
   nota. (Um arquivo que coleta zero porque a unica classe dele nao e coletada
   sai pelo item 1, nomeando os metodos — dizer "coleta zero" quando da para
   dizer "estes dois nao rodam" e trocar um achado acionavel por um aviso.);
3. **arquivo fora do padrao de nome** — contem `test_*` e o nome nao casa com
   `python_files`, entao nada dele roda nunca.

    python scripts/testes_mudos.py
    python scripts/testes_mudos.py --raiz print-agent

**A `--raiz` nao e enfeite.** Este repositorio tem DUAS suites com ciclos de
vida diferentes, e cada uma tem o seu `pytest.ini`. Rodando a coleta da raiz
da API, o `norecursedirs = print-agent` faz o pytest devolver zero — e este
script leria os 9 arquivos do agente como "mudos". Foi o que aconteceu na
primeira execucao, e nao era achado: era a ferramenta sendo chamada errada.
Cada suite e auditada a partir da raiz DELA.

Somente leitura: le codigo-fonte e roda a COLETA do pytest, que nao executa
teste nenhum. Nao abre banco.
"""

import argparse
import ast
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

# O prefixo de `python_functions` do pytest. E o unico pedaco de configuracao
# que este script conhece, e ele e o criterio do lado do AST — "alguem escreveu
# um teste aqui" —, nao uma regra de coleta.
PREFIXO_DE_TESTE = "test"

# `python_files` na configuracao padrao. Usado so para dizer se um arquivo com
# testes dentro tem nome que o pytest procura.
PADROES_DE_ARQUIVO = ("test_*.py", "*_test.py")


class _Candidato:
    """Uma coisa que PARECE teste, com o endereco que o pytest usaria."""

    def __init__(self, arquivo: Path, classes: tuple[str, ...], nome: str, linha: int):
        self.arquivo = arquivo
        self.classes = classes
        self.nome = nome
        self.linha = linha

    @property
    def endereco(self) -> tuple:
        return (self.arquivo, self.classes, self.nome)

    def __str__(self) -> str:
        caminho = self.arquivo.as_posix()
        dentro = "::".join((*self.classes, self.nome))
        return f"{caminho}::{dentro}  (linha {self.linha})"


def _candidatos_do_arquivo(arquivo: Path, raiz: Path) -> list[_Candidato]:
    """Todo `def test*` do arquivo, com a pilha de classes em volta."""
    achados: list[_Candidato] = []
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
    relativo = arquivo.relative_to(raiz)

    def descer(corpo, classes: tuple[str, ...]) -> None:
        for no in corpo:
            if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if no.name.startswith(PREFIXO_DE_TESTE):
                    achados.append(_Candidato(relativo, classes, no.name, no.lineno))
            elif isinstance(no, ast.ClassDef):
                # Recursivo porque o pytest tambem desce em classe aninhada, e
                # o endereco dela leva as duas (`Fora::Dentro::test_x`).
                descer(no.body, (*classes, no.name))

    descer(arvore.body, ())
    return achados


def _coletados(raiz: Path, alvo: str) -> tuple[set, set]:
    """O que o pytest REALMENTE coleta: (enderecos, arquivos com pelo menos um).

    `--collect-only` nao executa teste nenhum — ele importa os modulos e lista.

    `cwd=raiz` importa: o pytest resolve `rootdir` e o `.ini` a partir dali, e
    e o `.ini` que define `python_files`, `python_classes` e `norecursedirs`.
    Auditar uma suite de fora da raiz dela le a configuracao de OUTRO projeto.
    """
    resultado = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(raiz / alvo),
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        cwd=raiz,
        capture_output=True,
        text=True,
    )
    enderecos = set()
    arquivos = set()
    for linha in resultado.stdout.splitlines():
        linha = linha.strip()
        if "::" not in linha or linha.startswith(("=", "ERROR", "E ")):
            continue
        caminho, _, resto = linha.partition("::")
        if not caminho.endswith(".py"):
            continue
        partes = resto.split("::")
        # `test_x[param-1]` e `test_x` sao o mesmo `def`: o parametro nao
        # interessa para a pergunta "isto rodou?".
        partes[-1] = partes[-1].split("[")[0]
        arquivo = Path(caminho)
        enderecos.add((arquivo, tuple(partes[:-1]), partes[-1]))
        arquivos.add(arquivo)
    return enderecos, arquivos


def _tem_nome_de_teste(arquivo: Path) -> bool:
    return any(arquivo.match(padrao) for padrao in PADROES_DE_ARQUIVO)


def auditar(raiz: Path | None = None, alvo: str = "tests") -> dict[str, list]:
    """`raiz` existe para o teste montar uma arvore PLANTADA.

    Sem ela nao ha como provar que este varredor acusa o que deve acusar — e
    varredor visto so respondendo "nenhum" nao provou nada. Mesmo motivo do
    `diretorios` de `_Indice` e do `raiz` de `escrita_e_transacao`.
    """
    raiz = raiz or ROOT_DIR
    enderecos, arquivos_com_algo = _coletados(raiz, alvo)

    nao_coletados: list[str] = []
    fora_do_padrao: list[str] = []
    mudos: list[str] = []

    for arquivo in sorted((raiz / alvo).rglob("*.py")):
        if "__pycache__" in arquivo.parts:
            continue
        relativo = arquivo.relative_to(raiz)
        candidatos = _candidatos_do_arquivo(arquivo, raiz)

        if not _tem_nome_de_teste(arquivo):
            # Sem nome de teste, o pytest nem abre o arquivo. Um helper
            # (`fabricas.py`, `rotas_do_app.py`) esta certo assim — so vira
            # achado se tiver `def test*` dentro.
            if candidatos:
                fora_do_padrao.append(
                    f"{relativo.as_posix()}  ({len(candidatos)} teste(s), nome nao casa com "
                    f"{' nem '.join(PADROES_DE_ARQUIVO)})"
                )
            continue

        if candidatos:
            # Ha teste escrito no arquivo: o achado NOMEIA quais nao rodam.
            # Dizer so "o arquivo coleta zero" quando da para dizer "estes
            # dois metodos nao rodam" e trocar um achado acionavel por um
            # aviso — e num arquivo mudo cuja unica classe nao e coletada, os
            # dois fatos sao o mesmo fato.
            for candidato in candidatos:
                if candidato.endereco not in enderecos:
                    nao_coletados.append(str(candidato))
            continue

        if relativo not in arquivos_com_algo:
            # Nome de teste e NADA que pareca teste dentro. E um caso
            # diferente do de cima: nao ha o que consertar num metodo, o
            # arquivo inteiro e que nao tem razao de existir com esse nome.
            mudos.append(f"{relativo.as_posix()}  (nenhum `def {PREFIXO_DE_TESTE}*` dentro)")

    return {
        "nao_coletados": nao_coletados,
        "mudos": mudos,
        "fora_do_padrao": fora_do_padrao,
    }


TITULOS = {
    "nao_coletados": "Teste que existe no arquivo e o pytest NAO coleta",
    "mudos": "Arquivo com nome de teste do qual o pytest coleta ZERO casos",
    "fora_do_padrao": "Arquivo com testes dentro e nome que o pytest nao procura",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acha teste escrito que nao roda (somente leitura)."
    )
    parser.add_argument(
        "--raiz",
        default=".",
        help="a raiz da suite, onde mora o pytest.ini dela (ex.: print-agent)",
    )
    parser.add_argument("--alvo", default="tests", help="o diretorio de testes dentro da raiz")
    args = parser.parse_args()

    raiz = (ROOT_DIR / args.raiz).resolve()
    encontrados = auditar(raiz=raiz, alvo=args.alvo)
    total = sum(len(grupo) for grupo in encontrados.values())

    print("=" * 78)
    print(f"{total} achado(s)")
    print("=" * 78)

    for chave, titulo in TITULOS.items():
        grupo = encontrados[chave]
        print()
        print(f"## {titulo}  ({len(grupo)})")
        print()
        if not grupo:
            print("  Nenhum.")
        for item in grupo:
            print(f"  {item}")

    print()
    print("Quem decide o que e coletado e o pytest, via `--collect-only`; o AST")
    print("so diz o que PARECE teste. Nenhuma regra de coleta foi reimplementada")
    print("aqui — a primeira versao tentou, e acusou sete classes corretas.")
    print()
    print(f"Raiz auditada: {raiz.name}/{args.alvo}. Cada suite tem que ser auditada")
    print("a partir da raiz DELA, senao o `.ini` lido e o do projeto errado.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
