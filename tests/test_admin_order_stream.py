"""Stream SSE de pedidos do painel (BLOCO A2 da Fase 3).

O que estes testes protegem, em uma frase: **o painel nao pode perder
pedido**. Cada teste aqui corresponde a um jeito de perder um:

- o cursor da reconexao ser ignorado (o cliente volta e o que aconteceu
  offline some);
- a janela de sobreposicao nao existir (pedido cuja transacao demorou cai
  num intervalo ja varrido);
- o cursor andar para tras ou pular para o futuro;
- o filtro por filial deixar passar pedido de outra unidade — perder no
  sentido inverso, ver o que nao e seu.

O que NAO da para provar sem banco: que as duas consultas do poll acertam
o indice. Isso fica para a Fase 4.
"""

import json
import unittest
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.services import admin_order_stream_service as stream_module
from src.models.order_status_history_model import OrderStatusHistory
from src.models.print_agent_model import PrintAgentCommand
from src.models.printing_sector_model import PrintingSector
from src.services.admin_order_stream_service import (
    MAX_REPLAY_SECONDS,
    OVERLAP_SECONDS,
    AdminOrderStreamService,
)

from tests import fabricas


def make_order(restaurant_id, branch_id, created_at, status="pending"):
    return fabricas.pedido(
        restaurant_id=restaurant_id,
        branch_id=branch_id,
        order_number=42,
        customer_name_snapshot="Cliente",
        order_type="delivery",
        status=status,
        payment_method="cash",
        payment_status="on_delivery",
        total=Decimal("10.00"),
        created_at=created_at,
    )


class FakeStreamRepository:
    """Repositorio do poll, guardando com que `since` foi chamado."""

    def __init__(self, created=(), changed=()):
        self.created = list(created)
        self.changed = list(changed)
        self.since_calls = []

    def list_orders_created_since(self, restaurant_id, branch_id, since, limit):
        self.since_calls.append(since)
        return self.created

    def list_status_changes_since(self, restaurant_id, branch_id, since, limit):
        return self.changed


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def build_service(branch_id=None):
    return AdminOrderStreamService(
        AdminScope(admin_user=None, restaurant_id=uuid.uuid4(), branch_id=branch_id)
    )


class FakeCommandRepository:
    """Comandos do painel para o agente, guardando o `since` recebido."""

    def __init__(self, commands=()):
        self.commands = list(commands)
        self.since_calls = []

    def list_commands_since(self, branch_id, since, limit):
        self.since_calls.append((branch_id, since))
        return self.commands


class FakeSectorRepository:
    def __init__(self, sectors=()):
        self.sectors = list(sectors)

    def get(self, sector_id, restaurant_id):
        return next((s for s in self.sectors if s.id == sector_id), None)


def make_command(created_at, printer_name=None, sector_id=None, branch_id=None):
    return PrintAgentCommand(
        id=uuid.uuid4(),
        branch_id=branch_id or uuid.uuid4(),
        command_type="print_test",
        printer_name=printer_name,
        printing_sector_id=sector_id,
        created_at=created_at,
    )


def fetch_with(service, repository, cursor, commands=None, sectors=()):
    """Roda um poll com os repositorios trocados por fakes."""
    session = FakeSession()
    command_repository = FakeCommandRepository(commands or [])
    with patch.object(stream_module, "SessionLocal", lambda: session):
        with patch.object(stream_module, "OrderRepository", lambda db: repository):
            with patch.object(
                stream_module, "PrintAgentRepository", lambda db: command_repository
            ):
                with patch.object(
                    stream_module,
                    "PrintingSectorRepository",
                    lambda db: FakeSectorRepository(sectors),
                ):
                    events = service._fetch_events(cursor)
    return events, session


