"""O que o agente avisa para o icone da bandeja.

Sem console, o `AgentListener` e o UNICO caminho entre "o agente sabe" e "o
lojista ve". Uma chamada que deixe de acontecer nao quebra teste nenhum do
laco de impressao — a comanda continua saindo — e o efeito e o icone parado
na cor errada, ou a caixa de aviso que nunca aparece. E o modo de falhar
mais caro que existe aqui: silencio que parece funcionamento.

Por isso o listener e testado sozinho, e por evento.

A bandeja de verdade nao entra: `pystray` precisa de Windows com area de
trabalho. O que se prova aqui e que o agente CHAMA; que o `pystray` pinta,
so a maquina do balcao responde.
"""

import unittest
import uuid
from pathlib import Path
import tempfile

from print_agent.agent import AgentListener, PrintAgent
from print_agent.api_client import ApiError, AuthFatalError
from print_agent.config import Config
from print_agent.printers import Printer, PrinterError
from print_agent.sse import SseEvent
from print_agent.state import PrintedOrders


class RecordingListener(AgentListener):
    def __init__(self):
        self.connected = 0
        self.disconnected = []
        self.fatal = []
        self.printed = []
        self.printer_problems = []

    def on_connected(self):
        self.connected += 1

    def on_disconnected(self, reason):
        self.disconnected.append(reason)

    def on_fatal(self, reason):
        self.fatal.append(reason)

    def on_order_printed(self, label):
        self.printed.append(label)

    def on_printer_problem(self, printer_name, detail):
        self.printer_problems.append((printer_name, detail))


class FakePrinter(Printer):
    def __init__(self, failing_printers=()):
        self.sent = []
        self.failing_printers = set(failing_printers)

    def send(self, printer_name, payload, job_name):
        if printer_name in self.failing_printers:
            raise PrinterError(f"'{printer_name}' esta sem papel")
        self.sent.append((printer_name, payload, job_name))


class FakeClient:
    def __init__(self, jobs_by_order=None, stream_error=None):
        self.jobs_by_order = jobs_by_order or {}
        self.stream_error = stream_error

    def print_jobs(self, order_id):
        return self.jobs_by_order.get(order_id, [])

    def heartbeat(self, agent_version):
        return {}

    def report_printers(self, printers):
        return {}

    def open_stream(self, last_event_id=None):
        if self.stream_error:
            raise self.stream_error
        return iter([])


