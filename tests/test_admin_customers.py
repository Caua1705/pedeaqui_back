"""Clientes do painel (BLOCO D da Fase 3).

O teste que mais importa aqui e o primeiro: a consulta nao pode encostar na
tabela `customers`. A conta do cliente e global da plataforma, entao um
SELECT no cadastro entregaria ao lojista tambem quem nunca pediu na loja
dele — inclusive cliente de concorrente que usa a mesma plataforma. E
requisito de seguranca, nao de feature, e por isso esta escrito como teste
e nao so como comentario.
"""

import unittest
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from src.api.dependencies.admin_scope import AdminScope
from src.repositories.admin_customer_repository import (
    AdminCustomerRepository,
    _build_customer_search_condition,
)
from src.schemas.admin_customer_schema import CustomerSegment
from src.services.admin_customer_service import AdminCustomerService


RESTAURANT_ID = uuid.uuid4()
NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)


def compile_sql(statement) -> str:
    """SQL como o Postgres vai receber.

    Compilar no dialeto certo importa: o DISTINCT ON e o FILTER usados aqui
    so existem no Postgres, e o dialeto generico os descarta silenciosamente.
    """
    return str(statement.compile(dialect=postgresql.dialect()))


def make_row(**overrides):
    values = {
        "customer_phone": "85999990000",
        "orders_count": 3,
        "billable_orders_count": 3,
        "total_spent": Decimal("150.00"),
        # Estes tres vem CALCULADOS do banco desde 21/08/2026 — a linha falsa
        # os traz prontos porque a consulta os traria. Quem prova o valor
        # deles e `test_clientes_rfv_db.py`, contra o Postgres; o que se prova
        # aqui e que eles chegam ao contrato sem ninguem recalcular no meio.
        "average_ticket": Decimal("50.00"),
        "days_since_last_order": 0,
        "segment": "fiel",
        "cadence_days": 30.0,
        "first_order_at": NOW,
        "last_order_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingCustomerRepository:
    def __init__(self, rows=(), names=None):
        self.rows = list(rows)
        self.names = names or {}
        self.list_kwargs = None
        self.count_kwargs = None
        self.name_phones = None

    def list_customers(self, **kwargs):
        self.list_kwargs = kwargs
        return self.rows

    def count_customers(self, **kwargs):
        self.count_kwargs = kwargs
        return len(self.rows)

    def get_latest_names(self, restaurant_id, phones):
        self.name_phones = phones
        return self.names


def build_service(repository):
    service = AdminCustomerService(SimpleNamespace())
    service.repository = repository
    return service


def scope(branch_id=None):
    return AdminScope(admin_user=None, restaurant_id=RESTAURANT_ID, branch_id=branch_id)


class CapturingDb:
    """Guarda o SELECT montado em vez de executa-lo.

    E o que permite conferir o SQL de verdade sem subir Postgres: o
    statement compilado e exatamente o que iria para o banco.
    """

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    def scalar(self, statement):
        self.statements.append(statement)
        return 0


class DataSourceTests(unittest.TestCase):
    def test_the_query_never_reads_the_customers_table(self):
        db = CapturingDb()
        repository = AdminCustomerRepository(db)
        repository.list_customers(RESTAURANT_ID, NOW, search="maria")
        repository.count_customers(RESTAURANT_ID, NOW)
        repository.get_latest_names(RESTAURANT_ID, ["85999990000"])

        for statement in db.statements:
            sql = compile_sql(statement)
            self.assertIn("FROM orders", sql)
            # A conta do cliente e global: qualquer leitura de `customers`
            # aqui entregaria ao lojista quem nunca pediu na loja dele.
            self.assertNotIn("FROM customers", sql)
            self.assertNotIn("JOIN customers", sql)

    def test_total_spent_ignores_cancelled_and_rejected(self):
        db = CapturingDb()
        AdminCustomerRepository(db).list_customers(RESTAURANT_ID, NOW)

        sql = compile_sql(db.statements[0])
        # Pedido cancelado nao e dinheiro que entrou; somar faria o cliente
        # que mais desistiu parecer o melhor.
        self.assertIn("FILTER", sql.upper())

    def test_contract_does_not_expose_account_fields(self):
        from src.schemas.admin_customer_schema import AdminCustomerListItem

        # E-mail, CPF e o id de cadastro sao da conta global; o lojista e
        # dono do que o cliente informou ao pedir na loja dele.
        for field in ("email", "cpf", "customer_id", "birth_date"):
            self.assertNotIn(field, AdminCustomerListItem.model_fields)


class ListingTests(unittest.TestCase):
    def test_restaurant_comes_from_the_token(self):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope())

        self.assertEqual(repository.list_kwargs["restaurant_id"], RESTAURANT_ID)
        self.assertEqual(repository.count_kwargs["restaurant_id"], RESTAURANT_ID)

    def test_branch_bound_user_is_filtered_even_without_asking(self):
        branch_id = uuid.uuid4()
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(branch_id=branch_id))

        self.assertEqual(repository.list_kwargs["branch_id"], branch_id)

    def test_branch_filter_only_narrows(self):
        own_branch = uuid.uuid4()
        repository = RecordingCustomerRepository()
        with self.assertRaises(Exception) as raised:
            build_service(repository).list_customers(
                scope(branch_id=own_branch), branch_id=uuid.uuid4()
            )

        # Pedir a filial vizinha na querystring nao amplia o escopo.
        self.assertEqual(raised.exception.status_code, 404)

    def test_name_comes_from_the_most_recent_order(self):
        row = make_row(customer_phone="85999990000")
        repository = RecordingCustomerRepository(
            rows=[row], names={"85999990000": "Maria Silva"}
        )
        response = build_service(repository).list_customers(scope())

        self.assertEqual(response.items[0].customer_name, "Maria Silva")
        self.assertEqual(repository.name_phones, ["85999990000"])

    def test_pagination_envelope_is_filled(self):
        repository = RecordingCustomerRepository(rows=[make_row()])
        response = build_service(repository).list_customers(scope(), limit=10, offset=20)

        self.assertEqual((response.limit, response.offset, response.total), (10, 20, 1))

    def test_blank_search_becomes_none(self):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(), search="   ")

        self.assertIsNone(repository.list_kwargs["search"])

    def test_search_is_trimmed_and_capped(self):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(), search="  " + "a" * 500)

        self.assertEqual(len(repository.list_kwargs["search"]), 120)