class CursorTests(unittest.TestCase):
    def test_first_connection_starts_from_now(self):
        service = build_service()

        cursor, truncated = service._resolve_initial_cursor(None)

        self.assertFalse(truncated)
        # Nao replica historico: o que ja existia veio no GET /admin/orders
        # que o painel fez ao abrir a tela.
        self.assertLess((datetime.now(timezone.utc) - cursor).total_seconds(), 5)

    def test_reconnection_resumes_from_the_last_event_id(self):
        service = build_service()
        cursor_sent = datetime.now(timezone.utc) - timedelta(minutes=3)

        cursor, truncated = service._resolve_initial_cursor(cursor_sent.isoformat())

        self.assertFalse(truncated)
        self.assertEqual(cursor, cursor_sent)

    def test_very_old_reconnection_is_truncated_and_flagged(self):
        service = build_service()
        ancient = datetime.now(timezone.utc) - timedelta(seconds=MAX_REPLAY_SECONDS * 3)

        cursor, truncated = service._resolve_initial_cursor(ancient.isoformat())

        # Replicar horas de eventos e pior para os dois lados: o painel
        # recebe `sync_required` e recarrega a lista.
        self.assertTrue(truncated)
        self.assertGreater(cursor, ancient)

    def test_garbage_last_event_id_does_not_break_the_stream(self):
        service = build_service()

        cursor, truncated = service._resolve_initial_cursor("nao-e-uma-data")

        self.assertTrue(truncated)
        self.assertIsNotNone(cursor)

    def test_cursor_in_the_future_is_pulled_back_to_now(self):
        # Relogio do cliente adiantado nao pode fazer o stream pular tudo o
        # que acontecer na proxima hora.
        service = build_service()
        future = datetime.now(timezone.utc) + timedelta(hours=1)

        cursor, _ = service._resolve_initial_cursor(future.isoformat())

        self.assertLess(cursor, future)

    def test_naive_last_event_id_is_read_as_utc(self):
        service = build_service()
        naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None)

        cursor, _ = service._resolve_initial_cursor(naive.isoformat())

        self.assertIsNotNone(cursor.tzinfo)


class PollTests(unittest.TestCase):
    def test_poll_looks_behind_the_cursor(self):
        service = build_service()
        repository = FakeStreamRepository()
        cursor = datetime.now(timezone.utc)

        fetch_with(service, repository, cursor)

        # A sobreposicao existe porque created_at e o instante em que a
        # transacao COMECOU: sem ela, um pedido de transacao lenta cairia
        # num intervalo ja varrido e nunca chegaria ao painel.
        self.assertEqual(
            repository.since_calls[0], cursor - timedelta(seconds=OVERLAP_SECONDS)
        )

    def test_session_is_closed_after_every_poll(self):
        # Um stream vive 15 minutos; sessao aberta esse tempo seguraria uma
        # conexao do pool e uma transacao ociosa no Postgres.
        service = build_service()
        _, session = fetch_with(service, FakeStreamRepository(), datetime.now(timezone.utc))

        self.assertTrue(session.closed)

    def test_new_order_becomes_an_order_created_event(self):
        service = build_service()
        order = make_order(uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc))

        events, _ = fetch_with(
            service, FakeStreamRepository(created=[order]), datetime.now(timezone.utc)
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "order.created")
        self.assertEqual(events[0].order.id, order.id)
        self.assertEqual(events[0].event_key, f"order-created:{order.id}")

    def test_status_change_becomes_an_event_keyed_by_the_history_row(self):
        service = build_service()
        order = make_order(uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc), "preparing")
        history = OrderStatusHistory(
            id=uuid.uuid4(),
            order_id=order.id,
            status="preparing",
            created_at=datetime.now(timezone.utc),
            note="saiu",
        )

        events, _ = fetch_with(
            service, FakeStreamRepository(changed=[(history, order)]), datetime.now(timezone.utc)
        )

        self.assertEqual(events[0].type, "order.status_changed")
        # A linha do historico e a identidade do fato: ir para `preparing`
        # duas vezes sao dois eventos, e o painel precisa ver os dois.
        self.assertEqual(events[0].event_key, f"status-changed:{history.id}")
        self.assertEqual(events[0].note, "saiu")

    def test_events_come_out_in_the_order_they_happened(self):
        service = build_service()
        now = datetime.now(timezone.utc)
        older = make_order(uuid.uuid4(), uuid.uuid4(), now - timedelta(seconds=30))
        newer = make_order(uuid.uuid4(), uuid.uuid4(), now)
        history = SimpleNamespace(
            id=uuid.uuid4(), created_at=now - timedelta(seconds=15), note=None
        )

        events, _ = fetch_with(
            service,
            FakeStreamRepository(created=[older, newer], changed=[(history, newer)]),
            now,
        )

        # Os dois SELECTs vem ordenados cada um por si; o cursor so pode
        # avancar em ordem crescente do conjunto unido.
        moments = [event.occurred_at for event in events]
        self.assertEqual(moments, sorted(moments))


