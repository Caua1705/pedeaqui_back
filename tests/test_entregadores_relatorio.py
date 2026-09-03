"""`GET /admin/reports/couriers`: quanto o dono deve a cada motoboy no periodo.

O entregador ve o proprio historico pelo link dele; quem PAGA e o dono, e ate
esta rota ele teria que perguntar a cada um. O relatorio soma, por
entregador, as entregas concluidas no periodo e as taxas congeladas.

O que se prova sem banco: o recorte de datas no fuso da operacao, os
limites de periodo dos outros relatorios, a soma que separa corrida sem
taxa de corrida de R$ 0, e que o excluido continua na conta — ele foi pago
por corridas que fez.
"""

import unittest
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from src.services.admin_courier_service import AdminCourierService
from tests.rotas_do_app import caminhos


class FakeDb:
    def commit(self):
        pass

    def rollback(self):
        pass


class FakeCourierRepository:
    def __init__(self, linhas):
        self.linhas = linhas
        self.calls = []

    def totals_by_courier(self, restaurant_id, branch_id, start_at, end_at):
        self.calls.append((restaurant_id, branch_id, start_at, end_at))
        return self.linhas


def _linha(nome, entregas, sem_taxa, total, branch_id=None, deleted=False):
    return {
        "courier_id": uuid.uuid4(),
        "name": nome,
        "phone": "85999990000",
        "branch_id": branch_id or uuid.uuid4(),
        "deleted_at": datetime(2026, 9, 1, tzinfo=timezone.utc) if deleted else None,
        "deliveries_count": entregas,
        "deliveries_without_fee": sem_taxa,
        "fee_total": total,
    }


class TestARota(unittest.TestCase):
    def test_esta_registrada(self):
        self.assertIn("/admin/reports/couriers", caminhos())


class TestORelatorio(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()
        self.service = AdminCourierService(FakeDb())

    def _report(self, linhas, branch_id=None, start=date(2026, 9, 1), end=date(2026, 9, 30)):
        self.repository = FakeCourierRepository(linhas)
        self.service.courier_repository = self.repository
        return self.service.fee_report(
            restaurant_id=self.restaurant_id, start_date=start, end_date=end, branch_id=branch_id
        )

    def test_soma_por_entregador_e_no_total(self):
        report = self._report([
            _linha("Zé", 12, 0, Decimal("96.00")),
            _linha("Tonho", 5, 2, Decimal("21.00")),
        ])

        self.assertEqual([c.name for c in report.couriers], ["Zé", "Tonho"])
        self.assertEqual(report.couriers[0].fee_total, Decimal("96.00"))
        self.assertEqual(report.couriers[1].deliveries_without_fee, 2)
        self.assertEqual(report.deliveries_count, 17)
        self.assertEqual(report.deliveries_without_fee, 2)
        self.assertEqual(report.fee_total, Decimal("117.00"))

    def test_o_excluido_continua_na_conta_e_marcado(self):
        """Ele saiu, mas fez corridas no periodo e vai ser pago por elas."""
        report = self._report([_linha("Antigo", 3, 0, Decimal("21.00"), deleted=True)])

        self.assertTrue(report.couriers[0].is_deleted)
        self.assertEqual(report.fee_total, Decimal("21.00"))

    def test_sem_corrida_e_vazio_e_nao_erro(self):
        report = self._report([])

        self.assertEqual(report.couriers, [])
        self.assertEqual(report.fee_total, Decimal("0.00"))
        self.assertEqual(report.period.days, 30)

    def test_o_recorte_vai_ao_repositorio_no_fuso_da_operacao(self):
        self._report([], start=date(2026, 9, 1), end=date(2026, 9, 1))

        restaurant_id, branch_id, start_at, end_at = self.repository.calls[0]
        self.assertEqual(restaurant_id, self.restaurant_id)
        self.assertIsNone(branch_id)
        # 00:00 de 1/9 em Fortaleza (UTC-3) e 03:00 UTC; o fim e o comeco de 2/9.
        self.assertEqual(start_at.astimezone(timezone.utc), datetime(2026, 9, 1, 3, tzinfo=timezone.utc))
        self.assertEqual(end_at.astimezone(timezone.utc), datetime(2026, 9, 2, 3, tzinfo=timezone.utc))

    def test_o_recorte_de_filial_e_repassado(self):
        filial = uuid.uuid4()

        report = self._report([], branch_id=filial)

        self.assertEqual(self.repository.calls[0][1], filial)
        self.assertEqual(report.branch_id, filial)

    def test_periodo_invertido_e_longo_demais_sao_400(self):
        with self.assertRaises(HTTPException) as invertido:
            self._report([], start=date(2026, 9, 2), end=date(2026, 9, 1))
        with self.assertRaises(HTTPException) as longo:
            self._report([], start=date(2026, 1, 1), end=date(2026, 9, 1))

        self.assertEqual(invertido.exception.status_code, 400)
        self.assertEqual(longo.exception.status_code, 400)
