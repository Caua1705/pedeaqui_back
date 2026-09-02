"""O agente de impressao visto de dentro da API.

O que estes testes protegem, em ordem de gravidade:

1. **O agente NAO escolhe a filial.** Ela sai do token. Um agente que
   pudesse escolher se anunciaria como outra loja e receberia os comandos
   dela — a via de teste da Aldeota sairia no Centro.
2. **Agente sem filial e recusado.** `branch_id` nulo significa "todas as
   filiais" (`admin_scope.py`), e nao existe a maquina de todas as lojas.
   E o mesmo defeito que a revisao 0015 corrigiu no usuario do Junior; aqui
   ele responde 400 com o que fazer em vez de gravar um estado sem sentido.
3. **`is_online` e calculado, nao gravado.** Uma coluna booleana comecaria a
   mentir no segundo em que o agente caisse sem avisar.
4. **A lista de impressoras SUBSTITUI a anterior.** Impressora desinstalada
   que ficasse no seletor viraria uma escolha que nunca imprime.
5. **O teste de impressao responde "enfileirado", nao "saiu".** Por isso a
   resposta leva `agent_is_online`: sem ele o lojista aperta o botao, ve
   sucesso e fica olhando uma impressora que nao vai receber nada.

O repositorio e fake: ele prova que o parametro chegou, nao que o SQL esta
certo.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.models.printing_sector_model import PrintingSector
from src.schemas.admin_printing_schema import (
    PrintAgentHeartbeatRequest,
    PrintAgentPrinterInput,
    PrintAgentPrintersRequest,
    PrintTestRequest,
)
from src.services.print_agent_service import ONLINE_WINDOW_SECONDS, PrintAgentService
from src.models.print_agent_model import PrintAgent
from tests import fabricas


RESTAURANT_ID = uuid.uuid4()
BRANCH_ID = uuid.uuid4()
OTHER_BRANCH_ID = uuid.uuid4()


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def flush(self):
        self.events.append("flush")


class FakeAgentRepository:
    def __init__(self, agent=None, printers=()):
        self.agent = agent
        self.printers = list(printers)
        self.commands = []
        self.deleted_for = []

    def get_agent(self, branch_id):
        if self.agent is not None and self.agent.branch_id == branch_id:
            return self.agent
        return None

    def add_agent(self, agent):
        agent.id = uuid.uuid4()
        self.agent = agent
        return agent

    def list_printers(self, branch_id):
        return sorted(
            [printer for printer in self.printers if printer.branch_id == branch_id],
            key=lambda printer: (not printer.is_default, printer.name),
        )

    def delete_printers(self, branch_id):
        self.deleted_for.append(branch_id)
        antes = len(self.printers)
        self.printers = [p for p in self.printers if p.branch_id != branch_id]
        return antes - len(self.printers)

    def add_printers(self, printers):
        self.printers.extend(printers)
        return printers

    def add_command(self, command):
        command.id = uuid.uuid4()
        command.created_at = _now()
        self.commands.append(command)
        return command


class FakeBranchRepository:
    def __init__(self, branches=()):
        self.branches = list(branches)

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        for branch in self.branches:
            if branch.id == branch_id and branch.restaurant_id == restaurant_id:
                return branch
        return None


class FakeSectorRepository:
    def __init__(self, sectors=()):
        self.sectors = list(sectors)

    def get(self, sector_id, restaurant_id):
        # O repositorio real chega ao restaurante pela juncao com `branches`;
        # aqui todas as filiais de teste sao do mesmo restaurante.
        if restaurant_id != RESTAURANT_ID:
            return None
        return next((s for s in self.sectors if s.id == sector_id), None)


def _now():
    return datetime.now(timezone.utc)


def make_scope(branch_id=BRANCH_ID):
    return AdminScope(
        admin_user=fabricas.usuario_do_painel(email="agente@loja.com", role="attendant"),
        restaurant_id=RESTAURANT_ID,
        branch_id=branch_id,
    )


def make_branch(branch_id=BRANCH_ID, restaurant_id=RESTAURANT_ID):
    return fabricas.filial(id=branch_id, restaurant_id=restaurant_id)


def make_sector(name="Cozinha", branch_id=BRANCH_ID, printer_name=None):
    return PrintingSector(
        id=uuid.uuid4(),
        branch_id=branch_id,
        name=name,
        is_active=True,
        sort_order=0,
        printer_name=printer_name,
    )


def build_service(agent=None, printers=(), sectors=(), branches=None):
    service = PrintAgentService(FakeDb())
    service.repository = FakeAgentRepository(agent=agent, printers=printers)
    service.branch_repository = FakeBranchRepository(
        branches if branches is not None else [make_branch(), make_branch(OTHER_BRANCH_ID)]
    )
    service.sector_repository = FakeSectorRepository(sectors)
    return service


def make_agent(branch_id=BRANCH_ID, version="1.0.0", seconds_ago=0):
    return PrintAgent(
        id=uuid.uuid4(),
        branch_id=branch_id,
        agent_version=version,
        last_seen_at=_now() - timedelta(seconds=seconds_ago),
    )


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


class HeartbeatTests(unittest.TestCase):
    def test_the_first_beat_creates_the_row(self):
        service = build_service()

        status = service.heartbeat(make_scope(), PrintAgentHeartbeatRequest(agent_version="1.2.0"))

        self.assertEqual(status.branch_id, BRANCH_ID)
        self.assertEqual(status.agent_version, "1.2.0")
        self.assertTrue(status.is_online)
        self.assertIn("commit", service.db.events)

    def test_the_next_beat_updates_the_same_row(self):
        agent = make_agent(seconds_ago=300)
        service = build_service(agent=agent)

        service.heartbeat(make_scope(), PrintAgentHeartbeatRequest(agent_version="1.2.0"))

        self.assertIs(service.repository.agent, agent)
        self.assertEqual(agent.agent_version, "1.2.0")

    def test_a_new_version_overwrites_the_old_one(self):
        """E assim que o painel percebe que a maquina foi atualizada, sem
        ninguem avisar."""
        agent = make_agent(version="1.0.0")
        service = build_service(agent=agent)

        status = service.heartbeat(make_scope(), PrintAgentHeartbeatRequest(agent_version="2.0.0"))

        self.assertEqual(status.agent_version, "2.0.0")

    def test_the_branch_comes_from_the_token_not_from_the_body(self):
        """O corpo do heartbeat nao tem filial. Se tivesse, um agente se
        anunciaria como a loja vizinha e receberia os comandos dela."""
        self.assertNotIn("branch_id", PrintAgentHeartbeatRequest.model_fields)

    def test_an_agent_without_a_branch_is_refused(self):
        service = build_service()

        with self.assertRaises(HTTPException) as raised:
            service.heartbeat(make_scope(branch_id=None), PrintAgentHeartbeatRequest())

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("branch_id", raised.exception.detail)

    def test_a_version_that_did_not_arrive_is_accepted(self):
        """Agente antigo, anterior a esta rota, nao existe — mas um dublê de
        teste ou um curl sem corpo completo nao pode virar 500."""
        service = build_service()

        status = service.heartbeat(make_scope(), PrintAgentHeartbeatRequest())

        self.assertIsNone(status.agent_version)
        self.assertTrue(status.is_online)


# ---------------------------------------------------------------------------
# is_online
# ---------------------------------------------------------------------------


class OnlineWindowTests(unittest.TestCase):
    def test_a_recent_beat_is_online(self):
        service = build_service(agent=make_agent(seconds_ago=10))

        self.assertTrue(service.get_status(make_scope(), BRANCH_ID).is_online)

    def test_a_beat_older_than_the_window_is_offline(self):
        service = build_service(agent=make_agent(seconds_ago=ONLINE_WINDOW_SECONDS + 5))

        self.assertFalse(service.get_status(make_scope(), BRANCH_ID).is_online)

    def test_a_branch_that_never_installed_answers_200_not_404(self):
        """"Ninguem instalou aqui" e uma resposta que a tela precisa poder
        mostrar."""
        service = build_service(agent=None)

        status = service.get_status(make_scope(), BRANCH_ID)

        self.assertFalse(status.is_online)
        self.assertIsNone(status.last_seen_at)
        self.assertIsNone(status.agent_version)

    def test_the_elapsed_time_comes_along(self):
        service = build_service(agent=make_agent(seconds_ago=42))

        self.assertGreaterEqual(
            service.get_status(make_scope(), BRANCH_ID).seconds_since_last_seen, 42
        )

    def test_a_naive_timestamp_does_not_blow_up(self):
        """Linha gravada por fora (script, correcao a mao) pode chegar sem
        fuso, e subtrair ingenuo de consciente levanta TypeError — que numa
        tela de diagnostico viraria 500."""
        agent = make_agent()
        agent.last_seen_at = datetime.now(timezone.utc).replace(tzinfo=None)
        service = build_service(agent=agent)

        self.assertTrue(service.get_status(make_scope(), BRANCH_ID).is_online)


# ---------------------------------------------------------------------------
# escopo
# ---------------------------------------------------------------------------


class ScopeTests(unittest.TestCase):
    def test_another_branch_of_the_same_restaurant_is_404_for_a_bound_user(self):
        """404 e nao 403: um 403 confirmaria que aquela filial existe."""
        service = build_service()

        with self.assertRaises(HTTPException) as raised:
            service.get_status(make_scope(branch_id=BRANCH_ID), OTHER_BRANCH_ID)

        self.assertEqual(raised.exception.status_code, 404)

    def test_the_owner_reads_any_branch_of_his_restaurant(self):
        service = build_service(agent=make_agent(branch_id=OTHER_BRANCH_ID))

        status = service.get_status(make_scope(branch_id=None), OTHER_BRANCH_ID)

        self.assertEqual(status.branch_id, OTHER_BRANCH_ID)

    def test_a_branch_of_another_restaurant_is_404(self):
        service = build_service(branches=[])

        with self.assertRaises(HTTPException) as raised:
            service.get_status(make_scope(branch_id=None), BRANCH_ID)

        self.assertEqual(raised.exception.status_code, 404)


# ---------------------------------------------------------------------------
# impressoras
# ---------------------------------------------------------------------------


class PrinterReportTests(unittest.TestCase):
    def test_the_list_replaces_the_previous_one(self):
        service = build_service()
        service.report_printers(
            make_scope(),
            PrintAgentPrintersRequest(printers=[PrintAgentPrinterInput(name="VELHA")]),
        )

        resposta = service.report_printers(
            make_scope(),
            PrintAgentPrintersRequest(printers=[PrintAgentPrinterInput(name="NOVA")]),
        )

        self.assertEqual([printer.name for printer in resposta.printers], ["NOVA"])

    def test_an_empty_report_clears_the_list(self):
        """Maquina que perdeu todas as impressoras precisa esvaziar o seletor:
        deixar a lista velha faria o lojista escolher o que nao existe."""
        service = build_service()
        service.report_printers(
            make_scope(),
            PrintAgentPrintersRequest(printers=[PrintAgentPrinterInput(name="VELHA")]),
        )

        resposta = service.report_printers(make_scope(), PrintAgentPrintersRequest(printers=[]))

        self.assertEqual(resposta.printers, [])

    def test_the_default_printer_comes_first(self):
        service = build_service()

        resposta = service.report_printers(
            make_scope(),
            PrintAgentPrintersRequest(
                printers=[
                    PrintAgentPrinterInput(name="AAA", is_default=False),
                    PrintAgentPrinterInput(name="ZZZ", is_default=True),
                ]
            ),
        )

        self.assertEqual([printer.name for printer in resposta.printers], ["ZZZ", "AAA"])

    def test_surrounding_space_is_trimmed_from_the_name(self):
        """O nome tem que casar byte a byte com o do Windows. Um espaco colado
        no copiar-e-colar faz a via nao sair, e o unico sintoma e a impressora
        que nao recebeu nada."""
        entrada = PrintAgentPrinterInput(name="  EPSON TM-T20  ")

        self.assertEqual(entrada.name, "EPSON TM-T20")

    def test_an_agent_without_a_branch_cannot_report(self):
        service = build_service()

        with self.assertRaises(HTTPException) as raised:
            service.report_printers(make_scope(branch_id=None), PrintAgentPrintersRequest())

        self.assertEqual(raised.exception.status_code, 400)


# ---------------------------------------------------------------------------
# teste de impressao
# ---------------------------------------------------------------------------


class PrintTestTests(unittest.TestCase):
    def test_it_queues_a_command_for_the_branch(self):
        service = build_service()

        resposta = service.request_print_test(make_scope(), BRANCH_ID, PrintTestRequest())

        self.assertEqual(len(service.repository.commands), 1)
        self.assertEqual(service.repository.commands[0].branch_id, BRANCH_ID)
        self.assertEqual(resposta.branch_id, BRANCH_ID)

    def test_the_printer_of_the_sector_is_used_when_the_body_omits_one(self):
        sector = make_sector(printer_name="IMP-COZINHA")
        service = build_service(sectors=[sector])

        service.request_print_test(
            make_scope(), BRANCH_ID, PrintTestRequest(printing_sector_id=sector.id)
        )

        self.assertEqual(service.repository.commands[0].printer_name, "IMP-COZINHA")

    def test_the_body_printer_wins_over_the_sector(self):
        """E o caso de conferir uma maquina recem-instalada, antes de existir
        setor nenhum."""
        sector = make_sector(printer_name="IMP-COZINHA")
        service = build_service(sectors=[sector])

        service.request_print_test(
            make_scope(),
            BRANCH_ID,
            PrintTestRequest(printing_sector_id=sector.id, printer_name="IMP-NOVA"),
        )

        self.assertEqual(service.repository.commands[0].printer_name, "IMP-NOVA")

    def test_a_sector_of_another_branch_is_404(self):
        sector = make_sector(branch_id=OTHER_BRANCH_ID, printer_name="IMP-VIZINHA")
        service = build_service(sectors=[sector])

        with self.assertRaises(HTTPException) as raised:
            service.request_print_test(
                make_scope(branch_id=None),
                BRANCH_ID,
                PrintTestRequest(printing_sector_id=sector.id),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_an_unknown_sector_is_404(self):
        service = build_service(sectors=[])

        with self.assertRaises(HTTPException) as raised:
            service.request_print_test(
                make_scope(), BRANCH_ID, PrintTestRequest(printing_sector_id=uuid.uuid4())
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_it_records_who_asked(self):
        """Um teste gasta bobina e chama atencao no balcao; sem esta coluna
        nao ha como responder "quem apertou"."""
        scope = make_scope()
        service = build_service()

        service.request_print_test(scope, BRANCH_ID, PrintTestRequest())

        self.assertEqual(
            service.repository.commands[0].created_by_admin_user_id, scope.admin_user.id
        )

    def test_the_answer_says_whether_the_agent_is_listening(self):
        """Sem isto o lojista aperta o botao, ve sucesso e fica olhando uma
        impressora que nao vai receber nada porque o agente esta desligado
        desde ontem."""
        offline = build_service(agent=make_agent(seconds_ago=ONLINE_WINDOW_SECONDS + 60))
        online = build_service(agent=make_agent(seconds_ago=1))

        self.assertFalse(
            offline.request_print_test(make_scope(), BRANCH_ID, PrintTestRequest()).agent_is_online
        )
        self.assertTrue(
            online.request_print_test(make_scope(), BRANCH_ID, PrintTestRequest()).agent_is_online
        )

    def test_a_branch_outside_the_scope_is_404(self):
        service = build_service()

        with self.assertRaises(HTTPException) as raised:
            service.request_print_test(
                make_scope(branch_id=BRANCH_ID), OTHER_BRANCH_ID, PrintTestRequest()
            )

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