class DedupeTests(unittest.TestCase):
    def test_the_same_event_is_not_emitted_twice_in_one_connection(self):
        service = build_service()

        self.assertFalse(service._already_emitted("order-created:1"))
        service._remember("order-created:1")
        self.assertTrue(service._already_emitted("order-created:1"))

    def test_dedupe_memory_does_not_grow_without_a_ceiling(self):
        # Um pico de pedidos nao pode fazer a conexao crescer sem teto na
        # memoria do worker.
        service = build_service()

        for index in range(stream_module.DEDUPE_MEMORY_SIZE + 50):
            service._remember(f"order-created:{index}")

        self.assertEqual(len(service._emitted_keys), stream_module.DEDUPE_MEMORY_SIZE)
        # As mais antigas sao as descartadas.
        self.assertFalse(service._already_emitted("order-created:0"))
        self.assertTrue(service._already_emitted("order-created:549"))


class WireFormatTests(unittest.TestCase):
    def test_event_is_formatted_as_sse_with_the_cursor_in_the_id(self):
        service = build_service()
        order = make_order(uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc))
        events, _ = fetch_with(
            service, FakeStreamRepository(created=[order]), datetime.now(timezone.utc)
        )

        wire = service._format_event(events[0])
        lines = wire.strip().split("\n")

        self.assertTrue(lines[0].startswith("id: "))
        self.assertEqual(lines[1], "event: order.created")
        self.assertTrue(lines[2].startswith("data: "))
        self.assertTrue(wire.endswith("\n\n"))
        # O `id:` e o cursor devolvido como Last-Event-ID na reconexao —
        # tem que ser um instante lido de volta por fromisoformat.
        self.assertIsNotNone(datetime.fromisoformat(lines[0][len("id: "):]))

    def test_payload_carries_the_dedupe_key(self):
        service = build_service()
        order = make_order(uuid.uuid4(), uuid.uuid4(), datetime.now(timezone.utc))
        events, _ = fetch_with(
            service, FakeStreamRepository(created=[order]), datetime.now(timezone.utc)
        )

        payload = json.loads(service._format_event(events[0]).split("data: ")[1])

        # O stream entrega AO MENOS uma vez; sem esta chave o painel nao tem
        # como descartar o que ja aplicou.
        self.assertEqual(payload["event_key"], f"order-created:{order.id}")
        self.assertEqual(payload["order"]["id"], str(order.id))


