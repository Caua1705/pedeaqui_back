"""Exporta o /openapi.json a partir do app, sem servidor de pe.

## Por que o arquivo e versionado

O painel gera o cliente dele a partir do documento (`api:generate`), e ate
agora o fazia apontando para a API em producao. Isso deixou de funcionar: o
`/openapi.json` **nao e servido em producao**, de proposito — publicar o
documento inteiro entrega a superficie de ataque de graca, incluindo rotas
de lojista que ninguem de fora precisa conhecer.

O painel entao passa a consumir o arquivo COMMITADO aqui. Isso troca uma
dependencia de rede (que nao existe mais) por uma dependencia de disciplina:
o arquivo tem que acompanhar o codigo. E o que o `--check` do CI garante.

## Por que a saida e deterministica

Um export que muda de bytes sem o contrato ter mudado transforma o passo do
CI em ruido, e passo de CI que falha a toa e passo que as pessoas aprendem a
ignorar. Tres coisas garantem isso:

- `sort_keys=True`: nao dependemos da ordem em que o FastAPI monta os dicts,
  que muda com a ordem de declaracao das rotas — um `include_router` movido
  de lugar geraria um diff enorme sem nenhuma mudanca de contrato;
- `ensure_ascii=False`: acento sai como acento. O documento tem descricao em
  portugues, e `ç` a cada `ç` e ilegivel na revisao do diff;
- fim de linha LF explicito. O repositorio e editado no Windows com
  `core.autocrlf=true`; sem isto, o arquivo escrito aqui teria CRLF, o CI
  (Linux) geraria LF, e o `--check` acusaria diferenca em TODA linha sem
  nenhuma mudanca real. O `.gitattributes` fixa o outro lado.

## Uso

    python scripts/export_openapi.py            # regrava openapi.json
    python scripts/export_openapi.py --check    # so confere; 1 se desatualizado
"""

import argparse
import json
import sys
from pathlib import Path


# A raiz do repositorio, a partir deste arquivo: o script precisa funcionar
# chamado de qualquer diretorio, inclusive de dentro do container.
REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO_ROOT / "openapi.json"


def render() -> str:
    """O documento como ele deve estar em disco.

    O import mora aqui dentro, e nao no topo, porque importar `main` sobe a
    aplicacao inteira — e com ela o `src.core.config`, que derruba o
    processo com ValidationError se faltar variavel de ambiente. Com o
    import no topo, `--help` tambem morreria, e a mensagem de erro nao teria
    nada a ver com o que a pessoa pediu.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from main import app

    document = app.openapi()
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def write() -> int:
    # `open` em vez de `Path.write_text`/`read_text` nos dois lados: o
    # parametro `newline` so existe no `read_text` a partir do 3.13, e o
    # repositorio roda 3.12. Sem ele, a leitura traduziria CRLF para LF e o
    # `--check` passaria sobre um arquivo que quebraria no Linux.
    with open(OPENAPI_PATH, "w", encoding="utf-8", newline="\n") as arquivo:
        arquivo.write(render())
    print(f"openapi.json gravado ({OPENAPI_PATH})")
    return 0


def check() -> int:
    """Confere sem gravar. Devolve 1 quando o arquivo esta desatualizado."""
    esperado = render()

    if not OPENAPI_PATH.exists():
        print("ERRO: openapi.json nao existe.", file=sys.stderr)
        print("Rode: python scripts/export_openapi.py", file=sys.stderr)
        return 1

    # `newline=""` desliga a traducao de fim de linha na LEITURA: sem isso,
    # um arquivo com CRLF seria lido como LF e passaria no check, para
    # depois quebrar o `api:generate` de quem clonou no Linux.
    with open(OPENAPI_PATH, encoding="utf-8", newline="") as arquivo:
        atual = arquivo.read()

    if atual == esperado:
        print("openapi.json esta em dia com o codigo.")
        return 0

    print("ERRO: openapi.json esta desatualizado.", file=sys.stderr)
    print(file=sys.stderr)
    # Sem travessao nem acento nas mensagens de console: o console do Windows
    # abre na codepage do sistema (850/1252 no Brasil) e um caractere de fora
    # dela sai como "?" ou derruba o print (armadilha 29). Quem le esta
    # mensagem esta justamente numa maquina Windows, no meio de um commit.
    print("O painel gera o cliente dele a partir deste arquivo, e ele nao e", file=sys.stderr)
    print("servido em producao. Se ele nao acompanhar o codigo, o painel", file=sys.stderr)
    print("passa a chamar um contrato que nao existe mais.", file=sys.stderr)
    print(file=sys.stderr)
    print("Rode e commite o resultado:", file=sys.stderr)
    print("    python scripts/export_openapi.py", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="nao grava; sai com 1 se o arquivo nao bate com o codigo",
    )
    args = parser.parse_args()

    return check() if args.check else write()


if __name__ == "__main__":
    raise SystemExit(main())
