"""Stream de pedidos do painel (SSE).

## Por que SSE e nao WebSocket

O painel so PRECISA receber. Tudo o que ele manda (aceitar, recusar, mudar
status) ja tem rota REST com idempotencia, maquina de estados e historico.
Um canal bidirecional significaria uma segunda porta de escrita para as
mesmas operacoes, com autenticacao e validacao proprias — mais superficie
para o mesmo resultado.

Alem disso, tres coisas deste backend especificamente:

1. **O backend e sincrono, com poucos workers.** Endpoint `def` do FastAPI
   roda no threadpool; uma conexao WebSocket segurada por horas prende uma
   thread durante todo o tempo ocioso. Por isso a rota do stream e
   `async def` e o trabalho de banco vai para o threadpool so durante a
   consulta (`run_in_threadpool`), liberando a thread entre um poll e
   outro.
2. **Reconexao vem de graca.** O `EventSource` do navegador reconecta
   sozinho e reenvia o `Last-Event-ID`. Com WebSocket, reconexao e
   backoff seriam codigo no frontend.
3. **Atravessa o Traefik como HTTP comum.** WebSocket exige upgrade
   configurado no proxy; SSE e uma resposta `text/event-stream` que fica
   aberta.

## Por que o cursor mora no banco e nao em memoria

A tentacao seria uma fila em memoria alimentada pelo `update_order_status`.
Isso quebra por dois motivos ja registrados no projeto (armadilha 9.9 da
arquitetura): com mais de um worker, o pedido criado no worker A nunca
chegaria ao painel conectado no worker B; e um deploy no meio do almoco
perderia tudo o que estivesse na fila.

Aqui o estado do stream e um cursor de tempo. Cada poll pergunta ao banco
"o que aconteceu depois de X" — em `orders` (pedido novo) e em
`order_status_history` (mudanca de status). Reiniciar o processo, trocar de
worker ou o cliente cair e voltar nao perdem nada: o cursor volta pelo
`Last-Event-ID`.

## Entrega ao menos uma vez

Cada poll olha um pouco para tras do cursor (`OVERLAP_SECONDS`). Isso existe
porque `created_at` e o instante em que a transacao COMECOU: um pedido cuja
transacao demorou pode ficar visivel depois de outro com `created_at` maior
e, sem a sobreposicao, cairia num intervalo ja varrido — pedido perdido, que
e exatamente o que nao pode acontecer.

O preco e evento repetido. Por isso todo evento carrega `event_key` estavel
e o painel descarta o que ja aplicou.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from starlette.concurrency import run_in_threadpool

from src.api.dependencies.admin_scope import AdminScope
from src.db.session import SessionLocal
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_order_schema import AdminOrderStreamEvent
from src.services.admin_order_service import AdminOrderService


logger = logging.getLogger("uvicorn.error")

# Intervalo entre consultas ao banco. Quatro segundos e o compromisso entre
# "o pedido aparece na hora" (a cozinha nao percebe 4s) e o custo de duas
# consultas indexadas por painel aberto.
POLL_INTERVAL_SECONDS = 4.0

# Quanto o poll olha para tras do cursor. Ver "Entrega ao menos uma vez".
OVERLAP_SECONDS = 5

# Heartbeat. Proxy e balanceador derrubam conexao ociosa (o padrao do
# Traefik e da faixa de 60s); um comentario SSE a cada 20s mantem a conexao
# viva e nao vira evento no cliente.
HEARTBEAT_INTERVAL_SECONDS = 20.0

# Tempo de vida de uma conexao. Fechar e reabrir de proposito evita conexao
# zumbi acumulando no worker; o EventSource reconecta sozinho com o
# Last-Event-ID, entao para o painel isso e invisivel.
MAX_STREAM_SECONDS = 900

# Ate onde o replay volta no tempo. Um painel que ficou fechado a noite
# inteira nao deve receber 400 eventos na reconexao: recebe `sync_required`
# e recarrega a lista, que e mais barato para os dois lados.
MAX_REPLAY_SECONDS = 3600

# Teto de eventos por poll. Protege contra um pico (importacao, correcao em
# massa) virar uma resposta gigante de uma vez so; o que sobrar vem no poll
# seguinte, porque o cursor so avanca ate o ultimo emitido.
MAX_EVENTS_PER_POLL = 100

# Quantas chaves de evento a conexao lembra para nao repetir dentro dela
# mesma. Limitado porque um stream de 15 minutos com pico de pedidos nao
# pode crescer sem teto na memoria do worker.
DEDUPE_MEMORY_SIZE = 500

# Intervalo de reconexao sugerido ao EventSource, em milissegundos.
RETRY_HINT_MS = 3000


class AdminOrderStreamService:
    """Gera os eventos do stream de um lojista.

    Nao recebe `Session` no construtor, ao contrario dos outros services: um
    stream vive minutos e uma sessao aberta esse tempo todo seguraria uma
    conexao do pool e uma transacao ociosa no Postgres. Cada poll abre e
    fecha a sua.
    """

    def __init__(self, scope: AdminScope):
        self.scope = scope
        # Chaves ja emitidas NESTA conexao. OrderedDict e nao set por causa
        # do descarte das mais antigas quando o limite estoura.
        self._emitted_keys: OrderedDict[str, None] = OrderedDict()

    async def iter_events(self, last_event_id: str | None) -> AsyncIterator[str]:
        """Corpo do `text/event-stream`, pedaco a pedaco.

        Assincrono para nao prender uma thread do pool enquanto espera o
        proximo poll — ver a nota sobre backend sincrono no topo do modulo.
        """
        cursor, replay_truncated = self._resolve_initial_cursor(last_event_id)

        yield f"retry: {RETRY_HINT_MS}\n\n"
        if replay_truncated:
            yield self._format_event(
                AdminOrderStreamEvent(
                    type="sync_required",
                    event_key=f"sync:{cursor.isoformat()}",
                    occurred_at=cursor,
                    note="Reconexao antiga demais para replay. Recarregue a lista de pedidos.",
                )
            )

        started_at = _utcnow()
        last_heartbeat_at = started_at

        while (_utcnow() - started_at).total_seconds() < MAX_STREAM_SECONDS:
            events = await run_in_threadpool(self._fetch_events, cursor)

            for event in events:
                if self._already_emitted(event.event_key):
                    continue
                self._remember(event.event_key)
                cursor = max(cursor, event.occurred_at)
                last_heartbeat_at = _utcnow()
                yield self._format_event(event)

            if (_utcnow() - last_heartbeat_at).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS:
                # Comentario SSE: o navegador ignora, o proxy ve trafego.
                yield ": ping\n\n"
                last_heartbeat_at = _utcnow()

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _fetch_events(self, cursor: datetime) -> list[AdminOrderStreamEvent]:
        """Um poll: pedidos novos e mudancas de status desde o cursor.

        Roda no threadpool (o SQLAlchemy aqui e sincrono). A sessao e criada
        e fechada dentro da funcao justamente porque este e o unico trecho
        que precisa de banco.
        """
        since = cursor - timedelta(seconds=OVERLAP_SECONDS)
        db = SessionLocal()
        try:
            repository = OrderRepository(db)
            created = repository.list_orders_created_since(
                restaurant_id=self.scope.restaurant_id,
                branch_id=self.scope.branch_id,
                since=since,
                limit=MAX_EVENTS_PER_POLL,
            )
            changed = repository.list_status_changes_since(
                restaurant_id=self.scope.restaurant_id,
                branch_id=self.scope.branch_id,
                since=since,
                limit=MAX_EVENTS_PER_POLL,
            )
        finally:
            db.close()

        events = [
            AdminOrderStreamEvent(
                type="order.created",
                event_key=f"order-created:{order.id}",
                occurred_at=order.created_at,
                order=AdminOrderService.to_list_item(order),
            )
            for order in created
        ]
        events.extend(
            AdminOrderStreamEvent(
                type="order.status_changed",
                # A linha do historico e a identidade do fato: duas idas ao
                # mesmo status (preparing -> ready -> preparing) sao dois
                # eventos diferentes e o painel precisa ver os dois.
                event_key=f"status-changed:{history.id}",
                occurred_at=history.created_at,
                order=AdminOrderService.to_list_item(order),
                note=history.note,
            )
            for history, order in changed
        )
        # Ordena o conjunto ja unido: os dois SELECTs vem ordenados cada um
        # por si, mas o painel precisa ver a ordem em que os fatos
        # aconteceram, e o cursor so pode avancar em ordem crescente.
        events.sort(key=lambda event: event.occurred_at)
        return events

    def _resolve_initial_cursor(self, last_event_id: str | None) -> tuple[datetime, bool]:
        """De onde comecar: da reconexao do cliente ou de agora.

        Devolve tambem se o replay foi truncado, porque nesse caso o painel
        precisa ser avisado de que perdeu eventos e tem que recarregar.
        """
        now = _utcnow()
        floor = now - timedelta(seconds=MAX_REPLAY_SECONDS)

        if not last_event_id:
            # Primeira conexao: so o que acontecer daqui para a frente. O
            # que ja existe veio no GET /admin/orders que o painel fez ao
            # abrir a tela.
            return now, False

        try:
            cursor = datetime.fromisoformat(last_event_id)
        except ValueError:
            # Last-Event-ID e devolvido pelo navegador, mas nada impede um
            # cliente de mandar lixo. Lixo nao derruba o stream: comeca do
            # zero como uma conexao nova.
            logger.warning("[AdminStream] Last-Event-ID invalido: %s", last_event_id)
            return now, True

        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=timezone.utc)
        if cursor < floor:
            return floor, True
        # Cursor no futuro (relogio do cliente adiantado) nao pode pular
        # eventos: volta para agora.
        return min(cursor, now), False

    def _already_emitted(self, event_key: str) -> bool:
        return event_key in self._emitted_keys

    def _remember(self, event_key: str) -> None:
        self._emitted_keys[event_key] = None
        while len(self._emitted_keys) > DEDUPE_MEMORY_SIZE:
            self._emitted_keys.popitem(last=False)

    @staticmethod
    def _format_event(event: AdminOrderStreamEvent) -> str:
        """Um evento no formato do protocolo SSE.

        O `id:` e o instante do evento porque e ele que o navegador devolve
        como `Last-Event-ID` na reconexao — e cursor, nao identificador. Quem
        identifica o fato e o `event_key` dentro do JSON.
        """
        return (
            f"id: {event.occurred_at.isoformat()}\n"
            f"event: {event.type}\n"
            f"data: {event.model_dump_json()}\n\n"
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