class StreamTicketTests(unittest.TestCase):
    """O ticket que autentica o SSE, ja que EventSource nao manda cabecalho."""

    def _admin(self):
        # `password_changed_at` nulo = nada revogado. O ticket passa pelo
        # mesmo `_load_admin_from_token` do token do painel, que le este campo
        # — trocar a senha derruba a conexao SSE junto.
        return fabricas.usuario_do_painel(role="owner")

    def test_ticket_is_short_lived(self):
        from src.services.admin_auth_service import AdminAuthService

        response = AdminAuthService.create_stream_ticket(self._admin())

        # Ele viaja na querystring e acaba no log do proxy: quanto menos
        # tempo valer, melhor.
        self.assertLessEqual(response.expires_in_seconds, 60)
        self.assertTrue(response.ticket)

    def test_access_token_is_not_accepted_as_a_stream_ticket(self):
        from src.services.admin_auth_service import AdminAuthService

        admin = self._admin()
        service = AdminAuthService.__new__(AdminAuthService)
        service.repository = SimpleNamespace(get_by_id=lambda _: admin)
        access_token = AdminAuthService.create_access_token(admin)

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_stream_ticket(access_token)

        # `purpose` separa os dois: o token de 12h nao abre stream e o
        # ticket de 30s nao muda status de pedido.
        self.assertEqual(raised.exception.status_code, 401)

    def test_stream_ticket_is_not_accepted_as_an_access_token(self):
        from src.services.admin_auth_service import AdminAuthService

        admin = self._admin()
        service = AdminAuthService.__new__(AdminAuthService)
        service.repository = SimpleNamespace(get_by_id=lambda _: admin)
        ticket = AdminAuthService.create_stream_ticket(admin).ticket

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_token(ticket)

        self.assertEqual(raised.exception.status_code, 401)

    def test_valid_ticket_returns_the_admin_reloaded_from_the_database(self):
        from src.services.admin_auth_service import AdminAuthService

        admin = self._admin()
        service = AdminAuthService.__new__(AdminAuthService)
        service.repository = SimpleNamespace(get_by_id=lambda _: admin)
        ticket = AdminAuthService.create_stream_ticket(admin).ticket

        self.assertIs(service.get_admin_from_stream_ticket(ticket), admin)

    def test_deactivated_admin_cannot_open_the_stream(self):
        from src.services.admin_auth_service import AdminAuthService

        admin = self._admin()
        ticket = AdminAuthService.create_stream_ticket(admin).ticket
        admin.is_active = False
        service = AdminAuthService.__new__(AdminAuthService)
        service.repository = SimpleNamespace(get_by_id=lambda _: admin)

        with self.assertRaises(HTTPException) as raised:
            service.get_admin_from_stream_ticket(ticket)

        # Por isso o lojista e recarregado do banco e nao lido do que esta
        # assinado: quem foi desativado no meio do turno para de receber.
        self.assertEqual(raised.exception.status_code, 403)


