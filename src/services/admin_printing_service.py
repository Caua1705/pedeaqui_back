"""Setores de impressao e montagem das comandas.

Duas responsabilidades que andam juntas:

1. **Cadastro dos setores** e o vinculo produto -> setor (inclusive a
   aplicacao em massa por categoria, que e como o lojista configura o
   cardapio inteiro numa tarde em vez de produto a produto).
2. **Montagem das tarefas de impressao** de um pedido: quais vias existem,
   com quais itens, em que ordem. O DESENHO de cada via nao esta aqui — fica
   em `src/services/print_layout.py`, sem banco, para poder ser testado
   linha a linha.

Tres regras deste arquivo merecem ser lidas antes de mexer:

**Pedido nao pago nao gera via de producao.** E a mesma regra que
`ensure_payment_allows_order_status` ja aplica ao status: pedido com
pagamento online pendente nao entra na cozinha. Se a comanda saisse assim
mesmo, a regra do painel valeria so para quem olha a tela — a praca
prepararia o pedido do mesmo jeito, porque comanda impressa e ordem de
producao. A via do CLIENTE continua saindo: ela serve de conferencia no
balcao e nao manda ninguem cozinhar.

**Item nunca some da comanda.** O setor pende de filial e o produto pende de
restaurante, entao um produto so consegue apontar para o setor de UMA
filial. Num restaurante com varias lojas, o pedido da filial B pode conter
um produto apontando para um setor da filial A — e um setor pode ter sido
desativado depois de vinculado. Nesses dois casos o item vai para uma via
"SEM SETOR" e o caso e logado, em vez de o item desaparecer silenciosamente
da producao. O unico jeito de um item NAO ser impresso e o produto ter
`printing_sector_id` nulo, que e uma decisao explicita do lojista.

**Nada e apagado.** Setor so e ligado e desligado, pelo mesmo motivo do
cardapio: `products.printing_sector_id` referencia a linha por FK.
"""

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.printing_sector_model import PrintingSector
from src.repositories.admin_menu_repository import AdminMenuRepository
from src.repositories.branch_repository import BranchRepository
from src.repositories.printing_sector_repository import PrintingSectorRepository
from src.schemas.admin_printing_schema import (
    FONT_LARGE,
    FONT_NORMAL,
    PRINT_JOB_CUSTOMER,
    PRINT_JOB_PRODUCTION,
    CategoryPrintingSectorResponse,
    OrderPrintJobsResponse,
    PrintingSectorCreate,
    PrintingSectorResponse,
    PrintingSectorUpdate,
    PrintJobResponse,
    ProductPrintingSectorRequest,
    ProductPrintingSectorResponse,
)
from src.schemas.order_schema import OrderDetailResponse, OrderItemResponse
from src.services.admin_order_service import AdminOrderService
from src.services.order_state_machine import PAYMENT_STATUSES_THAT_RELEASE_ORDER
from src.services.print_layout import (
    PRODUCTION_WIDTH,
    RECEIPT_WIDTH,
    build_customer_receipt,
    build_production_ticket,
)


logger = logging.getLogger("uvicorn.error")

# Nome que aparece no topo da via do cliente. Ela nao pertence a setor
# nenhum, mas o campo e preenchido do mesmo jeito: e por ele que o agente
# identifica a bobina no log dele.
CUSTOMER_JOB_NAME = "Via do cliente"

# Via de resgate: recebe o item cujo setor nao serve para esta filial (setor
# de outra loja, ou setor desativado depois do vinculo). Existe para que
# nenhum item saia da comanda sem alguem ter mandado.
UNASSIGNED_SECTOR_NAME = "SEM SETOR"


