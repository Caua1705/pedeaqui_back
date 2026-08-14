"""Ponto de entrada: `python -m print_agent`, ou o executavel RapidexImpressao.

Modulo executavel e nao um script solto porque e assim que o PyInstaller e
qualquer gerenciador de servico do Windows chamam o processo: um executavel
com argumentos fixos, sem shell no meio.

**Todo caminho em disco sai de `print_agent.paths`**, nunca de `__file__`.
Congelado pelo PyInstaller, `__file__` aponta para uma pasta temporaria que
o Windows apaga quando o processo termina — config lido de la nunca seria o
que o instalador copiou, e log e estado gravados la evaporariam. Ver o
docstring de `paths.py`.

A saida do console continua em portugues e continua saindo — mas **ninguem
mais a ve**. O executavel e gerado com `--noconsole` (ver build.bat) e o
programa vive como icone na bandeja. O `print` sobrevive para quem roda
`python -m print_agent` a mao, e nada mais.

A consequencia dessa mudanca esta em `alerts.py`: mensagem que EXIGE acao do
lojista — configuracao incompleta, credencial recusada, impressora que nao
aceitou a via — vira caixa do Windows, senao ela desaparece e o programa fica
vivo sem imprimir, em silencio.
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

from print_agent import __version__, alerts, paths
from print_agent.agent import PrintAgent
from print_agent.api_client import ApiClient
from print_agent.config import Config, ConfigError, load_config, write_template
from print_agent.escpos import expected_codepage
from print_agent.logging_setup import harden_console, setup_logging
from print_agent.printers import LoggingPrinter, WindowsRawPrinter
from print_agent.state import PrintedOrders


# Codigos de saida. O instalador nao os le, mas quem roda a mao no balcao ve
# a mensagem, e um dia um agendador de tarefas pode olhar o codigo.
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="RapidexImpressao",
        description="Agente de impressao da loja: escuta pedidos aceitos e imprime as vias.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="caminho do config.ini (padrao: ao lado do programa)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="nao imprime: registra no log o que seria enviado a cada impressora",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="roda preso ao terminal, sem icone na bandeja (para depurar)",
    )
    parser.add_argument("--version", action="version", version=f"RapidexImpressao {__version__}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    # Antes de qualquer `print`: o console do Windows abre numa codepage que
    # nao cobre tudo, e as primeiras linhas ja carregam caminho de disco e
    # mensagem de erro de config. Ver `harden_console`.
    harden_console()

    args = parse_args(argv)
    config_path = args.config or paths.config_path()

    print(f"Rapidex Impressao {__version__}")
    print(f"Pasta do programa: {paths.base_dir()}")
    print(f"Configuracao:      {config_path}")
    print()

    if not config_path.is_file():
        return _handle_missing_config(config_path)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # Antes do setup_logging de proposito: se o config esta quebrado, nem
        # o caminho do log e confiavel.
        _fail(
            "A configuracao esta incompleta ou com erro.",
            str(exc),
            f"Abra o arquivo {config_path} e corrija.",
        )
        return EXIT_CONFIG

    setup_logging(
        config.log_file,
        level=config.log_level,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )
    logger = logging.getLogger("print_agent")

    dry_run = args.dry_run or config.dry_run
    logger.info("Rapidex Impressao %s iniciando", __version__)
    logger.info("Servidor: %s", config.api_base_url)
    logger.info("Registro (log) em: %s", config.log_file)
    _log_printer_map(logger, config)
    _warn_on_codepage_mismatch(logger, config)

    if config.token and not (config.email and config.password):
        # O aviso mais importante do boot. Ver api_client._authorize.
        logger.warning(
            "ATENCAO: o config.ini tem apenas um token, sem email/password. "
            "O token vence em 12 horas e o programa vai PARAR de imprimir "
            "ate alguem trocar. Para rodar sozinho, preencha email e "
            "password."
        )

    if dry_run:
        logger.warning("Modo de teste ligado: NADA sera impresso de verdade.")

    state = PrintedOrders(config.state_file, config.state_retention_days)
    logger.info(
        "Memoria de pedidos ja impressos: %d pedido(s) em %s",
        len(state),
        config.state_file,
    )

    agent = PrintAgent(
        config=config,
        client=ApiClient(
            base_url=config.api_base_url,
            token=config.token,
            email=config.email,
            password=config.password,
        ),
        printer=LoggingPrinter() if dry_run else WindowsRawPrinter(),
        state=state,
    )

    _install_signal_handlers(agent, logger)

    if args.no_tray:
        finished_cleanly = _run_headless(agent, logger)
    else:
        finished_cleanly = _run_with_tray(agent, config, logger)

    logger.info(
        "Encerrando. Resumo: %d pedidos impressos, %d vias, %d falhas, "
        "%d reconexoes, %d comandos do painel.",
        agent.stats.printed_orders,
        agent.stats.printed_jobs,
        agent.stats.failed_jobs,
        agent.stats.reconnects,
        agent.stats.commands,
    )

    if not finished_cleanly:
        # Sem `alerts`: com bandeja, o `Tray.on_fatal` ja mostrou a caixa —
        # e mostrou o motivo EXATO, que aqui nao esta mais a mao. Repetir
        # daria duas caixas para o mesmo problema.
        _fail(
            "O programa parou porque o servidor recusou a credencial.",
            "Confira email, password (ou token) no config.ini.",
            f"O motivo exato esta no fim de {config.log_file}",
        )
        return EXIT_AUTH
    return EXIT_OK


def _run_headless(agent: PrintAgent, logger: logging.Logger) -> bool:
    """O laco preso ao terminal, sem bandeja. `--no-tray`.

    E como o programa rodava antes do icone, e continua existindo para tres
    situacoes: depurar no terminal vendo o log ao vivo, rodar numa maquina
    que nao e Windows, e o teste automatizado.
    """
    try:
        return agent.run()
    except KeyboardInterrupt:
        logger.info("Encerrado pelo teclado (Ctrl+C).")
        return True


def _run_with_tray(agent: PrintAgent, config: Config, logger: logging.Logger) -> bool:
    """O laco numa thread, com o icone na bandeja segurando a principal.

    Cai para o modo sem bandeja quando o `pystray` nao esta disponivel — o
    que acontece fora do Windows e numa sessao sem area de trabalho. Um
    agente que imprime sem icone e muito melhor que um agente que nao sobe:
    quem esta sem icone perde a cor e o menu, quem nao sobe perde a cozinha.
    """
    try:
        from print_agent.tray import Tray
    except Exception as exc:
        logger.warning(
            "sem icone na bandeja (%s); rodando preso ao terminal. As "
            "mensagens continuam em %s",
            exc,
            config.log_file,
        )
        return _run_headless(agent, logger)

    tray = Tray(config=config, agent=agent)
    # O icone precisa do agente (para o "Sair") e o agente precisa do icone
    # (para mudar de cor). A ligacao e feita aqui, fora dos dois, para que
    # nenhum dos dois modulos importe o outro: `agent.py` tem que continuar
    # importavel numa maquina sem `pystray`.
    agent.listener = tray

    tray.run(agent.run)
    return not tray.fatal


def _handle_missing_config(config_path) -> int:
    """Primeira execucao: cria o modelo e explica, em vez de estourar.

    Sem isto, a primeira coisa que o lojista ve e um traceback de Python numa
    janela preta — e a instalacao morre ali.

    Sem console, a caixa e a UNICA coisa que aparece. Ela tambem abre a pasta
    do config no Explorer: um caminho com `%LOCALAPPDATA%` no meio, ditado por
    telefone, nao chega inteiro do outro lado.
    """
    try:
        write_template(config_path)
    except OSError as exc:
        _fail(
            "Nao foi possivel criar o arquivo de configuracao.",
            f"{config_path}: {exc}",
            "Verifique se a pasta existe e se ha permissao de escrita.",
        )
        return EXIT_CONFIG

    _tell(
        "Esta e a primeira vez que o programa roda nesta maquina.",
        "",
        "Criei o arquivo de configuracao abaixo, com os campos em branco:",
        "",
        f"    {config_path}",
        "",
        "Abra esse arquivo no Bloco de Notas e preencha:",
        "",
        "  email    - e-mail do usuario do painel criado para esta impressora",
        "  password - a senha desse usuario",
        "  printer  - o nome EXATO da impressora, como aparece no Windows em",
        "             Configuracoes > Bluetooth e dispositivos > Impressoras",
        "",
        "Depois salve o arquivo e abra o programa de novo.",
    )
    paths.open_folder(config_path.parent)
    _pause()
    return EXIT_CONFIG


def _log_printer_map(logger, config) -> None:
    if config.printers:
        for sector in sorted(config.printers):
            logger.info("Setor '%s' imprime em '%s'", sector, config.printers[sector])
    if config.default_printer:
        logger.info(
            "Demais setores imprimem em '%s' (impressora padrao)", config.default_printer
        )
    elif not config.printers:  # pragma: no cover - load_config ja recusaria
        logger.error("Nenhuma impressora configurada.")


def _warn_on_codepage_mismatch(logger, config) -> None:
    """Avisa quando `codepage` e `encoding` nao combinam.

    So aviso: impressora de fabricante exotico numera as tabelas do jeito
    dela, e quem precisou fugir do padrao para conseguir imprimir nao pode
    ser barrado por isto.

    Mas o par trocado e o defeito mais dificil de diagnosticar por telefone:
    nada estoura, o log fica limpo, o pedido imprime — e sai "Picanha Ó
    Moda" na bobina. Com esta linha, o log responde a pergunta.
    """
    expected = expected_codepage(config.encoding)
    if expected is None or expected == config.codepage:
        return
    logger.warning(
        "ATENCAO: [printing] encoding = %s combina com codepage = %d, mas o "
        "config.ini diz codepage = %d. Se os acentos sairem trocados na "
        "bobina ('Picanha Ó Moda'), e este par.",
        config.encoding,
        expected,
        config.codepage,
    )


def _fail(*lines: str) -> None:
    """Erro que impede o programa de subir: no console E numa caixa.

    Nos dois de proposito. O console serve a quem esta rodando a mao com
    `python -m print_agent`; a caixa e a unica coisa que existe no
    executavel `--noconsole`, que e como o lojista roda.
    """
    print(file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    for line in lines:
        print(line, file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    print(file=sys.stderr)
    alerts.show_error("\n".join(lines))
    _pause()


def _tell(*lines: str) -> None:
    """Instrucao que o lojista precisa seguir: no console E numa caixa.

    Separada do `_fail` pelo icone da caixa: erro tem X vermelho, instrucao
    tem so o texto. A pessoa que ve a tela na instalacao precisa distinguir
    "o programa quebrou" de "falta preencher o arquivo".
    """
    for line in lines:
        print(line)
    print()
    alerts.show_info("\n".join(lines))
    _pause()


def _pause() -> None:
    """Segura a janela aberta para a mensagem poder ser lida.

    Sem isto, um duplo-clique no executavel abre e fecha o console antes de
    qualquer um conseguir ler o que deu errado. Só pausa quando ha um
    terminal interativo: rodando como tarefa agendada, `stdin` nao e um
    console e o programa nao pode ficar preso esperando um Enter que nunca
    vem.
    """
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        input("Pressione Enter para fechar...")
    except (EOFError, KeyboardInterrupt):
        pass


def _install_signal_handlers(agent: PrintAgent, logger: logging.Logger) -> None:
    """Parada limpa quando o processo e desligado.

    Atender o sinal e o que garante que o agente nao morra no meio de uma
    via, deixando meia comanda na bobina e o pedido marcado como impresso.
    """

    def handle(signum, _frame):
        logger.info("Pedido de encerramento recebido; parando apos o pedido atual.")
        agent.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):  # pragma: no cover - depende do SO
                pass


if __name__ == "__main__":
    sys.exit(main())
