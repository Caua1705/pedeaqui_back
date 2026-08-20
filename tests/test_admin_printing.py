"""Setores de impressao e montagem das vias (BLOCO de impressao).

O que estes testes protegem, em ordem de gravidade:

1. **Pedido nao pago nao gera via de producao.** Comanda impressa e ordem
   de preparo. Se ela saisse com o pix em aberto, a regra do "aguardando
   pagamento, nao preparar" valeria so para quem esta olhando a tela — a
   praca prepararia o pedido do mesmo jeito.
2. **Nenhum item some da comanda.** So o produto com `printing_sector_id`
   nulo deixa de imprimir, porque isso e uma decisao explicita do lojista.
   Setor de outra filial ou setor desativado caem na via "SEM SETOR", nunca
   no vazio.
3. **Nada de outro restaurante nem de outra filial.** Setor, produto e
   categoria sao alcancados sempre com o restaurante do token, e o setor
   ainda passa pelo escopo de filial: um manager preso a uma loja nao
   aponta produto para a impressora da loja vizinha.

O repositorio e fake — ele prova que o parametro chegou, nao que o SQL esta
certo. O que fica para o teste de integracao e que o WHERE de fato filtra.
"""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from main import app
from src.api.dependencies.admin_scope import AdminScope
from src.models.order_item_model import OrderItem
from src.models.order_model import Order
from src.models.printing_sector_model import PrintingSector
from src.schemas.admin_printing_schema import (
    PRINT_JOB_CUSTOMER,
    PRINT_JOB_PRODUCTION,
    PrintingSectorCreate,
    PrintingSectorUpdate,
    ProductPrintingSectorRequest,
)
from src.services.admin_printing_service import UNASSIGNED_SECTOR_NAME, AdminPrintingService
from src.services.order_service import OrderService