class AdminPrintingService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = PrintingSectorRepository(db)
        self.branch_repository = BranchRepository(db)
        self.menu_repository = AdminMenuRepository(db)
        # Reusado inteiro, e nao so o repositorio de pedidos: e ele que
        # decide o que este lojista pode ler (restaurante E filial, mesmo
        # 404 para os tres motivos). Uma segunda conferencia de escopo aqui
        # seria a chance de as duas divergirem.
        self.order_service = AdminOrderService(db)

    def list_sectors(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
    ) -> list[PrintingSectorResponse]:
        """Setores da filial, ativos e inativos.

        Inclui os desativados de proposito, igual a listagem de categorias:
        quem desligou precisa continuar vendo para religar.
        """
        self._get_branch(scope, branch_id)
        sectors = self.repository.list_by_branch(branch_id)
        return [PrintingSectorResponse.model_validate(sector) for sector in sectors]

    def create_sector(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: PrintingSectorCreate,
    ) -> PrintingSectorResponse:
        self._get_branch(scope, branch_id)
        self._ensure_name_is_free(payload.name, branch_id)

        sector = PrintingSector(
            branch_id=branch_id,
            name=payload.name,
            sort_order=payload.sort_order,
            is_active=payload.is_active,
            printer_name=payload.printer_name,
        )
        self._commit(lambda: self.repository.add(sector))
        return PrintingSectorResponse.model_validate(sector)

    def update_sector(
        self,
        scope: AdminScope,
        sector_id: uuid.UUID,
        payload: PrintingSectorUpdate,
    ) -> PrintingSectorResponse:
        """Renomeia, reordena ou desativa o setor.

        Desativar (`is_active: false`) e o que o painel chama de excluir. O
        vinculo dos produtos NAO e desfeito: o setor pode voltar, e ate la
        os itens desses produtos saem na via "SEM SETOR" em vez de sumirem
        da producao.
        """
        sector = self._get_sector(scope, sector_id)
        changes = payload.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] != sector.name:
            self._ensure_name_is_free(changes["name"], sector.branch_id)

        for field, value in changes.items():
            setattr(sector, field, value)
        self._commit()
        return PrintingSectorResponse.model_validate(sector)

    def set_product_sector(
        self,
        scope: AdminScope,
        product_id: uuid.UUID,
        payload: ProductPrintingSectorRequest,
    ) -> ProductPrintingSectorResponse:
        """Aponta um produto para um setor, ou desliga a impressao dele.

        Rota propria em vez de um campo no PATCH do produto pelo mesmo
        motivo do botao de esgotado: e uma acao de um campo so, e um corpo
        de um campo so nao arrasta preco velho aberto em outra aba.
        """
        product = self._get_product(scope, product_id)
        sector = self._resolve_sector(scope, payload.printing_sector_id)

        product.printing_sector_id = sector.id if sector else None
        self._commit()
        return ProductPrintingSectorResponse(
            product_id=product.id,
            printing_sector_id=product.printing_sector_id,
            printing_sector_name=sector.name if sector else None,
        )

    def set_category_sector(
        self,
        scope: AdminScope,
        category_id: uuid.UUID,
        payload: ProductPrintingSectorRequest,
    ) -> CategoryPrintingSectorResponse:
        """Aplica um setor a todos os produtos da categoria de uma vez.

        E como a configuracao inicial acontece de verdade: "tudo que e
        bebida vai para o Bar, tudo que e pizza vai para o Forno". Produto a
        produto, um cardapio de 200 itens nao seria configurado nunca — e
        cardapio meio configurado imprime comanda pela metade.

        Sobrescreve inclusive quem ja tinha setor. E o esperado de "aplicar
        a categoria": quem quiser uma excecao reaponta aquele produto depois.
        """
        category = self.menu_repository.get_category(category_id, scope.restaurant_id)
        if category is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Categoria nao encontrada"
            )
        sector = self._resolve_sector(scope, payload.printing_sector_id)

        updated = 0

        def apply() -> None:
            nonlocal updated
            updated = self.repository.assign_sector_to_category(
                category_id,
                scope.restaurant_id,
                sector.id if sector else None,
            )

        self._commit(apply)
        return CategoryPrintingSectorResponse(
            category_id=category_id,
            printing_sector_id=sector.id if sector else None,
            updated_products=updated,
        )

    def build_print_jobs(
        self,
        order_id: uuid.UUID,
        scope: AdminScope,
    ) -> OrderPrintJobsResponse:
        """As vias de um pedido, prontas para a impressora.

        A leitura do pedido passa pelo `AdminOrderService`: mesmo 404 para
        pedido inexistente, de outro restaurante e de outra filial, e o
        mesmo `OrderDetailResponse` que o painel ja mostra — inclusive os
        adicionais agrupados por grupo de complemento, que sao o que faz a
        troca de acompanhamento aparecer na comanda da cozinha.
        """
        order = self.order_service.get_order_detail(order_id, scope)

        jobs = [
            PrintJobResponse(
                type=PRINT_JOB_CUSTOMER,
                sector_id=None,
                sector_name=CUSTOMER_JOB_NAME,
                # A via do cliente nao pertence a setor nenhum, entao nao
                # tem impressora escolhida: ela cai na padrao do agente. Foi
                # sempre assim; a coluna nova nao muda isso.
                printer_name=None,
                columns=RECEIPT_WIDTH,
                font_size=FONT_NORMAL,
                content=build_customer_receipt(order, RECEIPT_WIDTH),
            )
        ]
        jobs.extend(self._production_jobs(order, scope))

        return OrderPrintJobsResponse(
            order_id=order.id,
            order_number=order.order_number,
            branch_id=order.branch_id,
            jobs=jobs,
        )

    def _production_jobs(
        self,
        order: OrderDetailResponse,
        scope: AdminScope,
    ) -> list[PrintJobResponse]:
        """Uma via por setor que tenha item neste pedido.

        Vazio quando o pagamento online ainda nao foi confirmado: comanda
        impressa e ordem de producao, e a regra do "aguardando pagamento,
        nao preparar" nao pode valer so para quem esta olhando a tela.
        """
        if order.payment_status not in PAYMENT_STATUSES_THAT_RELEASE_ORDER:
            return []

        items_by_sector, unassigned = self._split_items_by_sector(order, scope)

        jobs = [
            PrintJobResponse(
                type=PRINT_JOB_PRODUCTION,
                sector_id=sector.id,
                sector_name=sector.name,
                # O que faz o rename de setor no painel parar de quebrar a
                # impressao em silencio: o agente passa a receber a
                # impressora, em vez de procura-la pelo nome do setor no
                # config.ini dele.
                printer_name=sector.printer_name,
                columns=PRODUCTION_WIDTH,
                font_size=FONT_LARGE,
                content=build_production_ticket(order, sector.name, items, PRODUCTION_WIDTH),
            )
            for sector, items in items_by_sector
        ]
        if unassigned:
            jobs.append(
                PrintJobResponse(
                    type=PRINT_JOB_PRODUCTION,
                    sector_id=None,
                    sector_name=UNASSIGNED_SECTOR_NAME,
                    # A via de resgate nao tem setor, logo nao tem impressora
                    # escolhida: cai na padrao, que e onde alguem a encontra.
                    printer_name=None,
                    columns=PRODUCTION_WIDTH,
                    font_size=FONT_LARGE,
                    content=build_production_ticket(
                        order, UNASSIGNED_SECTOR_NAME, unassigned, PRODUCTION_WIDTH
                    ),
                )
            )
        return jobs

    def _split_items_by_sector(
        self,
        order: OrderDetailResponse,
        scope: AdminScope,
    ) -> tuple[list[tuple[PrintingSector, list[OrderItemResponse]]], list[OrderItemResponse]]:
        """Separa os itens do pedido pelo setor que os prepara.

        Devolve os setores na ordem cadastrada (`sort_order`, depois nome), e
        nao na ordem em que os itens aparecem: e a ordem que o lojista
        arrumou pensando no fluxo da cozinha, e ela precisa ser a mesma em
        todo pedido para quem separa as bobinas nao se perder.

        Os itens DENTRO de cada via preservam a ordem do pedido, que e a
        mesma da tela do painel e a mesma da via do cliente — conferir uma
        contra a outra so funciona se as linhas estiverem na mesma sequencia.
        """
        product_ids = [item.product_id for item in order.items if item.product_id]
        sector_by_product = {
            product_id: sector
            for product_id, sector in self.repository.list_sectors_of_products(
                product_ids, scope.restaurant_id
            )
        }

        items_by_sector: dict[uuid.UUID, list[OrderItemResponse]] = {}
        sectors_by_id: dict[uuid.UUID, PrintingSector] = {}
        unassigned: list[OrderItemResponse] = []

        for item in order.items:
            if item.product_id is None or item.product_id not in sector_by_product:
                # Produto que nao esta mais no cardapio deste restaurante.
                # Nao da para saber a praca dele, e a via de resgate e
                # melhor que um item invisivel para a cozinha.
                unassigned.append(item)
                continue

            sector = sector_by_product[item.product_id]
            if sector is None:
                # Decisao explicita do lojista: este produto nao imprime via
                # de producao (a lata que sai da geladeira do balcao).
                continue
            if sector.branch_id != order.branch_id or not sector.is_active:
                self._log_unusable_sector(order, item, sector)
                unassigned.append(item)
                continue

            sectors_by_id[sector.id] = sector
            items_by_sector.setdefault(sector.id, []).append(item)

        ordered = sorted(
            sectors_by_id.values(),
            key=lambda sector: (sector.sort_order, sector.name),
        )
        return [(sector, items_by_sector[sector.id]) for sector in ordered], unassigned

    @staticmethod
    def _log_unusable_sector(
        order: OrderDetailResponse,
        item: OrderItemResponse,
        sector: PrintingSector,
    ) -> None:
        """Avisa que um item caiu na via de resgate.

        E sempre configuracao errada, nunca operacao normal: ou o produto
        aponta para o setor de outra filial (e o pedido e desta), ou o setor
        foi desativado sem os produtos serem reapontados. Sem este log,
        alguem descobriria pela comanda estranha no meio do almoco.
        """
        reason = (
            "setor de outra filial"
            if sector.branch_id != order.branch_id
            else "setor desativado"
        )
        logger.warning(
            "[Impressao] item sem setor utilizavel (%s) order_id=%s product_id=%s sector_id=%s",
            reason,
            order.id,
            item.product_id,
            sector.id,
        )

    def _resolve_sector(
        self,
        scope: AdminScope,
        sector_id: uuid.UUID | None,
    ) -> PrintingSector | None:
        """Setor do corpo da requisicao, ou None para "nao imprime".

        `None` aqui nao e campo faltando: e a instrucao de desligar a via de
        producao daquele produto.
        """
        if sector_id is None:
            return None
        return self._get_sector(scope, sector_id)

    def _get_sector(self, scope: AdminScope, sector_id: uuid.UUID) -> PrintingSector:
        """Setor dentro do escopo do token.

        Duas conferencias diferentes, como em `AdminSettingsService._get_branch`:
        o repositorio (pela juncao com `branches`) barra o setor de outro
        restaurante; `ensure_branch_allowed` barra o setor de uma filial que
        este lojista nao enxerga. Mesmo 404 para as duas — um 403
        confirmaria que aquele setor existe.
        """
        sector = self.repository.get(sector_id, scope.restaurant_id)
        if sector is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Setor de impressao nao encontrado"
            )
        scope.ensure_branch_allowed(sector.branch_id)
        return sector

    def _get_branch(self, scope: AdminScope, branch_id: uuid.UUID):
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial nao encontrada"
            )
        return branch

    def _get_product(self, scope: AdminScope, product_id: uuid.UUID):
        product = self.menu_repository.get_product(product_id, scope.restaurant_id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Produto nao encontrado"
            )
        return product

    def _ensure_name_is_free(self, name: str, branch_id: uuid.UUID) -> None:
        """Recusa dois setores com o mesmo nome na mesma filial.

        O banco tambem barra (uq_printing_sectors_branch_name); a
        conferencia aqui existe para responder 409 com mensagem em vez de
        deixar o IntegrityError virar 500 — mesmo arranjo do slug do
        cardapio.
        """
        if self.repository.get_by_name(name, branch_id) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ja existe um setor com esse nome nesta filial",
            )

    def _commit(self, before_commit=None) -> None:
        """Grava, com rollback em qualquer falha.

        Mesmo arranjo de `AdminMenuService._commit`: o `add`/`flush` (ou o
        UPDATE em massa) entra no MESMO try do commit, porque uma falha ali
        precisa desfazer a sessao do mesmo jeito.
        """
        try:
            if before_commit is not None:
                before_commit()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
