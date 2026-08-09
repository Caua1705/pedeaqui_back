"""Ponto de entrada: `python -m print_agent`.

Modulo executavel e nao um script solto porque e assim que o NSSM (e
qualquer gerenciador de servico do Windows) chama o processo: um executavel
do Python com argumentos fixos, sem shell no meio.
"""

import argparse
import logging
import signal
import sys
from pathlib import Path

from print_agent import __version__
from print_agent.agent import PrintAgent
from print_agent.api_client import ApiClient
from print_agent.config import ConfigError, load_config
from print_agent.logging_setup import setup_logging
from print_agent.printers import LoggingPrinter, WindowsRawPrinter
from print_agent.state import PrintedOrders


DEFAULT_CONFIG_NAME = "config.ini"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="print-agent",
        description="Agente de impressao da loja: escuta pedidos aceitos e imprime as vias.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_NAME,
        help="caminho do config.ini (padrao: ao lado da pasta print_agent)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="nao imprime: registra no log o que seria enviado a cada impressora",
    )
    parser.add_argument("--version", action="version", version=f"print-agent {__version__}")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Antes do setup_logging de proposito: se o config esta quebrado,
        # nem o caminho do log e confiavel.
        print(f"erro de configuracao: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        config.log_file,
        level=config.log_level,
        max_bytes=config.log_max_bytes,
        backup_count=config.log_backup_count,
    )
    logger = logging.getLogger("print_agent")

    dry_run = args.dry_run or config.dry_run
    logger.info("print-agent %s subindo (config: %s)", __version__, args.config)
    logger.info("API: %s", config.api_base_url)
    logger.info(
        "setores mapeados: %s%s",
        ", ".join(sorted(config.printers)) or "nenhum",
        f" (default: {config.default_printer})" if config.default_printer else "",
    )
    if dry_run:
        logger.warning("dry-run ligado: NADA sera impresso de verdade")

    state = PrintedOrders(config.state_file, config.state_retention_days)
    logger.info("%d pedidos ja impressos na memoria de %s", len(state), config.state_file)

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

    try:
        agent.run()
    except KeyboardInterrupt:
        logger.info("interrompido pelo teclado")
    logger.info(
        "encerrando: %d pedidos, %d vias, %d falhas, %d reconexoes",
        agent.stats.printed_orders,
        agent.stats.printed_jobs,
        agent.stats.failed_jobs,
        agent.stats.reconnects,
    )
    return 0


def _install_signal_handlers(agent: PrintAgent, logger: logging.Logger) -> None:
    """Parada limpa quando o servico e desligado.

    O NSSM manda CTRL+BREAK (SIGBREAK no Windows) antes de matar o processo.
    Atender o sinal e o que garante que o agente nao morra no meio de uma
    via, deixando meia comanda na bobina e o pedido marcado como impresso.
    """

    def handle(signum, _frame):
        logger.info("sinal %s recebido, encerrando apos o evento atual", signum)
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
