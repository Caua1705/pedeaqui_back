"""Log em arquivo, rotacionado.

Em arquivo porque o agente roda como servico do Windows: nao ha console para
onde olhar, e quando alguem pergunta "por que a comanda nao saiu ontem as
21h" a unica resposta possivel esta neste arquivo. O console continua
recebendo tudo para quem estiver rodando a mao.

Rotacionado porque o agente roda por meses sem ninguem tocar nele, e um log
de uma linha por evento em um restaurante movimentado enche o disco da
maquina do balcao — que costuma ser a mais apertada da loja.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_file: Path,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Limpa handlers de uma configuracao anterior: sem isso, chamar duas
    # vezes (teste, reinicio interno) duplicaria cada linha do log.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # O `urllib3` loga uma linha por reconexao do stream — e sao dezenas por
    # noite, todas previstas. Em WARNING ele so aparece quando importa.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
