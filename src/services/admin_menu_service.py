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

import secrets
import uuid

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.core.config import settings
from src.integrations.supabase_storage_client import (
    SupabaseStorageClient,
    SupabaseStorageError,
    SupabaseStorageNotConfiguredError,
)
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
    ProductImageResponse,
    ProductReorderRequest,
)
from src.repositories.restaurant_repository import RestaurantRepository
from src.services.menu_rules import blocking_required_group
from src.utils.images import IMAGE_CONTENT_TYPES, detect_image_extension
from src.utils.money import money_to_float, quantize_money
from src.utils.normalization import slugify
from src.utils.storage import build_storage_url


# Mesmo teto da busca de pedidos: nao e regra de negocio, e para nao montar
# um ILIKE de dez mil caracteres vindo da querystring.
MAX_SEARCH_LENGTH = 120

# Pasta da imagem de produto dentro do bucket, depois do slug do
# restaurante. Mantem a estrutura que ja existe hoje: <slug>/products/.
PRODUCT_IMAGE_FOLDER = "products"


class AdminMenuService:
    def __init__(self, db: Session, storage_client: SupabaseStorageClient | None = None):
        self.db = db
        self.repository = AdminMenuRepository(db)
        self.restaurant_repository = RestaurantRepository(db)
        self.storage_client = storage_client or SupabaseStorageClient(
            base_url=settings.SUPABASE_URL,
            bucket=settings.SUPABASE_STORAGE_BUCKET,
            service_key=settings.SUPABASE_SERVICE_ROLE_KEY,
            timeout_seconds=settings.SUPABASE_STORAGE_TIMEOUT_SECONDS,
        )

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

    def reorder_products(
        self,
        scope: AdminScope,
        payload: ProductReorderRequest,
    ) -> list[AdminProductResponse]:
        """Renumera os produtos de UMA categoria na ordem em que vieram.

        Mesma regra da reordenacao de categorias, um nivel abaixo: exige a
        lista completa e renumera de 0 a N-1. O conjunto que compartilha a
        numeracao aqui e a categoria, nao o restaurante — ver
        ProductReorderRequest.

        A categoria e conferida contra o escopo ANTES dos produtos. Sem isso,
        um `category_id` de outro restaurante com `product_ids` vazio de
        correspondencia devolveria 400 ("envie todos os produtos") em vez de
        404, e a diferenca entre as duas respostas contaria ao chamador que
        aquela categoria existe.

        Categoria com mais produtos que MAX_REORDER_ITEMS nao passa pelo
        contrato (422 no schema). E o mesmo teto da reordenacao de
        categorias; se aparecer catalogo desse tamanho, o que muda e o
        contrato, nao esta regra.
        """
        category = self._get_category(payload.category_id, scope)

        existing = self.repository.list_products_by_category(
            category.id, scope.restaurant_id
        )
        existing_ids = {product.id for product in existing}
        sent_ids = set(payload.product_ids)

        if sent_ids - existing_ids:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Produto nao encontrado nesta categoria",
            )
        if existing_ids - sent_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Envie todos os produtos da categoria na nova ordem",
            )

        by_id = {product.id: product for product in existing}
        for position, product_id in enumerate(payload.product_ids):
            by_id[product_id].sort_order = position
        self._commit()
        bloqueados = self.repository.product_ids_blocked_by_required_group(list(payload.product_ids))
        return [
            self._product_response(
                by_id[product_id],
                unavailable_by_required_group=product_id in bloqueados,
            )
            for product_id in payload.product_ids
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
        bloqueados = self.repository.product_ids_blocked_by_required_group(
            [product.id for product in products]
        )
        return AdminProductListResponse(
            items=[
                self._product_response(
                    product,
                    unavailable_by_required_group=product.id in bloqueados,
                )
                for product in products
            ],
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
            **self._one_product_response(product).model_dump(),
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
        return self._one_product_response(product)

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
        return self._one_product_response(product)

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
        return self._one_product_response(product)

    def upload_product_image(
        self,
        scope: AdminScope,
        product_id: uuid.UUID,
        content: bytes,
    ) -> ProductImageResponse:
        """Grava a foto do produto no bucket e aponta o produto para ela.

        A ordem importa: o objeto vai para o Storage ANTES do commit. Se
        gravasse `image_path` primeiro, uma falha do Storage deixaria o
        produto apontando para um arquivo que nao existe, e o cardapio
        publico mostraria imagem quebrada. Falhando aqui, o produto fica
        com a imagem antiga — que e o pior caso aceitavel.

        O objeto antigo nao e apagado. E de proposito: o CDN do Supabase
        pode ter a URL antiga em cache por minutos, e apagar na hora deixa
        o cardapio sem imagem nesse intervalo. Sobra lixo no bucket, e o
        preco por nunca mostrar imagem quebrada ao cliente.
        """
        product = self._get_product(product_id, scope)
        extension = self._validate_image(content)

        restaurant = self.restaurant_repository.get_by_id(scope.restaurant_id)
        if restaurant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Restaurante nao encontrado"
            )

        # O sufixo aleatorio existe para o CDN: reusar o mesmo caminho
        # faria o cliente continuar vendo a foto anterior ate o cache
        # expirar, e o lojista trocaria a imagem de novo achando que falhou.
        object_path = (
            f"{restaurant.slug}/{PRODUCT_IMAGE_FOLDER}/"
            f"{product.id}-{secrets.token_hex(4)}.{extension}"
        )
        try:
            self.storage_client.upload(
                object_path,
                content,
                IMAGE_CONTENT_TYPES[extension],
            )
        except SupabaseStorageNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Upload de imagem indisponivel: storage nao configurado",
            ) from exc
        except SupabaseStorageError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Nao foi possivel enviar a imagem agora",
            ) from exc

        product.image_path = object_path
        self._commit()
        return ProductImageResponse(
            image_path=object_path,
            image_url=build_storage_url(object_path),
        )

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
    def _validate_image(content: bytes) -> str:
        """Confere tamanho e tipo real do arquivo, e devolve a extensao.

        O tamanho e conferido aqui TAMBEM, alem do middleware: o middleware
        corta o corpo inteiro do multipart (arquivo + delimitadores), e o
        que interessa gravar no bucket e o arquivo. Sao dois limites
        diferentes medindo coisas diferentes.

        O tipo sai dos bytes, nunca do `content_type` do multipart — ver
        src/utils/images.py.
        """
        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Arquivo vazio"
            )
        if len(content) > settings.MAX_IMAGE_UPLOAD_BYTES:
            limit_mb = settings.MAX_IMAGE_UPLOAD_BYTES / 1_048_576
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"A imagem excede o tamanho maximo de {limit_mb:.0f} MB",
            )

        extension = detect_image_extension(content)
        if extension is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Formato invalido: envie JPEG, PNG ou WEBP",
            )
        return extension

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
    def _one_product_response(product: Product) -> AdminProductResponse:
        """A resposta de uma rota que mexe em UM produto.

        Aqui ler `product.option_groups` custa uma consulta, num produto so —
        e vale a pena: o lojista que acabou de salvar precisa ver na hora que
        o produto saiu de venda.
        """
        return AdminMenuService._product_response(
            product,
            unavailable_by_required_group=blocking_required_group(product) is not None,
        )

    @staticmethod
    def _product_response(
        product: Product,
        *,
        unavailable_by_required_group: bool,
    ) -> AdminProductResponse:
        """O sinal e OBRIGATORIO no parametro, e nao calculado aqui dentro.

        Calcular aqui leria `product.option_groups`, e na LISTAGEM isso
        dispararia uma consulta por produto — exatamente o que a listagem
        evita nao carregando os grupos. Cada quem chama diz o que sabe: a
        listagem passa o resultado da consulta agregada, as rotas de um
        produto so calculam pela regra.
        """
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
            printing_sector_id=product.printing_sector_id,
            unavailable_by_required_group=unavailable_by_required_group,
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