def make_job(sector="Cozinha", printer_name=None, content="COMANDA"):
    return {
        "type": "production",
        "sector_name": sector,
        "printer_name": printer_name,
        "columns": 24,
        "font_size": "normal",
        "content": content,
    }


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.listener = RecordingListener()
        self.printer = FakePrinter()

    def _agent(self, client, printers=None, default_printer="Balcao"):
        config = Config(
            api_base_url="https://api.exemplo.com",
            token="t",
            email=None,
            password=None,
            printers=printers or {},
            default_printer=default_printer,
            state_file=Path(self.tmp.name) / "estado.json",
            log_file=Path(self.tmp.name) / "agente.log",
            reconnect_min_seconds=0.0,
            reconnect_max_seconds=0.0,
            # Uma tentativa so: o que se prova aqui e o aviso, nao o retry
            # (que test_agent.py ja cobre). Com o padrao de 3 x 5s, cada
            # teste de falha de impressora custaria 10 segundos de espera.
            print_attempts=1,
            print_retry_seconds=0.0,
        )
        return PrintAgent(
            config=config,
            client=client,
            printer=self.printer,
            state=PrintedOrders(config.state_file, config.state_retention_days),
            listener=self.listener,
        )

    def _order_event(self, order_id):
        return SseEvent(
            event="order.status_changed",
            data='{"order": {"id": "%s", "order_number": 5471, "status": "accepted"}}'
            % order_id,
            event_id="1",
        )

    def test_comanda_completa_avisa_para_o_som_tocar(self):
        """O som de pedido novo pendura AQUI, e so quando TUDO saiu.

        Avisar antes de a ultima via sair mandaria alguem ate a impressora
        pegar meia comanda.
        """
        order_id = str(uuid.uuid4())
        agent = self._agent(FakeClient({order_id: [make_job(), make_job("Bar")]}))

        agent._handle_event(self._order_event(order_id))

        self.assertEqual(self.listener.printed, ["#5471"])

    def test_comanda_incompleta_nao_avisa(self):
        order_id = str(uuid.uuid4())
        self.printer.failing_printers = {"Bar"}
        agent = self._agent(
            FakeClient({order_id: [make_job(printer_name="Balcao"), make_job(printer_name="Bar")]})
        )

        agent._handle_event(self._order_event(order_id))

        self.assertEqual(self.listener.printed, [])

    def test_impressora_que_recusou_vira_aviso_com_o_nome_dela(self):
        """O nome tem que estar na mensagem: e o unico dado acionavel.

        "Nao imprimiu" nao diz a ninguem em qual das tres impressoras da loja
        olhar.
        """
        order_id = str(uuid.uuid4())
        self.printer.failing_printers = {"Cozinha"}
        agent = self._agent(FakeClient({order_id: [make_job(printer_name="Cozinha")]}))

        agent._handle_event(self._order_event(order_id))

        self.assertEqual(len(self.listener.printer_problems), 1)
        printer_name, detail = self.listener.printer_problems[0]
        self.assertEqual(printer_name, "Cozinha")
        self.assertIn("Cozinha", detail)

    def test_setor_sem_impressora_avisa_com_nome_vazio(self):
        """Nome vazio distingue "a impressora recusou" de "nao ha impressora".

        Sao dois consertos diferentes: um e papel/cabo, o outro e o painel ou
        o config.ini.
        """
        order_id = str(uuid.uuid4())
        agent = self._agent(
            FakeClient({order_id: [make_job(sector="Chapa")]}), default_printer=None
        )

        agent._handle_event(self._order_event(order_id))

        self.assertEqual(len(self.listener.printer_problems), 1)
        printer_name, detail = self.listener.printer_problems[0]
        self.assertEqual(printer_name, "")
        self.assertIn("Chapa", detail)

    def test_credencial_sem_conserto_avisa_fatal_uma_vez_e_para(self):
        agent = self._agent(FakeClient(stream_error=AuthFatalError("senha invalida")))

        finished_cleanly = agent.run()

        self.assertFalse(finished_cleanly)
        self.assertEqual(self.listener.fatal, ["senha invalida"])
        self.assertEqual(self.listener.disconnected, [])

    def test_queda_de_rede_avisa_desconexao_e_nao_fatal(self):
        """Amarelo, nao vermelho: o agente volta sozinho.

        Pintar de vermelho aqui faria o lojista ligar para o suporte por uma
        oscilacao de internet que se resolve em dois segundos.
        """
        agent = self._agent(FakeClient(stream_error=ApiError("conexao recusada")))
        # Sem isto o laco tentaria para sempre: a desconexao nao e fatal.
        agent._sleep = lambda seconds: agent.stop() or False

        agent.run()

        self.assertEqual(self.listener.fatal, [])
        self.assertIn("conexao recusada", self.listener.disconnected)

    def test_sem_listener_o_agente_roda_igual(self):
        """O padrao e o `AgentListener` vazio.

        E o que mantem `python -m print_agent --no-tray`, os testes e qualquer
        maquina sem `pystray` rodando sem um `if listener` em cada evento.
        """
        order_id = str(uuid.uuid4())
        config = Config(
            api_base_url="https://api.exemplo.com",
            token="t",
            email=None,
            password=None,
            default_printer="Balcao",
            state_file=Path(self.tmp.name) / "sem-listener.json",
        )
        agent = PrintAgent(
            config=config,
            client=FakeClient({order_id: [make_job()]}),
            printer=self.printer,
            state=PrintedOrders(config.state_file, config.state_retention_days),
        )

        agent._handle_event(self._order_event(order_id))

        self.assertEqual(len(self.printer.sent), 1)


if __name__ == "__main__":
    unittest.main()
