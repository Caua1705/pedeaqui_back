"""Onde ficam os arquivos em disco, congelado ou nao.

**Este modulo existe por causa de um unico defeito, e ele e silencioso.**

Empacotado com PyInstaller, `__file__` NAO aponta para a pasta onde o
lojista tem os arquivos dele. No `--onedir` de hoje (ver build.bat) ele
aponta para dentro da `_internal`, ao lado do executavel; no `--onefile`
que o projeto usava antes, apontava para uma pasta temporaria
(`sys._MEIPASS`, algo como `C:\\Users\\...\\Temp\\_MEI123456\\`) que o
Windows APAGA quando o processo termina.

O resultado, se algum caminho sair de `__file__`:

- o `config.ini` procurado nunca e o que o instalador copiou;
- o log e gravado onde ninguem vai procurar (e, no `--onefile`, evaporava
  junto com a pasta temporaria);
- `pedidos-impressos.json` idem — e no `--onefile` sumia a cada fechamento,
  fazendo o agente reimprimir a fila inteira do dia ao abrir de novo.

Os tres so aparecem DEPOIS de empacotar. Rodando `python -m print_agent` na
maquina de quem desenvolve, tudo funciona.

Por isso todo caminho em disco sai de `BASE_DIR`, e `BASE_DIR` e:

- **congelado**: a pasta do `.exe` (`sys.executable`), que e onde o
  instalador poe o `config.ini` e onde o lojista consegue achar o log;
- **normal**: a pasta `print-agent/`, que e onde o `config.ini.example`
  sempre esteve.

`sys._MEIPASS` NAO e usado aqui de proposito: ele serve para ler recurso
EMBUTIDO no executavel (somente leitura), nunca para gravar nem para achar
configuracao do usuario.
"""

import logging
import os
import sys
from pathlib import Path


logger = logging.getLogger(__name__)


def is_frozen() -> bool:
    """True quando rodando a partir do executavel do PyInstaller."""
    return getattr(sys, "frozen", False)


def base_dir() -> Path:
    """A pasta de onde saem config.ini, o log e o estado.

    Funcao e nao constante de modulo para os testes conseguirem exercitar os
    dois modos sem recarregar o pacote.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # print_agent/paths.py -> print_agent/ -> print-agent/
    return Path(__file__).resolve().parent.parent


# Conveniencia para quem so precisa do valor uma vez, no boot.
BASE_DIR = base_dir()

# Nomes fixos, ao lado do executavel. Sem subpasta de proposito: o lojista
# vai ser instruido por telefone a "abrir a pasta e mandar o arquivo que
# termina em .log", e uma pasta a mais e um passo a mais para errar.
CONFIG_NAME = "config.ini"
LOG_NAME = "rapidex-impressao.log"
STATE_NAME = "pedidos-impressos.json"


def config_path() -> Path:
    return base_dir() / CONFIG_NAME


def default_log_path() -> Path:
    return base_dir() / LOG_NAME


def default_state_path() -> Path:
    return base_dir() / STATE_NAME


def open_folder(folder: Path) -> None:
    """Abre a pasta no Explorer do Windows.

    E o que os dois primeiros itens do menu da bandeja fazem, e e a razao de
    eles existirem: sem console, "me manda o rapidex-impressao.log" e "abre o
    config.ini no Bloco de Notas" viravam instrucoes de digitar um caminho
    com `%LOCALAPPDATA%` no meio, por telefone.

    Abre a PASTA e nao o arquivo: o Explorer sempre sabe abrir uma pasta,
    enquanto `.log` costuma nao ter programa associado nessas maquinas — e ai
    o Windows responde com o dialogo de "como voce quer abrir este arquivo?",
    que e pior que nao ter feito nada.
    """
    try:
        os.startfile(folder)
    except Exception as exc:
        # Pasta apagada a mao, unidade de rede fora do ar, ou nao-Windows.
        # Nunca pode derrubar o agente: isto e conveniencia de menu, e a
        # impressao nao depende disso.
        logger.warning("nao foi possivel abrir a pasta %s: %s", folder, exc)
