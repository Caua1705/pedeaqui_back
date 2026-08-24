"""Recusa que `requirements.txt` e `requirements.lock.txt` divirjam.

Duas listas para a mesma coisa e sempre um convite a envelhecerem separadas, e
aqui o envelhecimento tem dois sintomas caros e nenhum aviso:

- **dependencia declarada e fora do lock:** o `Dockerfile` instala so o lock,
  entao o pacote nao entra na imagem. O erro aparece como
  `ModuleNotFoundError` no boot do container, em producao, depois do deploy;
- **pino do `requirements.txt` que o lock nao satisfaz:** e o starlette de
  23/08/2026 de novo, so que agora com dois arquivos discordando por escrito.

O caminho contrario NAO e erro: o lock tem dezenas de transitivas que ninguem
declara, e exigir que cada uma aparecesse no `requirements.txt` transformaria a
lista do que o projeto usa numa segunda copia do lock.

Roda no CI. Sem argumento nenhum: os dois arquivos ficam na raiz.
"""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


RAIZ = Path(__file__).resolve().parent.parent
DECLARADAS = RAIZ / "requirements.txt"
LOCK = RAIZ / "requirements.lock.txt"


def linhas_uteis(caminho: Path) -> list[str]:
    linhas = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        limpa = linha.strip()
        if limpa and not limpa.startswith("#"):
            linhas.append(limpa)
    return linhas


def ler_lock(caminho: Path) -> dict[str, str]:
    """`nome -> versao`. O marcador de plataforma, quando houver, e descartado.

    Ele nao entra na comparacao de proposito: `uvloop` so instala fora do
    Windows, mas a versao dele e uma so nos dois lugares — o marcador diz ONDE
    instalar, nao O QUE.
    """
    versoes = {}
    for linha in linhas_uteis(caminho):
        exigencia = Requirement(linha)
        fixadas = [
            especificador.version
            for especificador in exigencia.specifier
            if especificador.operator == "=="
        ]
        if len(fixadas) != 1:
            raise SystemExit(
                f"{caminho.name}: linha sem um `==` unico: {linha!r}. "
                "O lock e saida de `pip freeze` — nao edite a mao."
            )
        versoes[canonicalize_name(exigencia.name)] = fixadas[0]
    return versoes


def conferir(declaradas: list[str], no_lock: dict[str, str]) -> list[str]:
    problemas = []
    for linha in declaradas:
        exigencia = Requirement(linha)
        nome = canonicalize_name(exigencia.name)
        if nome not in no_lock:
            problemas.append(
                f"`{exigencia.name}` esta em requirements.txt e nao esta no lock. "
                "Regere o lock a partir da producao antes de subir."
            )
            continue
        versao = no_lock[nome]
        if exigencia.specifier and not exigencia.specifier.contains(Version(versao), prereleases=True):
            problemas.append(
                f"`{exigencia.name}`: requirements.txt pede `{exigencia.specifier}` "
                f"e o lock tem `{versao}`."
            )
    return problemas


def main() -> int:
    problemas = conferir(linhas_uteis(DECLARADAS), ler_lock(LOCK))
    if not problemas:
        print("requirements.lock.txt cobre tudo que o requirements.txt declara.")
        return 0
    for problema in problemas:
        print(problema)
    return 1


if __name__ == "__main__":
    sys.exit(main())
