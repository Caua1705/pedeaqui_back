"""Onde ficam os arquivos em disco, congelado ou nao.

**Este modulo existe por causa de um unico defeito, e ele e silencioso.**

Quando o agente e empacotado com PyInstaller em arquivo unico, o executavel
se descompacta numa pasta temporaria (`sys._MEIPASS`, algo como
`C:\\Users\\...\\AppData\\Local\\Temp\\_MEI123456\\`) e roda de la. Nessa
situacao `__file__` aponta para dentro dessa pasta — que o Windows APAGA
quando o processo termina.

O resultado, se algum caminho sair de `__file__`:

- o `config.ini` procurado nunca e o que o instalador copiou;
- o log e gravado na pasta temporaria e evapora junto com ela;
- `printed-orders.json` some a cada fechamento, e o agente reimprime a fila
  inteira do dia na proxima vez que abrir.

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

import sys
from pathlib import Path


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