class ContratoDaLinhaTests(unittest.TestCase):
    """A linha da consulta vira linha do contrato SEM ninguem recalcular.

    Desde 21/08/2026 `segment`, `average_ticket` e `days_since_last_order`
    sao colunas da consulta. Se alguem reintroduzir uma conta em Python aqui,
    ela vai discordar do SQL que os filtros usam — e a tela passa a mostrar um
    rotulo que o `?segment=` nao encontra.
    """

    def test_os_tres_campos_calculados_vem_da_linha(self):
        row = make_row(
            average_ticket=Decimal("41.67"),
            days_since_last_order=23,
            segment="em_risco",
        )
        item = build_service(
            RecordingCustomerRepository(rows=[row])
        ).list_customers(scope()).items[0]

        self.assertEqual(item.average_ticket, 41.67)
        self.assertEqual(item.days_since_last_order, 23)
        self.assertEqual(item.segment, CustomerSegment.EM_RISCO)

    def test_os_dois_contadores_vao_no_contrato(self):
        """Sem `billable_orders_count` na resposta, a tela mostra tres numeros
        que nao fecham e o lojista abre chamado."""
        row = make_row(orders_count=5, billable_orders_count=3, total_spent=Decimal("150.00"))
        item = build_service(
            RecordingCustomerRepository(rows=[row])
        ).list_customers(scope()).items[0]

        self.assertEqual((item.orders_count, item.billable_orders_count), (5, 3))
        self.assertEqual(item.total_spent, 150.00)

    def test_o_segmento_serializa_como_o_codigo_estavel(self):
        """O painel le a string do JSON, e nao o nome do membro do enum."""
        response = build_service(
            RecordingCustomerRepository(rows=[make_row(segment="ocasional")])
        ).list_customers(scope())

        self.assertEqual(response.model_dump(mode="json")["items"][0]["segment"], "ocasional")

    def test_cliente_sem_pedido_faturavel_sai_com_ticket_zero(self):
        """Quem pediu duas vezes e cancelou as duas. O banco devolve zero (a
        divisao vira NULL e cai no `coalesce`), e o contrato tambem."""
        row = make_row(
            orders_count=2,
            billable_orders_count=0,
            total_spent=Decimal("0"),
            average_ticket=Decimal("0"),
        )
        item = build_service(
            RecordingCustomerRepository(rows=[row])
        ).list_customers(scope()).items[0]

        self.assertEqual(item.average_ticket, 0.0)