class PrintAgentCommandTests(unittest.TestCase):
    """A ordem do painel para o agente, entregue pelo MESMO stream.

    Ela nao ganhou canal proprio de propósito: uma fila em memoria morreria
    com mais de um worker e no deploy do meio do almoco (armadilha 20). O
    stream ja tem cursor no banco, replay e reconexao.
    """

    def test_a_command_becomes_an_event(self):
        branch_id = uuid.uuid4()
        service = build_service(branch_id=branch_id)
        agora = datetime.now(timezone.utc)

        events, _ = fetch_with(
            service,
            FakeStreamRepository(),
            agora,
            commands=[make_command(agora, printer_name="IMP-COZINHA", branch_id=branch_id)],
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, "print_agent.command")
        self.assertEqual(events[0].command.printer_name, "IMP-COZINHA")

    def test_the_ticket_comes_ready_from_the_api(self):
        """O agente nao desenha nada: uma via de teste montada nele sairia
        diferente em cada loja conforme a versao instalada."""
        branch_id = uuid.uuid4()
        service = build_service(branch_id=branch_id)
        agora = datetime.now(timezone.utc)

        events, _ = fetch_with(
            service,
            FakeStreamRepository(),
            agora,
            commands=[make_command(agora, branch_id=branch_id)],
        )

        self.assertIn("TESTE DE IMPRESSAO", events[0].command.content)
        self.assertTrue(events[0].command.columns > 0)

    def test_the_sector_name_travels_with_the_command(self):
        """O agente imprime "TESTE — Cozinha" na bobina; quem esta no balcao
        precisa reconhecer qual botao do painel produziu aquela via."""
        branch_id = uuid.uuid4()
        sector = PrintingSector(id=uuid.uuid4(), branch_id=branch_id, name="Cozinha", is_active=True, sort_order=0)
        service = build_service(branch_id=branch_id)
        agora = datetime.now(timezone.utc)

        events, _ = fetch_with(
            service,
            FakeStreamRepository(),
            agora,
            commands=[make_command(agora, sector_id=sector.id, branch_id=branch_id)],
            sectors=[sector],
        )

        self.assertEqual(events[0].command.printing_sector_name, "Cozinha")
        self.assertIn("Cozinha", events[0].command.content)

    def test_a_scope_without_a_branch_receives_no_command(self):
        """O painel nao tem o que fazer com um comando de impressao — e
        mandar as ordens de TODAS as lojas para um agente sem filial faria a
        via de teste de uma unidade sair na outra."""
        service = build_service(branch_id=None)
        agora = datetime.now(timezone.utc)

        events, _ = fetch_with(
            service,
            FakeStreamRepository(),
            agora,
            commands=[make_command(agora)],
        )

        self.assertEqual(events, [])

    def test_the_event_key_is_the_command_id(self):
        """Dois testes seguidos na mesma impressora sao duas ordens
        diferentes, e as duas bobinas tem que sair."""
        branch_id = uuid.uuid4()
        service = build_service(branch_id=branch_id)
        agora = datetime.now(timezone.utc)
        primeiro = make_command(agora, branch_id=branch_id)
        segundo = make_command(agora, branch_id=branch_id)

        events, _ = fetch_with(
            service, FakeStreamRepository(), agora, commands=[primeiro, segundo]
        )

        self.assertEqual(
            [event.event_key for event in events],
            [f"print-agent-command:{primeiro.id}", f"print-agent-command:{segundo.id}"],
        )

    def test_the_command_poll_uses_the_same_overlap_window(self):
        """A janela de sobreposicao vale para o comando pelo mesmo motivo que
        vale para o pedido: `created_at` e o inicio da transacao, e sem ela um
        comando gravado devagar cairia num intervalo ja varrido."""
        branch_id = uuid.uuid4()
        service = build_service(branch_id=branch_id)
        cursor = datetime.now(timezone.utc)
        command_repository = FakeCommandRepository([])
        session = FakeSession()

        with patch.object(stream_module, "SessionLocal", lambda: session):
            with patch.object(
                stream_module, "OrderRepository", lambda db: FakeStreamRepository()
            ):
                with patch.object(
                    stream_module, "PrintAgentRepository", lambda db: command_repository
                ):
                    service._fetch_events(cursor)

        _, since = command_repository.since_calls[0]
        self.assertEqual(since, cursor - timedelta(seconds=OVERLAP_SECONDS))

    def test_a_command_of_an_unknown_type_does_not_take_the_stream_down(self):
        """O poll que entrega comando e o MESMO que entrega pedido.

        `command_type` e tipado com o enum para a lista sair no
        /openapi.json, e o preco disso e que uma linha com tipo desconhecido
        — gravada por uma versao mais nova e revertida, ou a mao no banco —
        levantaria ValidationError dentro do poll. Se ela subisse, a cozinha
        pararia de imprimir por causa de um comando que ninguem consegue
        executar de qualquer jeito.
        """
        branch_id = uuid.uuid4()
        service = build_service(branch_id=branch_id)
        agora = datetime.now(timezone.utc)
        estranho = make_command(agora, branch_id=branch_id)
        estranho.command_type = "abrir_gaveta"

        events, _ = fetch_with(
            service,
            FakeStreamRepository(),
            agora,
            commands=[estranho, make_command(agora, branch_id=branch_id)],
        )

        # O desconhecido some; o bom continua entregue.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].command.command_type, "print_test")

    def test_an_order_still_arrives_when_a_command_row_is_unusable(self):
        """O caso que importa de verdade: o pedido nao pode ser refem da
        linha ruim da outra tabela."""
        branch_id = uuid.uuid4()
        service = build_service(branch_id=branch_id)
        agora = datetime.now(timezone.utc)
        estranho = make_command(agora, branch_id=branch_id)
        estranho.command_type = "abrir_gaveta"

        events, _ = fetch_with(
            service,
            FakeStreamRepository(
                created=[make_order(uuid.uuid4(), uuid.uuid4(), agora)]
            ),
            agora,
            commands=[estranho],
        )

        self.assertEqual([event.type for event in events], ["order.created"])


if __name__ == "__main__":
    unittest.main()
