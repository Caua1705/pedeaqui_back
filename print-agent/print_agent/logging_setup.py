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
import os
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

    # O arquivo e UTF-8 e aguenta qualquer coisa; o console do Windows nao.
    # Ele abre na codepage do sistema (850/1252 no Brasil), e um caractere de
    # fora dela — o travessao que o lojista digitou em "Praça Quente — Térreo",
    # um emoji no nome do item — faz o `emit` levantar UnicodeEncodeError. O
    # logging engole a excecao, mas cospe "--- Logging error ---" com traceback
    # na tela E PERDE A LINHA. No dia da instalacao e justamente esta janela
    # que alguem esta olhando para saber se conectou.
    #
    # `errors="replace"` troca o caractere impossivel por "?" e entrega a linha.
    console = logging.StreamHandler(_console_stream())
    console.setFormatter(formatter)
    root.addHandler(console)

    # O `urllib3` loga uma linha por reconexao do stream — e sao dezenas por
    # noite, todas previstas. Em WARNING ele so aparece quando importa.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def harden_console() -> None:
    """Deixa `stdout` e `stderr` aceitarem acento sem derrubar a linha.

    Chamada no comeco do `__main__`, antes de qualquer `print` — e nao so
    dentro do `setup_logging` — porque as mensagens que mais precisam sair
    sao as que acontecem ANTES de existir log: o caminho do config ("Pasta do
    programa: C:\\Users\\João\\..."), e a mensagem de config invalido, que e
    a unica coisa na tela quando o programa se recusa a subir.

    Um `UnicodeEncodeError` ali nao vira mensagem de erro legivel: vira
    traceback de Python numa janela preta, que e exatamente o que o
    tratamento de config existe para evitar.

    Desde o build `--noconsole` ela tambem cobre o caso oposto: **nao existe
    console nenhum**, e `sys.stdout` e `sys.stderr` sao `None`. Ai o primeiro
    `print` levanta `AttributeError: 'NoneType' object has no attribute
    'write'` e o programa morre antes de existir log, sem janela e sem nada
    na tela — o pior desfecho possivel, porque nao sobra rastro de que ele
    chegou a abrir.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            # So `errors`: a codepage do console fica como esta, senao o que
            # hoje aparece certo passaria a aparecer errado.
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # stdout que nao e TextIOWrapper (teste, pipe de servico) ou que
            # nem existe (build "windowed"). Segue sem a rede de protecao.
            pass


def _console_stream():
    stream = sys.stdout
    if stream is None:
        # Sem console (build "windowed" do PyInstaller). O StreamHandler
        # cairia em `sys.stderr`, que nesse modo tambem e None, e cada linha
        # de log viraria AttributeError. O log em ARQUIVO continua saindo,
        # que e o que importa quando ninguem esta olhando a tela.
        return open(os.devnull, "w", encoding="utf-8")

    # Normalmente ja foi endurecido pelo `harden_console` no boot; repetir e
    # barato e cobre quem chama `setup_logging` direto (teste, embutido).
    harden_console()
    return stream
