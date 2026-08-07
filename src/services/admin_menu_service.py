"""Regras do cardapio no painel (BLOCO B da Fase 3).

Duas decisoes valem para o arquivo inteiro:

1. **Nada e apagado de verdade.** Categoria, produto, grupo e opcao so sao
   ligados e desligados (`is_active`). `order_items` e `order_item_options`
   referenciam essas linhas por FK, e um DELETE quebraria o historico de
   pedido que o cliente ainda consulta. "Excluir" no painel e desativar.

2. **Cardapio nao tem filial.** As tabelas de categoria e produto so tem
   `restaurant_id`; nao existe coluna de filial para restringir. Entao um
   manager preso a uma filial edita o cardapio do restaurante inteiro —
   igual ao que ja acontece com cupom, que tambem e do restaurante. O
   escopo por filial vale para o que TEM filial: pedidos e configuracao de
   filial.
"""

import uuid

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.category_model import Category
from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.repositories.admin_menu_repository import AdminMenuRepository
from src.schemas.admin_menu_schema import (
    AdminCategoryCreate,
    AdminCategoryResponse,
    AdminCategoryUpdate,
    AdminOptionCreate,
    AdminOptionGroupCreate,
    AdminOptionGroupFields,
    AdminOptionGroupResponse,
    AdminOptionGroupUpdate,
    AdminOptionResponse,
    AdminOptionUpdate,
    AdminProductCreate,
    AdminProductDetailResponse,
    AdminProductListResponse,
    AdminProductResponse,
    AdminProductUpdate,
    CategoryReorderRequest,
    ProductAvailabilityRequest,
)
from src.utils.money import money_to_float, quantize_money
from src.utils.normalization import slugify
from src.utils.storage import build_storage_url


# Mesmo teto da busca de pedidos: nao e regra de negocio, e para nao montar
# um ILIKE de dez mil caracteres vindo da querystring.
MAX_SEARCH_LENGTH = 120


