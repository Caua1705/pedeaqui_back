"""O laco do agente: escutar, buscar as vias, imprimir.

O agente e burro DE PROPOSITO. Ele nao sabe o que e um adicional, nao sabe
que via de producao nao leva preco e nao sabe que pedido nao pago nao vai
para a cozinha. Tudo isso e decidido pela API, que devolve as vias ja
prontas em `GET /admin/orders/{id}/print-jobs`. Aqui sobra: qual impressora
recebe cada via, e como falar ESC/POS com ela.

A razao de ser assim: existe UM agente por loja, instalado a mao, atualizado
quando alguem lembra. Regra que mora nele so pode ser corrigida indo ate a
loja. Regra que mora na API se corrige com um deploy e vale para todo mundo
na mesma hora.

## Reconectar e normal, nao e falha

O servidor fecha o stream a cada 15 minutos de proposito
(`MAX_STREAM_SECONDS`), e proxies derrubam conexao ociosa — daí o heartbeat
de 20s da API. Por isso o backoff so cresce quando a conexao cai RAPIDO: uma
que durou mais que `HEALTHY_CONNECTION_SECONDS` zera a espera, senao o
agente que roda a noite inteira chegaria de manha esperando um minuto entre
tentativas por ter reconectado normalmente centenas de vezes.

## Qual impressora recebe cada via

Duas fontes, nesta ordem: a impressora escolhida no PAINEL para aquele setor
(`printer_name`, que vem junto da via) e, se ela nao existir, o mapa do
`config.ini` local.

O painel vem primeiro porque o `config.ini` casa pelo NOME do setor —
renomear "Cozinha" para "Cozinha Quente" no painel fazia a via deixar de
casar, cair na impressora padrao e a comanda da cozinha comecar a sair no
balcao, sem erro nenhum em lugar nenhum. O `config.ini` continua valendo
como fallback: e ele que mantem imprimindo toda loja instalada antes de a
coluna existir.

## O que impede a reimpressao

Duas coisas, e as duas sao necessarias:

- O `Last-Event-ID` faz o servidor repetir o que aconteceu durante a queda,
  o que e desejado (senao o pedido perdido nunca imprime) e traz repeticao
  junto.
- A memoria em disco (`PrintedOrders`) descarta o que ja saiu. Sem ela,
  cada reconexao cuspiria a fila do dia inteiro.
"""

import logging
import random
import time
from dataclasses import dataclass

from print_agent.api_client import ApiClient, ApiError, AuthError, AuthFatalError
from print_agent.config import Config
from print_agent.escpos import build_payload
from print_agent.printers import Printer, PrinterError
from print_agent.sse import parse_events
from print_agent.state import PrintedOrders


logger = logging.getLogger(__name__)

# Acima disto, a conexao e considerada saudavel e o backoff volta ao piso.
# Trinta segundos separa "o servidor fechou depois de trabalhar" de "a API
# esta fora e recusa na hora".
HEALTHY_CONNECTION_SECONDS = 30.0

# Eventos que podem trazer um pedido no status que dispara a impressao.
# `order.created` entra na lista porque um pedido pode nascer ja aceito se a
# regra de aceite automatico mudar um dia — e mais barato escutar os dois do
# que descobrir isso com a cozinha parada.
ORDER_EVENT_TYPES = ("order.created", "order.status_changed")


@dataclass
class Stats:
    """Contadores do turno, para a linha de log de cada reconexao."""

    printed_orders: int = 0
    printed_jobs: int = 0
    failed_jobs: int = 0
    reconnects: int = 0


