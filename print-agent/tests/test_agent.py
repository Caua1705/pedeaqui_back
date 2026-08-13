"""O laco do agente: evento -> vias -> impressora.

O que estes testes protegem, em ordem de gravidade:

1. **Nao reimprimir.** O stream entrega ao menos uma vez e o servidor fecha
   a conexao a cada 15 minutos; repetido e o caso normal, nao a excecao.
2. **So imprimir o que foi ACEITO.** Pedido pendente na tela do lojista nao
   pode chegar na chapa.
3. **Cada via na impressora do seu setor**, com a fonte que a API pediu.
4. **Pedido que saiu pela metade nao e marcado como impresso.** Meia comanda
   e pior que nenhuma: a cozinha prepara o que viu e o resto some.
5. **O agente nao reformata nada.** O `content` que a API mandou chega na
   impressora do jeito que veio.

A impressora e o cliente da API sao dublês. O que fica para o teste manual
(descrito no README) e que o Windows aceita os bytes.
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path

from print_agent.agent import PrintAgent
from print_agent.api_client import ApiError
from print_agent.config import Config
from print_agent.printers import Printer, PrinterError
from print_agent.sse import SseEvent
from print_agent.state import PrintedOrders


class FakePrinter(Printer):
    def __init__(self, failing_printers=()):
        self.sent = []
        self.failing_printers = set(failing_printers)

    def send(self, printer_name, payload, job_name):
        if printer_name in self.failing_printers:
            raise PrinterError(f"'{printer_name}' esta sem papel")
        self.sent.append((printer_name, payload, job_name))


class FakeClient:
    def __init__(self, jobs_by_order=None, error=None, heartbeat_error=None):
        self.jobs_by_order = jobs_by_order or {}
        self.error = error
        self.heartbeat_error = heartbeat_error
        self.calls = []
        self.heartbeats = []
        self.reported_printers = []

    def print_jobs(self, order_id):
        self.calls.append(order_id)
        if self.error:
            raise self.error
        return self.jobs_by_order.get(order_id, [])

    def heartbeat(self, agent_version):
        if self.heartbeat_error:
            raise self.heartbeat_error
        self.heartbeats.append(agent_version)
        return {}

    def report_printers(self, printers):
        self.reported_printers.append(printers)
        return {}


def make_job(sector="Cozinha", content="COMANDA", font_size="large", printer_name=None):
    return {
        "type": "production",
        "sector_id": None,
        "sector_name": sector,
        "printer_name": printer_name,
        "columns": 24,
        "font_size": font_size,
        "content": content,
    }


def make_command_event(
    command_id=None,
    content="TESTE",
    printer_name=None,
    sector_name="Cozinha",
):
    command_id = command_id or str(uuid.uuid4())
    return SseEvent(
        event="print_agent.command",
        event_id="2026-08-12T10:00:00+00:00",
        data=json.dumps({
            "type": "print_agent.command",
            "event_key": f"print-agent-command:{command_id}",
            "occurred_at": "2026-08-12T10:00:00+00:00",
            "command": {
                "command_id": command_id,
                "command_type": "print_test",
                "branch_id": str(uuid.uuid4()),
                "printer_name": printer_name,
                "printing_sector_id": None,
                "printing_sector_name": sector_name,
                "content": content,
                "columns": 24,
                "font_size": "large",
            },
        }),
    )


def make_event(order_id, status="accepted", event="order.status_changed", number=1234):
    return SseEvent(
        event=event,
        event_id="2026-08-09T17:32:00+00:00",
        data=json.dumps({
            "type": event,
            "event_key": f"status-changed:{uuid.uuid4()}",
            "occurred_at": "2026-08-09T17:32:00+00:00",
            "order": {"id": order_id, "order_number": number, "status": status},
        }),
    )


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "printed.json"

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, client, printer, printers=None, default_printer=None, attempts=1):
        config = Config(
            api_base_url="https://api.exemplo.com",
            token="t",
            email=None,
            password=None,
            printers=printers if printers is not None else {"cozinha": "IMP-COZINHA"},
            default_printer=default_printer,
            state_file=self.state_path,
            print_attempts=attempts,
            print_retry_seconds=0,
        )
        return PrintAgent(
            config=config,
            client=client,
            printer=printer,
            state=PrintedOrders(self.state_path),
        )


class TriggerTests(AgentTestCase):
    def test_an_accepted_order_is_printed(self):
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job()]})
        printer = FakePrinter()
        agent = self.build(client, printer)

        agent._handle_event(make_event(order_id))

        self.assertEqual(len(printer.sent), 1)
        self.assertEqual(printer.sent[0][0], "IMP-COZINHA")

    def test_a_pending_order_is_not_printed(self):
        # Pedido que o lojista ainda nao assumiu nao pode chegar na chapa.
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job()]})
        agent = self.build(client, FakePrinter())

        agent._handle_event(make_event(order_id, status="pending"))

        self.assertEqual(client.calls, [])

    def test_a_cancelled_order_is_not_printed(self):
        order_id = str(uuid.uuid4())
        agent = self.build(FakeClient(), FakePrinter())

        agent._handle_event(make_event(order_id, status="cancelled"))

        self.assertEqual(agent.client.calls, [])

    def test_an_order_created_already_accepted_is_printed(self):
        # Hoje o pedido nasce em `pending`, mas escutar os dois eventos e
        # mais barato que descobrir a mudanca com a cozinha parada.
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job()]})
        printer = FakePrinter()
        agent = self.build(client, printer)

        agent._handle_event(make_event(order_id, event="order.created"))

        self.assertEqual(len(printer.sent), 1)

    def test_the_sync_required_warning_prints_nothing(self):
        agent = self.build(FakeClient(), FakePrinter())

        agent._handle_event(SseEvent(event="sync_required", data='{"type":"sync_required"}'))

        self.assertEqual(agent.client.calls, [])

    def test_an_event_without_an_order_is_ignored(self):
        agent = self.build(FakeClient(), FakePrinter())

        agent._handle_event(SseEvent(event="order.created", data='{"type":"order.created"}'))

        self.assertEqual(agent.client.calls, [])


class DeduplicationTests(AgentTestCase):
    def test_the_same_order_is_not_printed_twice(self):
        # O caso central: o stream repete de proposito e o servidor fecha a
        # conexao a cada 15 minutos.
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job()]})
        printer = FakePrinter()
        agent = self.build(client, printer)

        agent._handle_event(make_event(order_id))
        agent._handle_event(make_event(order_id))

        self.assertEqual(len(printer.sent), 1)
        self.assertEqual(len(client.calls), 1)

    def test_a_restarted_agent_does_not_reprint_the_day(self):
        order_id = str(uuid.uuid4())
        first = self.build(FakeClient({order_id: [make_job()]}), FakePrinter())
        first._handle_event(make_event(order_id))

        printer = FakePrinter()
        second = self.build(FakeClient({order_id: [make_job()]}), printer)
        second._handle_event(make_event(order_id))

        self.assertEqual(printer.sent, [])


class RoutingTests(AgentTestCase):
    def test_each_copy_goes_to_the_printer_of_its_sector(self):
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [
            make_job(sector="Via do cliente", font_size="normal"),
            make_job(sector="Cozinha"),
            make_job(sector="Bar"),
        ]})
        printer = FakePrinter()
        agent = self.build(client, printer, printers={
            "via do cliente": "IMP-CAIXA",
            "cozinha": "IMP-COZINHA",
            "bar": "IMP-BAR",
        })

        agent._handle_event(make_event(order_id))

        self.assertEqual(
            [name for name, _, _ in printer.sent],
            ["IMP-CAIXA", "IMP-COZINHA", "IMP-BAR"],
        )

    def test_the_sector_name_is_matched_without_accent_or_case(self):
        # O nome vem do painel, digitado por gente. "Praça Quente" tem que
        # achar a linha `praca quente` do config.ini.
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job(sector="Praça Quente")]})
        printer = FakePrinter()
        agent = self.build(client, printer, printers={"praca quente": "IMP-QUENTE"})

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-QUENTE")

    def test_an_unmapped_sector_falls_back_to_the_default(self):
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job(sector="Setor Novo")]})
        printer = FakePrinter()
        agent = self.build(client, printer, printers={}, default_printer="IMP-PADRAO")

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-PADRAO")

    def test_an_unmapped_sector_without_a_default_is_a_failure(self):
        # Setor criado no painel depois da instalacao. Nao pode passar em
        # silencio: e uma via que nao vai sair.
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job(sector="Setor Novo")]})
        printer = FakePrinter()
        agent = self.build(client, printer, printers={}, default_printer=None)

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent, [])
        self.assertEqual(agent.stats.failed_jobs, 1)
        self.assertNotIn(order_id, agent.state)


class ContentTests(AgentTestCase):
    def test_the_agent_does_not_reformat_the_content(self):
        # A regra que justifica o agente existir deste jeito: a formatacao e
        # unica e vive na API.
        order_id = str(uuid.uuid4())
        content = "2x PRATO FEITO\n  > Acompanhamento:\n    Espaguete"
        client = FakeClient({order_id: [make_job(content=content)]})
        printer = FakePrinter()
        agent = self.build(client, printer)

        agent._handle_event(make_event(order_id))

        payload = printer.sent[0][1]
        for line in content.split("\n"):
            self.assertIn(line.encode("cp850"), payload)

    def test_a_copy_without_content_is_a_failure(self):
        order_id = str(uuid.uuid4())
        job = make_job()
        job["content"] = ""
        client = FakeClient({order_id: [job]})
        agent = self.build(client, FakePrinter())

        agent._handle_event(make_event(order_id))

        self.assertEqual(agent.stats.failed_jobs, 1)
        self.assertNotIn(order_id, agent.state)


class FailureTests(AgentTestCase):
    def test_an_order_printed_in_half_is_not_marked_as_printed(self):
        # Marcado, ele nunca mais tentaria — e a cozinha prepararia so o que
        # viu.
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [
            make_job(sector="Cozinha"),
            make_job(sector="Bar"),
        ]})
        printer = FakePrinter(failing_printers=["IMP-BAR"])
        agent = self.build(client, printer, printers={
            "cozinha": "IMP-COZINHA",
            "bar": "IMP-BAR",
        })

        agent._handle_event(make_event(order_id))

        self.assertEqual(len(printer.sent), 1)
        self.assertNotIn(order_id, agent.state)

    def test_a_failed_copy_is_retried_before_giving_up(self):
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job()]})
        printer = FakePrinter(failing_printers=["IMP-COZINHA"])
        agent = self.build(client, printer, attempts=3)

        agent._handle_event(make_event(order_id))

        self.assertEqual(agent.stats.failed_jobs, 1)

    def test_an_api_failure_does_not_mark_the_order(self):
        # A API fora do ar por um instante nao pode custar a comanda: sem a
        # marca, a proxima repeticao do evento tenta de novo.
        order_id = str(uuid.uuid4())
        client = FakeClient(error=ApiError("timeout"))
        agent = self.build(client, FakePrinter())

        agent._handle_event(make_event(order_id))

        self.assertNotIn(order_id, agent.state)

    def test_an_order_with_no_copies_is_not_marked(self):
        # Acontece com pedido cujo pagamento online nao confirmou: a API
        # devolve so a via do cliente. Lista vazia mesmo e sinal de que algo
        # esta errado, entao o pedido continua elegivel.
        order_id = str(uuid.uuid4())
        agent = self.build(FakeClient({order_id: []}), FakePrinter())

        agent._handle_event(make_event(order_id))

        self.assertNotIn(order_id, agent.state)


class PrinterChoiceTests(AgentTestCase):
    """De onde sai a impressora de cada via.

    O painel vence o config.ini, e a razao e um defeito concreto: o
    config.ini casa pelo NOME do setor, entao renomear "Cozinha" no painel
    fazia a via cair na impressora padrao e a comanda da cozinha comecar a
    sair no balcao — sem erro em lugar nenhum.
    """

    def test_the_printer_chosen_in_the_panel_wins(self):
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job(printer_name="IMP-DO-PAINEL")]})
        printer = FakePrinter()
        agent = self.build(client, printer, printers={"cozinha": "IMP-DO-INI"})

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-DO-PAINEL")

    def test_without_a_panel_choice_the_ini_still_answers(self):
        """A instalacao anterior a coluna `printer_name` nao pode parar de
        imprimir por causa dela."""
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job(printer_name=None)]})
        printer = FakePrinter()
        agent = self.build(client, printer, printers={"cozinha": "IMP-DO-INI"})

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-DO-INI")

    def test_a_renamed_sector_still_prints_when_the_panel_says_where(self):
        """O caso que a coluna existe para consertar: o setor foi renomeado
        no painel e o config.ini nao sabe. Antes, isto caia na padrao."""
        order_id = str(uuid.uuid4())
        client = FakeClient(
            {order_id: [make_job(sector="Cozinha Quente", printer_name="IMP-COZINHA")]}
        )
        printer = FakePrinter()
        agent = self.build(client, printer, printers={"cozinha": "IMP-COZINHA"})

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-COZINHA")


class CommandTests(AgentTestCase):
    def test_a_print_test_command_prints_the_content_the_api_sent(self):
        printer = FakePrinter()
        agent = self.build(FakeClient(), printer)

        agent._handle_event(make_command_event(content="TESTE DE IMPRESSAO"))

        self.assertEqual(len(printer.sent), 1)
        self.assertIn(b"TESTE DE IMPRESSAO", printer.sent[0][1])

    def test_the_command_goes_to_the_printer_the_panel_chose(self):
        printer = FakePrinter()
        agent = self.build(FakeClient(), printer, printers={"cozinha": "IMP-DO-INI"})

        agent._handle_event(make_command_event(printer_name="IMP-ESCOLHIDA"))

        self.assertEqual(printer.sent[0][0], "IMP-ESCOLHIDA")

    def test_without_a_printer_it_falls_back_to_the_ini(self):
        printer = FakePrinter()
        agent = self.build(FakeClient(), printer, printers={"cozinha": "IMP-DO-INI"})

        agent._handle_event(make_command_event(printer_name=None, sector_name="Cozinha"))

        self.assertEqual(printer.sent[0][0], "IMP-DO-INI")

    def test_the_same_command_does_not_print_twice(self):
        """O replay do stream volta ate uma hora. Sem a memoria, um teste
        apertado as 14h sairia de novo a cada reconexao."""
        printer = FakePrinter()
        agent = self.build(FakeClient(), printer)
        command_id = str(uuid.uuid4())

        agent._handle_event(make_command_event(command_id=command_id))
        agent._handle_event(make_command_event(command_id=command_id))

        self.assertEqual(len(printer.sent), 1)

    def test_a_command_that_fails_to_print_is_still_marked(self):
        """Assimetria deliberada com o pedido: pedido incompleto e tentado de
        novo (a cozinha nao pode perder um item), mas via de teste repetida a
        cada reconexao so gasta bobina."""
        printer = FakePrinter(failing_printers={"IMP-ESCOLHIDA"})
        agent = self.build(FakeClient(), printer)
        command_id = str(uuid.uuid4())

        agent._handle_event(
            make_command_event(command_id=command_id, printer_name="IMP-ESCOLHIDA")
        )

        self.assertEqual(printer.sent, [])
        self.assertIn(f"cmd:{command_id}", agent.state)

    def test_a_command_without_content_is_ignored(self):
        printer = FakePrinter()
        agent = self.build(FakeClient(), printer)

        agent._handle_event(make_command_event(content=""))

        self.assertEqual(printer.sent, [])

    def test_a_command_without_a_printer_anywhere_does_not_crash(self):
        printer = FakePrinter()
        agent = self.build(FakeClient(), printer, printers={}, default_printer=None)

        agent._handle_event(make_command_event(printer_name=None))

        self.assertEqual(printer.sent, [])

    def test_an_order_is_not_confused_with_a_command(self):
        """As chaves do arquivo de estado convivem: pedido cru, comando com
        prefixo. Sem o prefixo, um uuid poderia (em tese) valer pelos dois."""
        order_id = str(uuid.uuid4())
        client = FakeClient({order_id: [make_job()]})
        agent = self.build(client, FakePrinter())

        agent._handle_event(make_command_event(command_id=order_id))
        agent._handle_event(make_event(order_id))

        self.assertIn(order_id, agent.state)
        self.assertIn(f"cmd:{order_id}", agent.state)


class HeartbeatTests(AgentTestCase):
    def test_the_first_line_of_the_stream_already_beats(self):
        """Zero, e nao "agora", no estado inicial: o painel nao pode mostrar
        offline por meio minuto depois de alguem ligar a maquina — que e
        exatamente quando estao olhando a tela."""
        client = FakeClient()
        agent = self.build(client, FakePrinter())

        list(agent._with_heartbeat([": ping"]))

        self.assertEqual(len(client.heartbeats), 1)

    def test_it_does_not_beat_on_every_line(self):
        client = FakeClient()
        agent = self.build(client, FakePrinter())

        list(agent._with_heartbeat([": ping"] * 50))

        self.assertEqual(len(client.heartbeats), 1)

    def test_a_failed_heartbeat_does_not_stop_the_stream(self):
        """O heartbeat e informacao para o painel. Uma tela que mostra
        "offline" e infinitamente melhor que um agente que parou de imprimir
        porque nao conseguiu se anunciar."""
        client = FakeClient(heartbeat_error=ApiError("servidor fora"))
        agent = self.build(client, FakePrinter())

        entregues = list(agent._with_heartbeat(["a", "b", "c"]))

        self.assertEqual(entregues, ["a", "b", "c"])

    def test_the_lines_pass_through_untouched(self):
        agent = self.build(FakeClient(), FakePrinter())

        self.assertEqual(list(agent._with_heartbeat(["id: 1", "", "data: {}"])),
                         ["id: 1", "", "data: {}"])


class OldBackendTests(AgentTestCase):
    """O agente da frente 4 contra um backend que ainda NAO tem as rotas dela.

    Nao e hipotese: o agente e instalado a mao na maquina do balcao e o
    backend sobe por deploy. As duas coisas nao andam juntas, e a ordem em que
    andam nao e escolhida por ninguem — instalar o agente novo numa loja antes
    do deploy e o caso normal, nao a excecao.

    A unica coisa que precisa continuar valendo e esta: **a comanda sai**. O
    heartbeat e a lista de impressoras sao informacao para o painel, e o painel
    velho nao tem onde mostra-las de qualquer jeito.
    """

    def test_the_order_still_prints_when_the_new_routes_answer_404(self):
        order_id = str(uuid.uuid4())
        client = FakeClient(
            {order_id: [make_job()]}, heartbeat_error=ApiError("POST /heartbeat respondeu 404")
        )
        printer = FakePrinter()
        agent = self.build(client, printer, printers={"cozinha": "IMP-COZINHA"})

        list(agent._with_heartbeat([": ping"]))
        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-COZINHA")

    def test_a_failed_printer_report_does_not_interrupt_anything(self):
        client = FakeClient()
        client.report_printers = lambda printers: (_ for _ in ()).throw(
            ApiError("POST /printers respondeu 404")
        )
        agent = self.build(client, FakePrinter())

        agent._report_printers()  # nao pode levantar

    def test_a_job_without_the_printer_name_key_falls_back_to_the_ini(self):
        """O backend antigo nao manda a chave — nem nula, AUSENTE. E por isso
        que a leitura e `.get()`: com indexacao, toda via de um backend
        anterior a frente 4 viraria KeyError e a cozinha pararia."""
        order_id = str(uuid.uuid4())
        job = make_job()
        del job["printer_name"]

        printer = FakePrinter()
        agent = self.build(
            FakeClient({order_id: [job]}), printer, printers={"cozinha": "IMP-COZINHA"}
        )

        agent._handle_event(make_event(order_id))

        self.assertEqual(printer.sent[0][0], "IMP-COZINHA")


class StopTests(AgentTestCase):
    def test_stop_ends_the_loop(self):
        agent = self.build(FakeClient(), FakePrinter())
        agent.stop()

        # `_sleep` devolve False quando o agente foi parado, e e isso que
        # tira o laco principal do ar sem esperar o backoff inteiro.
        self.assertFalse(agent._sleep(30))


if __name__ == "__main__":
    unittest.main()