class FiltrosTests(unittest.TestCase):
    """Os cinco filtros, do jeito que chegam ao repositorio."""

    def _filtros_de(self, **kwargs):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(), **kwargs)
        return repository.list_kwargs["filters"]

    def test_sem_filtro_nenhum_campo_vira_comparacao(self):
        filtros = self._filtros_de()

        self.assertEqual(
            (
                filtros.segment,
                filtros.last_order_from,
                filtros.last_order_to,
                filtros.min_ticket,
                filtros.max_ticket,
            ),
            (None, None, None, None, None),
        )

    def test_o_segmento_chega_como_enum(self):
        filtros = self._filtros_de(segment=CustomerSegment.PERDIDO)

        self.assertEqual(filtros.segment, CustomerSegment.PERDIDO)

    def test_as_datas_sao_lidas_no_fuso_da_operacao(self):
        """A data que o lojista escolhe e o DIA DELE. Sem a conversao, tres
        horas de pedidos caem no dia errado do recorte."""
        filtros = self._filtros_de(
            last_order_from=date(2026, 8, 1), last_order_to=date(2026, 8, 31)
        )

        self.assertEqual(
            filtros.last_order_from.astimezone(timezone.utc),
            datetime(2026, 8, 1, 3, tzinfo=timezone.utc),
        )
        # Fim EXCLUSIVO na meia-noite do dia seguinte: com o fim fechado, o
        # pedido das 23:59:59.7 do dia 31 ficaria de fora do proprio dia.
        self.assertEqual(
            filtros.last_order_to.astimezone(timezone.utc),
            datetime(2026, 9, 1, 3, tzinfo=timezone.utc),
        )

    def test_cada_data_funciona_sozinha(self):
        so_o_comeco = self._filtros_de(last_order_from=date(2026, 8, 1))
        so_o_fim = self._filtros_de(last_order_to=date(2026, 8, 31))

        self.assertIsNotNone(so_o_comeco.last_order_from)
        self.assertIsNone(so_o_comeco.last_order_to)
        self.assertIsNone(so_o_fim.last_order_from)
        self.assertIsNotNone(so_o_fim.last_order_to)

    def test_periodo_invertido_responde_400(self):
        """Lista vazia deixaria o lojista procurando o cliente que sumiu da
        tela; 400 diz que o pedido e que estava errado."""
        with self.assertRaises(HTTPException) as raised:
            self._filtros_de(
                last_order_from=date(2026, 8, 31), last_order_to=date(2026, 8, 1)
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_faixa_de_ticket_invertida_responde_400(self):
        with self.assertRaises(HTTPException) as raised:
            self._filtros_de(min_ticket=Decimal("80"), max_ticket=Decimal("20"))

        self.assertEqual(raised.exception.status_code, 400)

    def test_a_pagina_e_a_contagem_recebem_os_MESMOS_filtros(self):
        """Filtro que vale so num dos lados devolve pagina que nao bate com o
        total — e o lojista ve "23 clientes" sobre uma lista de 5."""
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(
            scope(), segment=CustomerSegment.FIEL, min_ticket=Decimal("30")
        )

        self.assertEqual(repository.list_kwargs["filters"], repository.count_kwargs["filters"])

    def test_a_pagina_e_a_contagem_sao_classificadas_no_MESMO_instante(self):
        """O `agora` vai dentro da consulta como parametro. Se cada consulta
        pegasse o proprio, uma leitura na virada do dia classificaria a pagina
        contra hoje e o total contra ontem."""
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope())

        self.assertIs(repository.list_kwargs["now"], repository.count_kwargs["now"])


class SearchConditionTests(unittest.TestCase):
    def test_digits_search_matches_the_phone(self):
        condition = str(_build_customer_search_condition("85999"))

        self.assertIn("customer_phone_snapshot", condition)

    def test_text_search_matches_the_name(self):
        condition = str(_build_customer_search_condition("dona maria"))

        self.assertIn("customer_name_snapshot", condition)
        self.assertIn("lower", condition.lower())

    def test_like_wildcards_are_escaped(self):
        condition = _build_customer_search_condition("100%")

        # Sem escapar, um "%" digitado sozinho listaria a base inteira.
        self.assertIn("\\%", condition.right.value)


if __name__ == "__main__":
    unittest.main()