class PrintAgent:
    def __init__(self, config: Config, client: ApiClient, printer: Printer, state: PrintedOrders):
        self.config = config
        self.client = client
        self.printer = printer
        self.state = state
        self.stats = Stats()
        self.last_event_id: str | None = None
        self._running = True

    def stop(self) -> None:
        """Pedido de parada, atendido no fim do evento em andamento.

        Nao interrompe uma via no meio: o Ctrl+C (ou o stop do servico) que
        cortasse um `WritePrinter` deixaria meia comanda na bobina e o pedido
        marcado como impresso.
        """
        self._running = False

    def run(self) -> bool:
        """Laco principal.

        Devolve `True` quando parou normalmente (`stop()`) e `False` quando
        parou por credencial que nao tem conserto sozinha — e o `__main__`
        usa isso para escolher o codigo de saida.
        """
        delay = self.config.reconnect_min_seconds

        while self._running:
            started_at = time.monotonic()
            try:
                self._consume_stream()
            except AuthFatalError as exc:
                # Nao adianta insistir: alguem precisa editar o config.ini.
                # Ficar tentando para sempre deixaria um processo vivo que
                # nunca imprime, e ninguem percebe isso ate o cliente
                # reclamar.
                logger.error("PARANDO: %s", exc)
                self._running = False
                return False
            except AuthError as exc:
                # Token de 12h vencido com email/password ao lado: o proximo
                # ciclo refaz o login sozinho. E o caminho normal de um
                # agente que roda por semanas.
                logger.info("credencial expirada (%s); refazendo o login", exc)
                delay = self.config.reconnect_min_seconds
            except ApiError as exc:
                logger.warning("conexao com o servidor caiu: %s", exc)
            except Exception:  # pragma: no cover - rede e Windows sao criativos
                logger.exception("erro inesperado na conexao com o servidor")

            if not self._running:
                break

            lasted = time.monotonic() - started_at
            if lasted >= HEALTHY_CONNECTION_SECONDS:
                # Fechamento normal do servidor: reconecta imediatamente,
                # sem penalizar quem estava funcionando.
                delay = self.config.reconnect_min_seconds

            self.stats.reconnects += 1
            wait = min(delay, self.config.reconnect_max_seconds)
            # Jitter para varias lojas nao voltarem todas no mesmo segundo
            # depois de uma queda da API.
            wait += random.uniform(0, wait * 0.25)
            logger.info(
                "reconectando em %.0f segundos (a conexao durou %.0fs; "
                "%d pedidos impressos ate agora)",
                wait,
                lasted,
                self.stats.printed_orders,
            )
            if self._sleep(wait):
                delay = min(delay * 2, self.config.reconnect_max_seconds)

        return True

    def _consume_stream(self) -> None:
        if self.last_event_id:
            logger.info("conectando ao servidor (retomando de onde parou)")
        else:
            logger.info("conectando ao servidor")
        lines = self.client.open_stream(self.last_event_id)
        logger.info("conectado. Aguardando pedidos.")

        for event in parse_events(lines):
            if not self._running:
                return
            if event.event_id:
                self.last_event_id = event.event_id
            self._handle_event(event)

    def _handle_event(self, event) -> None:
        if event.event == "sync_required":
            # O agente ficou fora tempo demais para o replay (mais de uma
            # hora). Nao ha o que recuperar sozinho: pedidos aceitos nesse
            # intervalo podem nao ter impresso, e quem decide reimprimir e
            # quem esta no balcao.
            logger.warning(
                "o servidor avisou que o replay foi truncado: pedidos aceitos "
                "durante a queda podem NAO ter sido impressos. Confira o "
                "painel."
            )
            return

        if event.event not in ORDER_EVENT_TYPES:
            return

        payload = event.json()
        if not payload:
            return

        order = payload.get("order") or {}
        order_id = order.get("id")
        status = order.get("status")
        if not order_id or status != self.config.trigger_status:
            return

        if order_id in self.state:
            logger.debug("pedido %s ja impresso, ignorando", order_id)
            return

        self._print_order(order_id, order.get("order_number"))

    def _printer_for(self, chosen_in_panel: str | None, sector_name: str) -> str | None:
        """A impressora de uma via: a do painel, senao a do config.ini.

        O painel vence porque e o unico dos dois que nao quebra quando o
        setor e renomeado — o `config.ini` casa pelo NOME do setor, e um
        rename fazia a via cair na impressora padrao sem nenhum erro
        aparecer.

        O config.ini continua valendo como fallback, e nao como legado a
        remover: e ele que mantem imprimindo toda loja instalada antes de a
        coluna `printer_name` existir.
        """
        return chosen_in_panel or self.config.printer_for(sector_name)

    def _print_order(self, order_id: str, order_number=None) -> None:
        """Busca as vias do pedido e manda cada uma para a sua impressora.

        O pedido so e marcado como impresso quando TODAS as vias sairam. Meia
        comanda impressa e pior que nenhuma: a cozinha prepara o que viu e o
        resto do pedido some.
        """
        label = f"#{order_number}" if order_number else order_id
        logger.info("pedido %s recebido, buscando as vias para imprimir", label)
        try:
            jobs = self.client.print_jobs(order_id)
        except ApiError as exc:
            logger.error("nao foi possivel obter as vias do pedido %s: %s", label, exc)
            return

        if not jobs:
            # Acontece de verdade: pedido online ainda nao pago nao gera via
            # de producao, e a API devolve a lista vazia de proposito.
            logger.warning(
                "pedido %s nao gerou via nenhuma (pagamento ainda nao "
                "confirmado?). Nada foi impresso.",
                label,
            )
            return

        printed = 0
        for job in jobs:
            if self._print_job(job, label):
                printed += 1

        if printed == len(jobs):
            self.state.mark(order_id)
            self.stats.printed_orders += 1
            logger.info("pedido %s impresso por completo (%d vias)", label, printed)
        else:
            # Sem `mark`: o pedido continua elegivel. Se o servidor repetir o
            # evento (o que acontece na reconexao), ele tenta de novo.
            logger.error(
                "pedido %s saiu incompleto: %d de %d vias. NAO foi marcado "
                "como impresso.",
                label,
                printed,
                len(jobs),
            )

    def _print_job(self, job: dict, label: str) -> bool:
        sector = job.get("sector_name") or "?"
        content = job.get("content")
        if not content:
            logger.error("via '%s' do pedido %s veio sem conteudo", sector, label)
            self.stats.failed_jobs += 1
            return False

        printer_name = self._printer_for(job.get("printer_name"), sector)
        if not printer_name:
            # Setor sem impressora escolhida no painel, sem linha no
            # config.ini e sem 'default'. Grita no log: e uma via que nao vai
            # sair.
            logger.error(
                "setor '%s' (pedido %s) nao tem impressora escolhida no painel "
                "nem no config.ini, e nao ha 'default'. A via NAO foi impressa.",
                sector,
                label,
            )
            self.stats.failed_jobs += 1
            return False

        payload = build_payload(
            content,
            font_size=job.get("font_size", "normal"),
            codepage=self.config.codepage,
            encoding=self.config.encoding,
            cut=self.config.cut,
            feed_lines=self.config.feed_lines,
        )
        job_name = f"pedido {label} - {sector}"

        for attempt in range(1, self.config.print_attempts + 1):
            try:
                self.printer.send(printer_name, payload, job_name)
                self.stats.printed_jobs += 1
                logger.info(
                    "pedido %s: via '%s' impressa em '%s'", label, sector, printer_name
                )
                return True
            except PrinterError as exc:
                logger.warning(
                    "pedido %s: tentativa %d de %d de imprimir '%s' em '%s' "
                    "falhou (%s). Tentando de novo em %.0fs.",
                    label,
                    attempt,
                    self.config.print_attempts,
                    sector,
                    printer_name,
                    exc,
                    self.config.print_retry_seconds,
                )
                if attempt < self.config.print_attempts:
                    self._sleep(self.config.print_retry_seconds)

        logger.error(
            "pedido %s: a via '%s' NAO foi impressa em '%s' apos %d "
            "tentativas. Confira papel, energia e cabo da impressora. O "
            "pedido continua na fila e sera tentado de novo na proxima "
            "reconexao.",
            label,
            sector,
            printer_name,
            self.config.print_attempts,
        )
        self.stats.failed_jobs += 1
        return False

    def _sleep(self, seconds: float) -> bool:
        """Espera em fatias para o `stop()` ser atendido em ate um segundo.

        Um `time.sleep(60)` inteiro faria o "parar servico" do Windows bater
        no timeout e matar o processo.
        """
        remaining = seconds
        while remaining > 0 and self._running:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step
        return self._running
