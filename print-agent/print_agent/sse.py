"""Leitura do protocolo SSE.

O `EventSource` do navegador faz isto de graca; um agente em Python precisa
fazer a mao. Sao poucas regras, e elas estao separadas do resto justamente
para poder ser testadas sem rede: `parse_events` recebe linhas e devolve
eventos, sem saber de onde as linhas vieram.

Formato produzido pela API (`AdminOrderStreamService._format_event`):

    retry: 3000

    id: 2026-08-09T17:32:00+00:00
    event: order.created
    data: {"type": "order.created", ...}

    : ping

Tres coisas que o agente NAO pode tratar como erro:

1. **`: ping`** e comentario. Chega a cada 20s e existe para o proxy nao
   derrubar a conexao ociosa. Nao e evento.
2. **A conexao fechar depois de 15 minutos** e o comportamento normal do
   servidor (`MAX_STREAM_SECONDS`), nao uma falha. Reabrir e esperado.
3. **Evento repetido** e esperado: o servidor entrega ao menos uma vez.
   Quem descarta e a memoria de pedidos ja impressos.
"""

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str
    event_id: str | None = None

    def json(self) -> dict | None:
        """`data` decodificado, ou None se vier quebrado.

        Um `data:` invalido nao derruba o agente. A alternativa seria uma
        excecao que fecha o stream e, com ela, um pedido que nao imprime por
        causa de um evento que talvez nem fosse de pedido.
        """
        if not self.data:
            return None
        try:
            payload = json.loads(self.data)
        except ValueError:
            logger.warning("evento com data invalido, ignorado: %.120s", self.data)
            return None
        return payload if isinstance(payload, dict) else None


def parse_events(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Transforma linhas cruas do stream em eventos.

    Acumula campos ate a linha em branco, que e o que fecha um evento no
    protocolo. Campos desconhecidos e comentarios sao descartados sem
    reclamar — o protocolo manda ignora-los, e um dia a API pode passar a
    mandar algo novo sem que este agente precise ser atualizado.
    """
    event_type = "message"
    data_lines: list[str] = []
    event_id: str | None = None

    for raw_line in lines:
        line = raw_line.rstrip("\n").rstrip("\r")

        if line.startswith(":"):
            # Comentario, incluindo o heartbeat. Ignorado de proposito.
            continue

        if not line:
            if data_lines:
                yield SseEvent(
                    event=event_type,
                    data="\n".join(data_lines),
                    event_id=event_id,
                )
            event_type = "message"
            data_lines = []
            # `event_id` NAO e zerado: no protocolo o ultimo id visto
            # continua valendo para os eventos seguintes que nao trouxerem
            # um. E ele que vira o Last-Event-ID da reconexao.
            continue

        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value

        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_type = value
        elif field == "id":
            event_id = value
        elif field == "retry":
            # A API sugere o intervalo de reconexao. O agente tem a propria
            # politica de backoff (ver `agent`), entao a sugestao so e
            # registrada.
            logger.debug("servidor sugeriu retry de %s ms", value)