RESTAURANT_ID = uuid.uuid4()
BRANCH_ID = uuid.uuid4()
OTHER_BRANCH_ID = uuid.uuid4()
CREATED_AT = datetime(2026, 8, 9, 17, 32, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeSectorRepository:
    """Fake que respeita o escopo do WHERE real.

    `printing_sectors` nao tem `restaurant_id`: o repositorio real chega ao
    restaurante pela juncao com `branches`. Aqui a filial carrega o
    restaurante dela, que e o mesmo efeito.
    """

    def __init__(self, sectors=(), products=(), branches_of=None):
        self.sectors = list(sectors)
        self.products = list(products)
        # filial -> restaurante, o que a juncao com `branches` resolve.
        self.branches_of = branches_of or {
            BRANCH_ID: RESTAURANT_ID,
            OTHER_BRANCH_ID: RESTAURANT_ID,
        }
        self.added = []
        self.category_updates = []

    def list_by_branch(self, branch_id, include_inactive=True):
        return sorted(
            [
                sector for sector in self.sectors
                if sector.branch_id == branch_id and (include_inactive or sector.is_active)
            ],
            key=lambda sector: (sector.sort_order, sector.name),
        )

    def get(self, sector_id, restaurant_id):
        for sector in self.sectors:
            if sector.id != sector_id:
                continue
            if self.branches_of.get(sector.branch_id) == restaurant_id:
                return sector
        return None

    def get_by_name(self, name, branch_id):
        for sector in self.sectors:
            if sector.name == name and sector.branch_id == branch_id:
                return sector
        return None

    def add(self, sector):
        # O que o flush do repositorio real faz: devolve a linha com id.
        sector.id = uuid.uuid4()
        self.added.append(sector)
        return sector

    def list_sectors_of_products(self, product_ids, restaurant_id):
        rows = []
        for product in self.products:
            if product.id not in product_ids or product.restaurant_id != restaurant_id:
                continue
            sector = next(
                (item for item in self.sectors if item.id == product.printing_sector_id),
                None,
            )
            rows.append((product.id, sector))
        return rows

    def assign_sector_to_category(self, category_id, restaurant_id, sector_id):
        self.category_updates.append((category_id, restaurant_id, sector_id))
        affected = [
            product for product in self.products
            if product.category_id == category_id and product.restaurant_id == restaurant_id
        ]
        for product in affected:
            product.printing_sector_id = sector_id
        return len(affected)


class FakeMenuRepository:
    def __init__(self, products=(), categories=()):
        self.products = list(products)
        self.categories = list(categories)

    def get_product(self, product_id, restaurant_id):
        for product in self.products:
            if product.id == product_id and product.restaurant_id == restaurant_id:
                return product
        return None

    def get_category(self, category_id, restaurant_id):
        for category in self.categories:
            if category.id == category_id and category.restaurant_id == restaurant_id:
                return category
        return None


class FakeBranchRepository:
    def __init__(self, branches=()):
        self.branches = list(branches)

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        for branch in self.branches:
            if branch.id == branch_id and branch.restaurant_id == restaurant_id:
                return branch
        return None


class FakeOrderService:
    """Substitui o AdminOrderService, que e quem resolve o escopo do pedido.

    Trocado inteiro de proposito: o teste aqui e sobre as VIAS, e a regra de
    "que pedido este lojista pode ler" ja tem os testes dela em
    test_admin_tenant_isolation.
    """

    def __init__(self, detail):
        self.detail = detail
        self.calls = []

    def get_order_detail(self, order_id, scope):
        self.calls.append((order_id, scope))
        return self.detail


def make_scope(branch_id=None):
    return AdminScope(
        admin_user=SimpleNamespace(id=uuid.uuid4(), email="dono@loja.com", role="owner"),
        restaurant_id=RESTAURANT_ID,
        branch_id=branch_id,
    )


def make_sector(name="Cozinha", branch_id=BRANCH_ID, is_active=True, sort_order=0):
    return PrintingSector(
        id=uuid.uuid4(),
        branch_id=branch_id,
        name=name,
        is_active=is_active,
        sort_order=sort_order,
    )


def make_product(
    sector_id=None,
    category_id=None,
    restaurant_id=RESTAURANT_ID,
    branch_id=BRANCH_ID,
):
    """Produto de UMA filial.

    A filial default e a mesma dos setores deste arquivo: desde a revisao
    20260820_0026 produto e setor tem que estar na mesma loja, e a FK
    composta nao deixa gravar o contrario.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        branch_id=branch_id,
        category_id=category_id or uuid.uuid4(),
        printing_sector_id=sector_id,
    )


def make_order_detail(items, payment_status="on_delivery", branch_id=BRANCH_ID):
    order = Order(
        id=uuid.uuid4(),
        order_number=1234,
        restaurant_id=RESTAURANT_ID,
        branch_id=branch_id,
        customer_name_snapshot="Dona Maria",
        customer_phone_snapshot="85999998888",
        order_type="delivery",
        status="preparing",
        payment_method="cash",
        payment_status=payment_status,
        subtotal=Decimal("32.00"),
        delivery_fee=Decimal("5.00"),
        service_fee=Decimal("0.00"),
        coupon_discount_amount=Decimal("0.00"),
        cashback_redeemed_amount=Decimal("0.00"),
        discount_total=Decimal("0.00"),
        total=Decimal("37.00"),
        created_at=CREATED_AT,
    )
    order.items = list(items)
    order.status_history = []
    return OrderService.to_order_detail_response(order)


def make_item(product_id, name="Prato feito"):
    item = OrderItem(
        id=uuid.uuid4(),
        product_id=product_id,
        product_code_snapshot=None,
        product_name_snapshot=name,
        product_description_snapshot=None,
        unit_price_snapshot=Decimal("32.00"),
        quantity=1,
        observation=None,
        total=Decimal("32.00"),
        created_at=CREATED_AT,
    )
    item.options = []
    return item


def build_service(sector_repository=None, menu_repository=None, branches=(), order_detail=None):
    service = AdminPrintingService(FakeDb())
    service.repository = sector_repository or FakeSectorRepository()
    service.menu_repository = menu_repository or FakeMenuRepository()
    service.branch_repository = FakeBranchRepository(
        branches or [SimpleNamespace(id=BRANCH_ID, restaurant_id=RESTAURANT_ID)]
    )
    if order_detail is not None:
        service.order_service = FakeOrderService(order_detail)
    return service


def jobs_of(response):
    return [(job.type, job.sector_name) for job in response.jobs]


class PrintJobTests(unittest.TestCase):
    def test_a_paid_order_gets_the_customer_copy_and_one_copy_per_sector(self):
        kitchen = make_sector("Cozinha", sort_order=0)
        bar = make_sector("Bar", sort_order=1)
        steak = make_product(kitchen.id)
        drink = make_product(bar.id)
        detail = make_order_detail([
            make_item(steak.id, "Prato feito"),
            make_item(drink.id, "Suco de caju"),
        ])
        service = build_service(
            FakeSectorRepository([kitchen, bar], [steak, drink]),
            order_detail=detail,
        )

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(
            jobs_of(response),
            [(PRINT_JOB_CUSTOMER, "Via do cliente"),
             (PRINT_JOB_PRODUCTION, "Cozinha"),
             (PRINT_JOB_PRODUCTION, "Bar")],
        )
        kitchen_job = response.jobs[1]
        self.assertIn("PRATO FEITO", kitchen_job.content)
        self.assertNotIn("SUCO", kitchen_job.content)
        self.assertEqual(kitchen_job.font_size, "large")
        self.assertEqual(kitchen_job.columns, 24)

    def test_the_sectors_come_in_the_order_the_shop_arranged(self):
        # E a ordem que o lojista arrumou pensando no fluxo da cozinha, e
        # ela precisa ser a mesma em todo pedido — quem separa as bobinas
        # se perde se cada pedido sair numa sequencia.
        grill = make_sector("Chapa", sort_order=0)
        kitchen = make_sector("Cozinha", sort_order=5)
        first = make_product(kitchen.id)
        second = make_product(grill.id)
        detail = make_order_detail([make_item(first.id), make_item(second.id)])
        service = build_service(
            FakeSectorRepository([kitchen, grill], [first, second]),
            order_detail=detail,
        )

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(
            [job.sector_name for job in response.jobs if job.type == PRINT_JOB_PRODUCTION],
            ["Chapa", "Cozinha"],
        )

    def test_an_unpaid_online_order_gets_no_production_copy(self):
        # A regra central: comanda de producao e ordem de preparo. A via do
        # cliente continua saindo — ela serve de conferencia no balcao e nao
        # manda ninguem cozinhar.
        kitchen = make_sector()
        product = make_product(kitchen.id)
        detail = make_order_detail([make_item(product.id)], payment_status="pending")
        service = build_service(
            FakeSectorRepository([kitchen], [product]),
            order_detail=detail,
        )

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(jobs_of(response), [(PRINT_JOB_CUSTOMER, "Via do cliente")])

    def test_a_paid_online_order_gets_the_production_copies(self):
        kitchen = make_sector()
        product = make_product(kitchen.id)
        detail = make_order_detail([make_item(product.id)], payment_status="paid")
        service = build_service(
            FakeSectorRepository([kitchen], [product]),
            order_detail=detail,
        )

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(len(response.jobs), 2)

    def test_a_product_without_a_sector_prints_no_production_copy(self):
        # Nulo tem significado: a lata que sai da geladeira do balcao nao
        # passa por praca nenhuma, e uma comanda para ela so ensina a
        # cozinha a ignorar comanda.
        product = make_product(sector_id=None)
        detail = make_order_detail([make_item(product.id, "Refrigerante lata")])
        service = build_service(FakeSectorRepository([], [product]), order_detail=detail)

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(jobs_of(response), [(PRINT_JOB_CUSTOMER, "Via do cliente")])

    def test_a_deactivated_sector_falls_back_too(self):
        # Desativar o setor sem reapontar os produtos e configuracao pela
        # metade, nao instrucao de parar de imprimir.
        sector = make_sector(is_active=False)
        product = make_product(sector.id)
        detail = make_order_detail([make_item(product.id)])
        service = build_service(FakeSectorRepository([sector], [product]), order_detail=detail)

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(
            [job.sector_name for job in response.jobs if job.type == PRINT_JOB_PRODUCTION],
            [UNASSIGNED_SECTOR_NAME],
        )

    def test_the_fallback_copy_comes_after_the_real_sectors(self):
        # O setor desativado e o unico caminho que ainda leva a via de
        # resgate: "setor de outra filial" deixou de ser gravavel na revisao
        # 20260820_0026 (FK composta), e o teste daquele caso virou o de
        # escrita, em ProductBindingTests.
        kitchen = make_sector("Cozinha")
        orphan = make_sector("Cozinha desligada", is_active=False)
        routed = make_product(kitchen.id)
        stray = make_product(orphan.id)
        detail = make_order_detail([make_item(stray.id), make_item(routed.id)])
        service = build_service(
            FakeSectorRepository([kitchen, orphan], [routed, stray]),
            order_detail=detail,
        )

        response = service.build_print_jobs(detail.id, make_scope())

        self.assertEqual(
            [job.sector_name for job in response.jobs if job.type == PRINT_JOB_PRODUCTION],
            ["Cozinha", UNASSIGNED_SECTOR_NAME],
        )

    def test_the_order_is_read_through_the_scope_of_the_token(self):
        # A rota nao tem conferencia de escopo propria: ela reusa o
        # AdminOrderService, que ja responde o mesmo 404 para pedido
        # inexistente, de outro restaurante e de outra filial.
        detail = make_order_detail([])
        service = build_service(order_detail=detail)
        scope = make_scope()

        service.build_print_jobs(detail.id, scope)

        self.assertEqual(service.order_service.calls, [(detail.id, scope)])


class SectorCrudTests(unittest.TestCase):
    def test_creating_a_sector_binds_it_to_the_branch_in_the_path(self):
        repository = FakeSectorRepository()
        service = build_service(repository)

        response = service.create_sector(
            make_scope(), BRANCH_ID, PrintingSectorCreate(name="Chapa", sort_order=2)
        )

        self.assertEqual(response.branch_id, BRANCH_ID)
        self.assertEqual(response.name, "Chapa")
        self.assertEqual(response.sort_order, 2)
        self.assertTrue(response.is_active)

    def test_a_repeated_name_in_the_same_branch_is_refused(self):
        # Duas impressoras chamadas "Cozinha" tornam impossivel saber para
        # onde um produto foi apontado.
        repository = FakeSectorRepository([make_sector("Cozinha")])
        service = build_service(repository)

        with self.assertRaises(HTTPException) as caught:
            service.create_sector(make_scope(), BRANCH_ID, PrintingSectorCreate(name="Cozinha"))

        self.assertEqual(caught.exception.status_code, 409)

    def test_the_same_name_in_another_branch_is_fine(self):
        # A "Cozinha" do Centro e outra maquina que a "Cozinha" da Aldeota.
        repository = FakeSectorRepository([make_sector("Cozinha", branch_id=OTHER_BRANCH_ID)])
        service = build_service(
            repository,
            branches=[
                SimpleNamespace(id=BRANCH_ID, restaurant_id=RESTAURANT_ID),
                SimpleNamespace(id=OTHER_BRANCH_ID, restaurant_id=RESTAURANT_ID),
            ],
        )

        response = service.create_sector(
            make_scope(), BRANCH_ID, PrintingSectorCreate(name="Cozinha")
        )

        self.assertEqual(response.branch_id, BRANCH_ID)

    def test_deactivating_is_how_a_sector_is_removed(self):
        # Nao existe DELETE: products.printing_sector_id aponta para esta
        # linha por FK.
        sector = make_sector()
        service = build_service(FakeSectorRepository([sector]))

        response = service.update_sector(
            make_scope(), sector.id, PrintingSectorUpdate(is_active=False)
        )

        self.assertFalse(response.is_active)

    def test_a_sector_of_another_restaurant_is_not_found(self):
        sector = make_sector()
        repository = FakeSectorRepository([sector])
        repository.branches_of = {BRANCH_ID: uuid.uuid4()}
        service = build_service(repository)

        with self.assertRaises(HTTPException) as caught:
            service.update_sector(make_scope(), sector.id, PrintingSectorUpdate(name="X"))

        self.assertEqual(caught.exception.status_code, 404)

    def test_a_branch_locked_manager_does_not_reach_another_branch_sector(self):
        # Mesmo 404 e nao 403: um 403 confirmaria que aquele setor existe.
        sector = make_sector(branch_id=OTHER_BRANCH_ID)
        service = build_service(FakeSectorRepository([sector]))

        with self.assertRaises(HTTPException) as caught:
            service.update_sector(
                make_scope(branch_id=BRANCH_ID), sector.id, PrintingSectorUpdate(name="X")
            )

        self.assertEqual(caught.exception.status_code, 404)

    def test_listing_a_branch_outside_the_scope_is_not_found(self):
        service = build_service(FakeSectorRepository())

        with self.assertRaises(HTTPException) as caught:
            service.list_sectors(make_scope(branch_id=BRANCH_ID), OTHER_BRANCH_ID)

        self.assertEqual(caught.exception.status_code, 404)


class ProductBindingTests(unittest.TestCase):
    def test_a_product_can_be_pointed_at_a_sector(self):
        sector = make_sector()
        product = make_product()
        service = build_service(
            FakeSectorRepository([sector], [product]),
            FakeMenuRepository([product]),
        )

        response = service.set_product_sector(
            make_scope(), product.id, ProductPrintingSectorRequest(printing_sector_id=sector.id)
        )

        self.assertEqual(product.printing_sector_id, sector.id)
        self.assertEqual(response.printing_sector_name, "Cozinha")

    def test_null_turns_the_production_copy_off_for_that_product(self):
        # Nao e campo faltando: e a instrucao de nao imprimir.
        sector = make_sector()
        product = make_product(sector.id)
        service = build_service(
            FakeSectorRepository([sector], [product]),
            FakeMenuRepository([product]),
        )

        response = service.set_product_sector(
            make_scope(), product.id, ProductPrintingSectorRequest(printing_sector_id=None)
        )

        self.assertIsNone(product.printing_sector_id)
        self.assertIsNone(response.printing_sector_name)

    def test_a_product_of_another_restaurant_is_not_found(self):
        product = make_product(restaurant_id=uuid.uuid4())
        service = build_service(FakeSectorRepository(), FakeMenuRepository([product]))

        with self.assertRaises(HTTPException) as caught:
            service.set_product_sector(
                make_scope(), product.id, ProductPrintingSectorRequest()
            )

        self.assertEqual(caught.exception.status_code, 404)

    def test_a_sector_of_another_restaurant_cannot_be_bound(self):
        # Sem isto, um UUID de setor descoberto por fora mandaria a comanda
        # deste restaurante para a impressora de outro.
        sector = make_sector()
        product = make_product()
        repository = FakeSectorRepository([sector], [product])
        repository.branches_of = {BRANCH_ID: uuid.uuid4()}
        service = build_service(repository, FakeMenuRepository([product]))

        with self.assertRaises(HTTPException) as caught:
            service.set_product_sector(
                make_scope(),
                product.id,
                ProductPrintingSectorRequest(printing_sector_id=sector.id),
            )

        self.assertEqual(caught.exception.status_code, 404)


class CategoryBindingTests(unittest.TestCase):
    def test_applying_to_a_category_hits_every_product_at_once(self):
        # E como a configuracao acontece: "toda bebida vai para o Bar".
        # Produto a produto, um cardapio de 200 itens nunca sai do lugar.
        sector = make_sector("Bar")
        category = SimpleNamespace(id=uuid.uuid4(), restaurant_id=RESTAURANT_ID, branch_id=BRANCH_ID)
        drinks = [make_product(category_id=category.id) for _ in range(3)]
        other = make_product()
        repository = FakeSectorRepository([sector], drinks + [other])
        service = build_service(repository, FakeMenuRepository(drinks + [other], [category]))

        response = service.set_category_sector(
            make_scope(),
            category.id,
            ProductPrintingSectorRequest(printing_sector_id=sector.id),
        )

        self.assertEqual(response.updated_products, 3)
        self.assertEqual({product.printing_sector_id for product in drinks}, {sector.id})
        self.assertIsNone(other.printing_sector_id)

    def test_the_bulk_update_is_scoped_to_the_restaurant_of_the_token(self):
        # O restaurante entra no WHERE junto com a categoria: sem ele, um
        # UUID de categoria alheia reescreveria o cardapio de outro dono.
        sector = make_sector()
        category = SimpleNamespace(id=uuid.uuid4(), restaurant_id=RESTAURANT_ID, branch_id=BRANCH_ID)
        repository = FakeSectorRepository([sector])
        service = build_service(repository, FakeMenuRepository(categories=[category]))

        service.set_category_sector(
            make_scope(),
            category.id,
            ProductPrintingSectorRequest(printing_sector_id=sector.id),
        )

        self.assertEqual(repository.category_updates, [(category.id, RESTAURANT_ID, sector.id)])

    def test_a_category_of_another_restaurant_is_not_found(self):
        category = SimpleNamespace(id=uuid.uuid4(), restaurant_id=uuid.uuid4(), branch_id=OTHER_BRANCH_ID)
        service = build_service(FakeSectorRepository(), FakeMenuRepository(categories=[category]))

        with self.assertRaises(HTTPException) as caught:
            service.set_category_sector(
                make_scope(), category.id, ProductPrintingSectorRequest()
            )

        self.assertEqual(caught.exception.status_code, 404)


class PublishedContractTests(unittest.TestCase):
    """O painel e o agente de impressao escrevem a partir do /openapi.json.

    Uma rota ou um campo que existe na resposta mas nao no documento e algo
    que o cliente so descobre lendo o backend.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = app.openapi()

    def test_the_print_jobs_route_is_published(self):
        operation = self.schema["paths"]["/admin/orders/{order_id}/print-jobs"]["get"]
        body = operation["responses"]["200"]["content"]["application/json"]["schema"]

        self.assertEqual(body["$ref"], "#/components/schemas/OrderPrintJobsResponse")

    def test_the_job_publishes_everything_the_agent_needs(self):
        job = self.schema["components"]["schemas"]["PrintJobResponse"]

        # O agente le exatamente estes campos: seleciona a fonte, escreve o
        # conteudo e corta. Nada mais.
        for field in ("type", "sector_name", "columns", "font_size", "content"):
            self.assertIn(field, job["properties"])

    def test_the_sector_routes_are_published(self):
        for path, method in (
            ("/admin/branches/{branch_id}/printing-sectors", "get"),
            ("/admin/branches/{branch_id}/printing-sectors", "post"),
            ("/admin/printing-sectors/{sector_id}", "patch"),
            ("/admin/products/{product_id}/printing-sector", "patch"),
            ("/admin/categories/{category_id}/printing-sector", "patch"),
        ):
            self.assertIn(method, self.schema["paths"][path])

    def test_the_product_shows_which_sector_it_prints_on(self):
        product = self.schema["components"]["schemas"]["AdminProductResponse"]

        self.assertIn("printing_sector_id", product["properties"])


if __name__ == "__main__":
    unittest.main()
