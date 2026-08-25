import logging
from collections.abc import Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.models.branch_model import Branch
from src.repositories.branch_repository import BranchRepository
from src.repositories.cashback_rule_repository import CashbackRuleRepository
from src.repositories.menu_repository import MenuRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.banner_schema import BannerResponse
from src.schemas.coupon_schema import PublicCouponResponse
from src.schemas.menu_schema import RestaurantMenuResponse
from src.schemas.product_schema import ProductOptionGroupResponse, ProductOptionResponse, ProductResponse
from src.schemas.restaurant_schema import (
    BranchCashbackTermsResponse,
    BranchResponse,
    CategoryResponse,
    DeliveryTimeBandResponse,
    RestaurantSettingsResponse,
)
from src.services.branch_operation import BranchOperation, resolve_branch_operation
from src.services.cashback_rule import resolve_cashback_terms
from src.services.menu_rules import blocking_required_group
from src.services.restaurant_service import RestaurantService
from src.utils.money import money_to_float
from src.utils.storage import build_storage_url


logger = logging.getLogger("uvicorn.error")


class MenuService:
    def __init__(self, db: Session):
        self.menu_repository = MenuRepository(db)
        self.branch_repository = BranchRepository(db)
        self.cashback_rule_repository = CashbackRuleRepository(db)
        self.product_repository = ProductRepository(db)
        self.restaurant_service = RestaurantService(db)

    def get_restaurant_menu(
        self,
        restaurant_slug: str,
        branch_id: UUID | None = None,
    ) -> RestaurantMenuResponse:
        """O cardapio DAQUELA FILIAL, e a operacao dela.

        Desde a revisao 20260820_0026 o `branch_id` resolve a resposta
        inteira: produtos, categorias, precos, disponibilidade E o bloco
        `settings`. Nao existe mais "o cardapio do restaurante" — cada loja
        tem o seu, sem heranca e sem sobrescrita.

        Sem `branch_id` vale a filial padrao, a mesma de
        `POST /delivery/estimate` e de `GET /restaurants/{slug}/info` sem
        filial. Nao ha caminho que devolva o cardapio "do restaurante":
        juntar as lojas numa lista so mostraria a mesma picanha duas vezes,
        com dois precos e sem dizer qual e de qual.

        Restaurante sem filial ativa responde 200 com `products` e
        `categories` vazios — a vitrine (nome, banner, cupom) continua de pe,
        mas nao ha loja de onde tirar cardapio.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        settings = self.menu_repository.get_settings(restaurant.id)
        branch = self._resolve_menu_branch(restaurant.id, branch_id)
        operation = resolve_branch_operation(branch, settings) if branch else None

        return RestaurantMenuResponse(
            restaurant=self.restaurant_service.to_public_response(restaurant),
            branch_id=branch.id if branch else None,
            settings_branch_id=branch.id if branch else None,
            settings=(
                self._settings_response(
                    operation,
                    self._delivery_time_bands(branch),
                    self._cashback_terms(restaurant.id, branch),
                )
                if operation
                else None
            ),
            branches=[BranchResponse.model_validate(branch) for branch in self.menu_repository.get_active_branches(restaurant.id)],
            banners=[self._banner_response(banner) for banner in self.menu_repository.get_banners_by_type(restaurant.id, "hero")],
            highlight_banners=[
                self._banner_response(banner)
                for banner in self.menu_repository.get_banners_by_type(restaurant.id, "highlight")
            ],
            coupons=self._coupon_responses(restaurant.id),
            categories=self._category_responses(branch),
            products=self._product_responses_of_branch(branch),
        )

    def get_products_by_category(
        self,
        restaurant_slug: str,
        category_slug: str,
        branch_id: UUID | None = None,
    ) -> list[ProductResponse]:
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        branch = self._require_menu_branch(restaurant.id, branch_id)
        if not self.product_repository.active_category_exists(branch.id, category_slug):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Categoria não encontrada")
        products = self.product_repository.list_active_by_category_slug(branch.id, category_slug)
        return self._sellable_product_responses(products)

    def get_product_detail(
        self,
        restaurant_slug: str,
        product_slug: str,
        branch_id: UUID | None = None,
    ) -> ProductResponse:
        """O produto por slug, DENTRO de uma filial.

        Sem `branch_id` vale a filial padrao, e isso nao e detalhe: e o que
        faz os links ja divulgados continuarem funcionando. A migracao
        20260820_0026 deixou as linhas que ja existiam NA filial padrao —
        mesmos ids, mesmos slugs —, entao
        `/restaurants/junior-da-picanha/products/picanha` abre exatamente o
        produto que sempre abriu.

        Produto que so existe numa filial NAO padrao responde 404 pelo link
        sem parametro. E a resposta correta: sem filial nao ha preco a
        mostrar, e escolher uma loja por conta propria e o defeito que o
        passo 2 fechou.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        branch = self._require_menu_branch(restaurant.id, branch_id)
        product = self.product_repository.get_active_by_slug(branch.id, product_slug)
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produto não encontrado")

        # Fora de venda responde 404 igual a produto inexistente, e nao uma
        # mensagem propria: o link do produto e publico e compartilhavel, e
        # distinguir os dois casos aqui contaria a quem tem o link o que esta
        # acontecendo dentro da loja. O lojista descobre pelo /admin.
        blocking = blocking_required_group(product)
        if blocking is not None:
            logger.warning(
                "[Cardapio] produto fora de venda: grupo obrigatorio sem opcao ativa "
                "| product_id=%s | produto=%s | option_group_id=%s | grupo=%s",
                product.id,
                product.name,
                blocking.id,
                blocking.name,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Produto não encontrado")

        return self.product_response(product)

    def _resolve_menu_branch(
        self,
        restaurant_id: UUID,
        branch_id: UUID | None,
    ) -> Branch | None:
        """A filial de que o cardapio inteiro vai falar.

        404 para filial que nao e deste restaurante, e nao a filial padrao
        calada: o app que mandou o id errado precisa descobrir isso, senao
        ele mostra o cardapio e o valor minimo de outra loja achando que
        acertou.

        `None` (e nao 404) quando o restaurante nao tem filial ativa: a
        vitrine continua sendo respondida, so sem cardapio e sem o bloco de
        operacao — nao ha loja para eles descreverem.
        """
        if branch_id is None:
            return self.branch_repository.get_default_branch(restaurant_id)

        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Filial não encontrada para este restaurante",
            )
        return branch

    def _require_menu_branch(self, restaurant_id: UUID, branch_id: UUID | None) -> Branch:
        """Igual a `_resolve_menu_branch`, mas "sem filial" vira 404.

        As rotas de UM produto e de UMA categoria nao tem resposta parcial
        para dar: sem loja nao existe aquele produto nem aquele preco. O
        cardapio inteiro tem — ele ainda devolve a vitrine.
        """
        branch = self._resolve_menu_branch(restaurant_id, branch_id)
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Restaurante sem filial disponível",
            )
        return branch

    def _category_responses(self, branch: Branch | None) -> list[CategoryResponse]:
        if branch is None:
            return []
        return [
            CategoryResponse.model_validate(category)
            for category in self.menu_repository.get_active_categories(branch.id)
        ]

    def _product_responses_of_branch(self, branch: Branch | None) -> list[ProductResponse]:
        if branch is None:
            return []
        return self._sellable_product_responses(
            self.menu_repository.get_active_products(branch.id)
        )

    def _delivery_time_bands(self, branch: Branch | None) -> list[DeliveryTimeBandResponse]:
        """As faixas de prazo desta filial, na ordem que a regra usa.

        Uma consulta a mais no cardapio, e ela e paga com o que evita: sem as
        faixas aqui, a tela de cardapio (que ainda nao tem endereco) so tem o
        par unico de `estimated_delivery_time_*` para mostrar — o mesmo "30 a
        60 min" para o bairro da esquina e para o do outro lado da cidade.
        """
        if branch is None:
            return []
        return [
            DeliveryTimeBandResponse(
                max_distance_km=float(band.max_distance_km),
                delivery_time_min=band.delivery_time_min,
                delivery_time_max=band.delivery_time_max,
            )
            for band in self.branch_repository.list_delivery_time_bands(branch.id)
        ]

    def _cashback_terms(self, restaurant_id: UUID, branch: Branch) -> BranchCashbackTermsResponse:
        """Os termos de RESGATE daquela filial, para o app poder explicar o zero.

        `resolve_cashback_terms` e a mesma funcao que o checkout chama, e nao
        uma segunda leitura das mesmas tabelas: duas implementacoes da heranca
        discordariam sem erro, e a discordancia aqui e a tela prometendo um
        resgate que o pedido nao faz.

        Sem `momento`, entao o `percent` que volta e o de hoje — e e
        justamente por isso que ele NAO e publicado (ver
        `BranchCashbackTermsResponse`). Daqui sai so o que nao depende do dia.

        Uma consulta a mais no `/menu`, que e caminho quente:
        `get_rules_for_branch` traz as duas regras de uma vez, com os dias da
        semana em `selectinload`.
        """
        da_filial, do_restaurante = self.cashback_rule_repository.get_rules_for_branch(
            restaurant_id, branch.id
        )
        terms = resolve_cashback_terms(da_filial, do_restaurante)
        return BranchCashbackTermsResponse(
            enabled=terms.enabled,
            min_redeem_balance=money_to_float(terms.min_redeem_balance),
        )

    @staticmethod
    def _settings_response(
        operation: BranchOperation,
        delivery_time_bands: list[DeliveryTimeBandResponse],
        cashback: BranchCashbackTermsResponse,
    ) -> RestaurantSettingsResponse:
        """O bloco de operacao, ja resolvido para a filial.

        `payment_methods` saiu daqui na revisao 20260820_0027, junto com a
        coluna: quem manda em forma de pagamento e `branch_payment_methods`,
        por filial, em `GET /restaurants/{slug}/info?branch_id=...`. Enquanto
        ele foi ecoado aqui, o app que acreditasse nele mostrava ao cliente
        uma forma que o pedido recusa com 400.
        """
        return RestaurantSettingsResponse(
            min_order_value=money_to_float(operation.min_order_value),
            estimated_delivery_time_min=operation.estimated_delivery_time_min,
            estimated_delivery_time_max=operation.estimated_delivery_time_max,
            default_delivery_fee=money_to_float(operation.default_delivery_fee),
            service_fee_enabled=operation.service_fee_enabled,
            service_fee_amount=money_to_float(operation.service_fee_amount),
            accepts_delivery=operation.accepts_delivery,
            accepts_pickup=operation.accepts_pickup,
            is_open=operation.is_open,
            accepts_delivery_now=operation.accepts_delivery_now,
            delivery_paused_until=operation.delivery_paused_until,
            delivery_pause_reason=operation.delivery_pause_reason,
            free_delivery_enabled=operation.free_delivery_enabled,
            free_delivery_min_order_value=_optional_money(
                operation.free_delivery_min_order_value
            ),
            delivery_time_bands=delivery_time_bands,
            cashback=cashback,
        )

    @staticmethod
    def product_response(product: Product) -> ProductResponse:
        return ProductResponse(
            id=product.id,
            restaurant_id=product.restaurant_id,
            branch_id=product.branch_id,
            category_id=product.category_id,
            code=product.code,
            name=product.name,
            slug=product.slug,
            description=product.description,
            serves_people=product.serves_people,
            price=money_to_float(product.price),
            image_path=product.image_path,
            image_url=build_storage_url(product.image_path),
            is_active=product.is_active,
            is_available=product.is_available,
            sort_order=product.sort_order,
            option_groups=[
                MenuService._option_group_response(group)
                for group in MenuService._answerable_option_groups(product)
            ],
        )

    @staticmethod
    def _option_group_response(group: ProductOptionGroup) -> ProductOptionGroupResponse:
        return ProductOptionGroupResponse(
            id=group.id,
            name=group.name,
            description=group.description,
            min_select=group.min_select,
            max_select=group.max_select,
            is_required=group.is_required,
            sort_order=group.sort_order,
            options=[
                MenuService._option_response(option)
                for option in MenuService._in_menu_order(group.options)
            ],
        )

    @staticmethod
    def _option_response(option: ProductOption) -> ProductOptionResponse:
        return ProductOptionResponse(
            id=option.id,
            name=option.name,
            description=option.description,
            additional_price=money_to_float(option.additional_price),
            sort_order=option.sort_order,
        )

    @staticmethod
    def _in_menu_order(items: Sequence) -> list:
        """Os ativos, na ordem em que o lojista os pos no cardapio.

        O nome desempata `sort_order` repetido, e sem esse desempate a MESMA
        tela sairia com os adicionais em ordens diferentes entre duas cargas —
        quem confere o pedido pela posicao erraria (mesmo motivo do `sorted`
        da comanda, armadilha 14).

        `sort_order` nulo conta como zero, entao linha antiga sem ele
        preenchido vai para o comeco e nao para o fim.

        Uma funcao para os dois niveis: o filtro e a ordenacao eram identicos
        para grupo e para opcao, escritos duas vezes dentro de uma list
        comprehension aninhada na outra.
        """
        active = [item for item in items if item.is_active]
        return sorted(active, key=lambda item: (item.sort_order or 0, item.name))

    @staticmethod
    def _answerable_option_groups(product: Product) -> list:
        """Os grupos que o cliente CONSEGUE responder, na ordem do cardapio.

        Grupo sem nenhuma opcao ativa nao oferece escolha nenhuma, entao nao
        aparece. Isto aqui e cosmetico e vale so para o grupo OPCIONAL: quando
        o grupo vazio e obrigatorio, o produto inteiro sai de venda antes de
        chegar aqui (`services/menu_rules.blocking_required_group`).
        """
        com_escolha = [
            group
            for group in product.option_groups
            if any(option.is_active for option in group.options)
        ]
        return MenuService._in_menu_order(com_escolha)

    def _sellable_product_responses(self, products) -> list[ProductResponse]:
        """A lista do cardapio, sem os produtos que nao tem como ser vendidos."""
        responses = []
        for product in products:
            blocking = blocking_required_group(product)
            if blocking is not None:
                logger.warning(
                    "[Cardapio] produto fora de venda: grupo obrigatorio sem opcao ativa "
                    "| product_id=%s | produto=%s | option_group_id=%s | grupo=%s",
                    product.id,
                    product.name,
                    blocking.id,
                    blocking.name,
                )
                continue
            responses.append(self.product_response(product))
        return responses

    @staticmethod
    def _banner_response(banner) -> BannerResponse:
        return BannerResponse(
            id=banner.id,
            restaurant_id=banner.restaurant_id,
            banner_type=banner.banner_type,
            image_path=banner.image_path,
            image_url=build_storage_url(banner.image_path),
            sort_order=banner.sort_order,
            is_active=banner.is_active,
        )

    def _coupon_responses(self, restaurant_id) -> list[PublicCouponResponse]:
        """Os cupons da vitrine — sem os que nao tem como ser montados.

        UM CUPOM QUEBRADO NAO DERRUBA O CARDAPIO. `_coupon_response` le
        `coupon.template.image_path`, e um cupom ativo cujo template foi
        apagado levantava AttributeError bem no meio da comprehension que
        montava a lista — ou seja, a vitrine INTEIRA respondia 500. O cliente
        nao via "cupom indisponivel", via a loja fora do ar, e o lojista nao
        tinha como adivinhar que a causa era um cupom.

        O template ausente e sempre defeito de dado (a FK deveria impedir),
        entao ele e REGISTRADO no log em vez de ignorado em silencio: sem
        isso, o cupom que o lojista criou simplesmente nao apareceria e nao
        haveria onde procurar o porque.
        """
        responses = []
        for coupon in self.menu_repository.get_active_coupons(restaurant_id):
            if coupon.template is None:
                logger.warning(
                    "[Cardapio] cupom ativo sem template, fora da vitrine | coupon_id=%s | code=%s",
                    coupon.id,
                    coupon.code,
                )
                continue
            responses.append(self._coupon_response(coupon))
        return responses

    @staticmethod
    def _coupon_response(coupon) -> PublicCouponResponse:
        template = coupon.template
        return PublicCouponResponse(
            id=coupon.id,
            code=coupon.code,
            name=coupon.title,
            image_path=template.image_path,
            image_url=build_storage_url(template.image_path),
            discount_type=coupon.discount_type,
            discount_value=money_to_float(coupon.discount_value),
            min_order_value=money_to_float(coupon.min_order_value),
            sort_order=coupon.sort_order or 0,
            is_active=bool(coupon.is_active),
        )


def _optional_money(value) -> float | None:
    """Nulo continua nulo.

    `money_to_float(None)` devolveria 0.00, e aqui isso nao seria so feio: o
    app leria "frete gratis acima de R$ 0" — ou seja, sempre gratis — onde a
    verdade e "esta filial nao tem campanha de frete gratis".
    """
    return money_to_float(value) if value is not None else None