class AdminMenuService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AdminMenuRepository(db)

    def list_categories(self, scope: AdminScope) -> list[AdminCategoryResponse]:
        categories = self.repository.list_categories(scope.restaurant_id)
        return [AdminCategoryResponse.model_validate(category) for category in categories]

    def create_category(
        self,
        scope: AdminScope,
        payload: AdminCategoryCreate,
    ) -> AdminCategoryResponse:
        slug = self._build_slug(payload.name)
        self._ensure_category_slug_is_free(slug, scope.restaurant_id)

        category = Category(
            restaurant_id=scope.restaurant_id,
            name=payload.name,
            slug=slug,
            sort_order=payload.sort_order,
            is_active=payload.is_active,
        )
        self._commit(lambda: self.repository.add_category(category))
        return AdminCategoryResponse.model_validate(category)

    def update_category(
        self,
        scope: AdminScope,
        category_id: uuid.UUID,
        payload: AdminCategoryUpdate,
    ) -> AdminCategoryResponse:
        category = self._get_category(category_id, scope)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(category, field, value)
        self._commit()
        return AdminCategoryResponse.model_validate(category)

    def reorder_categories(
        self,
        scope: AdminScope,
        payload: CategoryReorderRequest,
    ) -> list[AdminCategoryResponse]:
        """Renumera as categorias na ordem em que vieram no corpo.

        Exige a lista COMPLETA do restaurante. Renumerar so uma parte
        deixaria as de fora com `sort_order` repetido, e a ordem final do
        cardapio passaria a depender do desempate por nome — que nao e o que
        o lojista arrastou na tela. Quem receber este 400 recarrega a lista:
        e o sinal de que alguem criou categoria em outra aba.
        """
        existing = self.repository.list_categories(scope.restaurant_id)
        existing_ids = {category.id for category in existing}
        sent_ids = set(payload.category_ids)

        unknown = sent_ids - existing_ids
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Categoria nao encontrada"
            )
        if existing_ids - sent_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Envie todas as categorias do restaurante na nova ordem",
            )

        by_id = {category.id: category for category in existing}
        for position, category_id in enumerate(payload.category_ids):
            by_id[category_id].sort_order = position
        self._commit()
        return [
            AdminCategoryResponse.model_validate(by_id[category_id])
            for category_id in payload.category_ids
        ]

    def list_products(
        self,
        scope: AdminScope,
        category_id: uuid.UUID | None = None,
        search: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AdminProductListResponse:
        normalized_search = self._normalize_search(search)
        products = self.repository.list_products(
            restaurant_id=scope.restaurant_id,
            category_id=category_id,
            search=normalized_search,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )
        total = self.repository.count_products(
            restaurant_id=scope.restaurant_id,
            category_id=category_id,
            search=normalized_search,
            is_active=is_active,
        )
        return AdminProductListResponse(
            items=[self._product_response(product) for product in products],
            total=total,
            limit=limit,
            offset=offset,
        )

    def get_product(self, scope: AdminScope, product_id: uuid.UUID) -> AdminProductDetailResponse:
        product = self.repository.get_product_with_options(product_id, scope.restaurant_id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Produto nao encontrado"
            )
        return AdminProductDetailResponse(
            **self._product_response(product).model_dump(),
            option_groups=[
                self._option_group_response(group)
                for group in sorted(
                    product.option_groups,
                    key=lambda group: (group.sort_order or 0, group.name),
                )
            ],
        )

    def create_product(
        self,
        scope: AdminScope,
        payload: AdminProductCreate,
    ) -> AdminProductResponse:
        self._get_category(payload.category_id, scope)
        slug = self._build_slug(payload.name)
        self._ensure_product_slug_is_free(slug, scope.restaurant_id)

        product = Product(
            restaurant_id=scope.restaurant_id,
            category_id=payload.category_id,
            name=payload.name,
            slug=slug,
            description=payload.description,
            price=quantize_money(payload.price),
            code=payload.code,
            is_active=payload.is_active,
            is_available=payload.is_available,
            sort_order=payload.sort_order,
        )
        self._commit(lambda: self.repository.add_product(product))
        return self._product_response(product)

    def update_product(
        self,
        scope: AdminScope,
        product_id: uuid.UUID,
        payload: AdminProductUpdate,
    ) -> AdminProductResponse:
        product = self._get_product(product_id, scope)
        changes = payload.model_dump(exclude_unset=True)
        # Mover de categoria so vale para categoria do mesmo restaurante,
        # senao o produto sumiria do cardapio de quem o cadastrou.
        if "category_id" in changes:
            self._get_category(changes["category_id"], scope)
        if "price" in changes and changes["price"] is not None:
            changes["price"] = quantize_money(changes["price"])

        for field, value in changes.items():
            setattr(product, field, value)
        self._commit()
        return self._product_response(product)

    def set_product_availability(
        self,
        scope: AdminScope,
        product_id: uuid.UUID,
        payload: ProductAvailabilityRequest,
    ) -> AdminProductResponse:
        """Acao rapida de esgotado/disponivel (BLOCO B4).

        Nao mexe em `is_active`: "acabou hoje" e diferente de "saiu do
        cardapio". O cardapio publico ja esconde os dois, mas so o
        `is_available` volta sozinho na cabeca do lojista amanha de manha.
        """
        product = self._get_product(product_id, scope)
        product.is_available = payload.is_available
        self._commit()
        return self._product_response(product)

    def list_option_groups(
        self,
        scope: AdminScope,
        product_id: uuid.UUID,
    ) -> list[AdminOptionGroupResponse]:
        self._get_product(product_id, scope)
        groups = self.repository.list_option_groups(product_id)
        return [self._option_group_response(group) for group in groups]

    def create_option_group(
        self,
        scope: AdminScope,
        product_id: uuid.UUID,
        payload: AdminOptionGroupCreate,
    ) -> AdminOptionGroupResponse:
        self._get_product(product_id, scope)
        group = ProductOptionGroup(product_id=product_id, **payload.model_dump())
        self._commit(lambda: self.repository.add_option_group(group))
        return self._option_group_response(group)

    def update_option_group(
        self,
        scope: AdminScope,
        group_id: uuid.UUID,
        payload: AdminOptionGroupUpdate,
    ) -> AdminOptionGroupResponse:
        """Edicao parcial validada sobre o resultado da mescla.

        Validar so o que veio no corpo deixaria passar um max_select=1
        enviado sozinho para um grupo que ja tem min_select=3 — cada campo
        valido isolado, o par impossivel.
        """
        group = self._get_option_group(group_id, scope)
        merged = {
            "name": group.name,
            "description": group.description,
            "min_select": group.min_select,
            "max_select": group.max_select,
            "is_required": group.is_required,
            # Nullable no schema antigo; a validacao exige inteiro.
            "sort_order": group.sort_order or 0,
            "is_active": group.is_active,
        }
        merged.update(payload.model_dump(exclude_unset=True))
        try:
            validated = AdminOptionGroupFields.model_validate(merged)
        except ValidationError as exc:
            # O Pydantic so vira 422 automatico quando recusa o CORPO da
            # requisicao. Esta validacao acontece depois, sobre a mescla com
            # o banco: sem traduzir, um par (min, max) impossivel sairia
            # como 500 e o painel nao teria o que mostrar ao lojista.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="; ".join(error["msg"] for error in exc.errors()),
            ) from exc

        for field, value in validated.model_dump().items():
            setattr(group, field, value)
        self._commit()
        return self._option_group_response(group)

    def create_option(
        self,
        scope: AdminScope,
        group_id: uuid.UUID,
        payload: AdminOptionCreate,
    ) -> AdminOptionResponse:
        self._get_option_group(group_id, scope)
        option = ProductOption(
            option_group_id=group_id,
            name=payload.name,
            description=payload.description,
            additional_price=quantize_money(payload.additional_price),
            sort_order=payload.sort_order,
            is_active=payload.is_active,
        )
        self._commit(lambda: self.repository.add_option(option))
        return self._option_response(option)

    def update_option(
        self,
        scope: AdminScope,
        option_id: uuid.UUID,
        payload: AdminOptionUpdate,
    ) -> AdminOptionResponse:
        option = self._get_option(option_id, scope)
        changes = payload.model_dump(exclude_unset=True)
        if "additional_price" in changes and changes["additional_price"] is not None:
            changes["additional_price"] = quantize_money(changes["additional_price"])

        for field, value in changes.items():
            setattr(option, field, value)
        self._commit()
        return self._option_response(option)

    def _get_category(self, category_id: uuid.UUID, scope: AdminScope) -> Category:
        category = self.repository.get_category(category_id, scope.restaurant_id)
        if category is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Categoria nao encontrada"
            )
        return category

    def _get_product(self, product_id: uuid.UUID, scope: AdminScope) -> Product:
        product = self.repository.get_product(product_id, scope.restaurant_id)
        if product is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Produto nao encontrado"
            )
        return product

    def _get_option_group(self, group_id: uuid.UUID, scope: AdminScope) -> ProductOptionGroup:
        group = self.repository.get_option_group(group_id, scope.restaurant_id)
        if group is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de opcoes nao encontrado"
            )
        return group

    def _get_option(self, option_id: uuid.UUID, scope: AdminScope) -> ProductOption:
        option = self.repository.get_option(option_id, scope.restaurant_id)
        if option is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Opcao nao encontrada"
            )
        return option

    def _ensure_category_slug_is_free(self, slug: str, restaurant_id: uuid.UUID) -> None:
        if self.repository.get_category_by_slug(slug, restaurant_id) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ja existe uma categoria com esse nome neste restaurante",
            )

    def _ensure_product_slug_is_free(self, slug: str, restaurant_id: uuid.UUID) -> None:
        # O slug e a chave da URL publica do produto e a busca do cardapio
        # aceita so um resultado: dois produtos com o mesmo slug fariam o
        # link publico apontar para um deles sem criterio.
        if self.repository.get_product_by_slug(slug, restaurant_id) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ja existe um produto com esse nome neste restaurante",
            )

    def _commit(self, before_commit=None) -> None:
        """Grava, com rollback em qualquer falha.

        Recebe o que precisa ser feito ANTES do commit (o `add` + `flush` do
        repositorio, quando e criacao) para que insercao e commit fiquem no
        mesmo try — uma falha no flush precisa desfazer a sessao do mesmo
        jeito que uma falha no commit.
        """
        try:
            if before_commit is not None:
                before_commit()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _build_slug(name: str) -> str:
        slug = slugify(name)
        if not slug:
            # Nome so de emoji ou de pontuacao: o slug sairia vazio e a URL
            # publica do cardapio ficaria quebrada.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O nome precisa ter ao menos uma letra ou numero",
            )
        return slug

    @staticmethod
    def _normalize_search(search: str | None) -> str | None:
        if search is None:
            return None
        cleaned = search.strip()
        if not cleaned:
            return None
        return cleaned[:MAX_SEARCH_LENGTH]

    @staticmethod
    def _product_response(product: Product) -> AdminProductResponse:
        return AdminProductResponse(
            id=product.id,
            category_id=product.category_id,
            code=product.code,
            name=product.name,
            slug=product.slug,
            description=product.description,
            price=money_to_float(product.price),
            image_path=product.image_path,
            image_url=build_storage_url(product.image_path),
            is_active=product.is_active,
            is_available=product.is_available,
            sort_order=product.sort_order,
        )

    @staticmethod
    def _option_group_response(group: ProductOptionGroup) -> AdminOptionGroupResponse:
        return AdminOptionGroupResponse(
            id=group.id,
            product_id=group.product_id,
            name=group.name,
            description=group.description,
            min_select=group.min_select,
            max_select=group.max_select,
            is_required=group.is_required,
            sort_order=group.sort_order,
            is_active=group.is_active,
            options=[
                AdminMenuService._option_response(option)
                # O painel lista inclusive a opcao desativada, entao a
                # ordenacao acontece aqui e nao no filtro do cardapio.
                for option in sorted(
                    group.options,
                    key=lambda option: (option.sort_order or 0, option.name),
                )
            ],
        )

    @staticmethod
    def _option_response(option: ProductOption) -> AdminOptionResponse:
        return AdminOptionResponse(
            id=option.id,
            option_group_id=option.option_group_id,
            name=option.name,
            description=option.description,
            additional_price=money_to_float(option.additional_price),
            sort_order=option.sort_order,
            is_active=option.is_active,
        )
