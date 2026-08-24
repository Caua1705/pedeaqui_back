"""O interpretador que roda a suite tem que ter as versoes do lock.

O QUE ISTO PEGA, E COMO ELE APARECEU. Em 24/08/2026, um spike sobre streaming
do LangChain foi montado no interpretador local e quase virou conclusao. O
`requirements.lock.txt` fixa `langchain-openai==1.6.0` e `openai==3.3.1` — que
e o que o `Dockerfile` instala e o que roda em producao. O interpretador local
tinha `1.4.1` e `2.52.0`: um salto de MAJOR na biblioteca cuja unica funcao no
projeto e falar com a OpenAI.

A medicao completa do dia: 41 pacotes batendo, **24 divergentes e 3 ausentes**
— entre eles `SQLAlchemy`, `psycopg`, `alembic` e a pilha HTTP inteira
(`httpx2`/`httpcore2`, que a versao do lock do `openai` usa e a versao antiga
nem conhece).

A CAUSA NAO FOI UM VENV QUE ENVELHECEU: **nao havia venv nenhum.** O `README`
manda criar um (`py -m venv venv` e `pip install -r requirements.lock.txt`) e o
`.gitignore` o ignora, mas o diretorio nao existia. Sem ele, `python -m pytest`
cai no Python global da maquina — que tem os pacotes que sobraram de qualquer
outro trabalho — e **nao reclama de nada**. Verde ali nao diz nada sobre
producao.

POR QUE ISTO E UM TESTE, E NAO UM PASSO DO CI. No CI o problema nao existe: o
job instala o lock num runner limpo, entao este arquivo nasce verde la e
qualquer vermelho e local. E e local que o defeito mora — e onde alguem olha um
verde antes de abrir o PR e conclui que a mudanca esta boa.

E o inverso — deixar so o CI cobrar — e o que ja aconteceu com o `pythonpath`:
o problema fica invisivel exatamente para quem esta programando, e so aparece
no fim, quando a explicacao ja custa caro.

`requirements-dev.txt` NAO entra na conferencia, de proposito: ele nao vai para
a imagem. Divergencia em `pytest` muda como o teste roda, nao o que o codigo
faz em producao — e cobrar isso aqui transformaria a trava em barulho.
"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.markers import UndefinedEnvironmentName
from packaging.requirements import Requirement


LOCK = Path(__file__).resolve().parent.parent / "requirements.lock.txt"

COMO_CONSERTAR = (
    "\n\nO ambiente que esta rodando esta suite nao e o que vai para producao.\n"
    "Conserto (na raiz do repositorio):\n"
    "    py -m venv venv\n"
    "    venv\\Scripts\\activate        # Linux/macOS: source venv/bin/activate\n"
    "    pip install -r requirements.lock.txt -r requirements-dev.txt\n"
    "\nSe a intencao era SUBIR uma versao, o caminho e o contrario: mude o\n"
    "`requirements.lock.txt` (e o `requirements.txt`, se o pacote for\n"
    "declarado la) e reinstale. `scripts/check_lockfile.py` cobra o par."
)


def _exigencias_do_lock() -> list[tuple[str, str]]:
    """`(nome, versao)` de cada linha do lock que vale NESTA plataforma.

    O marcador e avaliado e nao ignorado: `uvloop` so instala fora do Windows,
    e cobrar a presenca dele numa maquina Windows seria um vermelho que nao
    corresponde a defeito nenhum. O `check_lockfile.py` descarta o marcador
    porque compara DUAS LISTAS entre si; aqui a comparacao e com o ambiente
    real, e o marcador passa a importar.
    """
    exigencias = []
    for linha in LOCK.read_text(encoding="utf-8").splitlines():
        limpa = linha.strip()
        if not limpa or limpa.startswith("#"):
            continue
        exigencia = Requirement(limpa)
        if exigencia.marker is not None:
            try:
                if not exigencia.marker.evaluate():
                    continue
            except UndefinedEnvironmentName:
                pass
        fixadas = [
            especificador.version
            for especificador in exigencia.specifier
            if especificador.operator == "=="
        ]
        # Linha sem um `==` unico e problema do `check_lockfile.py`, que
        # levanta com a mensagem certa. Aqui ela so nao da o que conferir.
        if len(fixadas) == 1:
            exigencias.append((exigencia.name, fixadas[0]))
    return exigencias


def test_nenhum_pacote_do_lock_esta_ausente():
    """Pacote que falta e o caso mais grave: o codigo importa OUTRA coisa.

    Foi o que aconteceu com `httpx2`/`httpcore2` — a versao do lock do `openai`
    fala com a OpenAI por eles, e a versao antiga instalada localmente usava o
    `httpx` classico. Toda conclusao sobre timeout, pool de conexao ou
    keepalive tirada dali valia para uma biblioteca que producao nao tem.
    """
    ausentes = []
    for nome, esperada in _exigencias_do_lock():
        try:
            version(nome)
        except PackageNotFoundError:
            ausentes.append(f"  {nome}=={esperada}")

    assert not ausentes, (
        f"{len(ausentes)} pacote(s) do lock nao estao instalados:\n"
        + "\n".join(sorted(ausentes))
        + COMO_CONSERTAR
    )


def test_as_versoes_instaladas_sao_as_do_lock():
    """Versao diferente da de producao faz o verde daqui nao valer la.

    A mensagem lista TODAS as divergencias de uma vez, e nao a primeira: quem
    esta com o ambiente errado costuma estar errado em vinte pacotes, e
    descobrir isso um `pip install` por vez e o pior jeito.
    """
    divergentes = []
    for nome, esperada in _exigencias_do_lock():
        try:
            instalada = version(nome)
        except PackageNotFoundError:
            # Coberto pelo teste acima, com mensagem propria.
            continue
        if instalada != esperada:
            divergentes.append(f"  {nome}: lock={esperada} instalado={instalada}")

    assert not divergentes, (
        f"{len(divergentes)} pacote(s) divergem do `requirements.lock.txt`, "
        "que e o que o Dockerfile instala:\n"
        + "\n".join(sorted(divergentes))
        + COMO_CONSERTAR
    )
