"""O que entra no indice do Rapi, e a decisao de reindexar ou so carimbar.

Compartilhado entre a varredura continua (`scripts/reindex_worker.py`) e a
completa (`scripts/reindex_ai.py`). Nao e organizacao: o `content_hash`
depende da formatacao EXATA do texto, entao duas copias que divergissem por um
espaco fariam a segunda varredura achar que TODO produto mudou — um embedding
pago por produto para gravar o mesmo vetor de novo.
"""

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from src.ai.services.embedding_service import EmbeddingService
from src.models.product_model import Product
from src.repositories.ai_repository import AIRepository


# "touched" e o caso em que o produto mudou mas o que vai para o vetor nao —
# mexeram no preco, no `sort_order`, na imagem. Nao gera embedding.
IndexOutcome = Literal["created", "updated", "touched"]


def index_product(
    repository: AIRepository,
    embedding_service: EmbeddingService,
    product: Product,
    category_name: str | None,
    source_updated_at: datetime,
) -> IndexOutcome:
    """Poe UM produto em dia no indice. Nao commita — quem commita e quem chama.

    `source_updated_at` e a VERSAO do produto que este indice passa a
    refletir, e nao a hora em que o indexamos. Ver `AIRepository.save_embedding`
    para o que muda com isso.
    """
    content = build_product_content(product, category_name)
    content_hash = build_content_hash(content)
    metadata = build_product_metadata(product, category_name)
    current = repository.get_embedding_by_product(
        restaurant_id=product.restaurant_id,
        product_id=product.id,
    )

    if current and current["content_hash"] == content_hash:
        repository.touch_embedding(
            restaurant_id=product.restaurant_id,
            product_id=product.id,
            metadata=metadata,
            source_updated_at=source_updated_at,
        )
        return "touched"

    embedding = embedding_service.generate_embedding(content)
    repository.save_embedding(
        restaurant_id=product.restaurant_id,
        product_id=product.id,
        content=content,
        content_hash=content_hash,
        metadata=metadata,
        embedding=embedding,
        source_updated_at=source_updated_at,
    )

    if current:
        return "updated"
    return "created"


def build_product_content(product: Product, category_name: str | None) -> str:
    """O texto que vira vetor. So o que ajuda a ACHAR o produto.

    `Preco` e `Disponivel` NAO estao aqui, e nao e economia de bytes.

    Sao os dois campos que mais mudam num restaurante — "acabou o X" e o toque
    mais frequente do dia no painel — e nenhum dos dois ajuda a busca
    semantica: ninguem procura por "23,90". Dentro do conteudo, cada toggle de
    disponibilidade mudava o `content_hash` e comprava um embedding novo. Fora
    dele, a varredura passa, o hash bate, e a linha so leva o carimbo.

    A disponibilidade ainda e respeitada onde importa: `similarity_search`
    filtra por `is_available` no momento da busca. Dentro do vetor ela era
    redundante alem de cara.

    `Slug` saiu por outro motivo: e derivado do nome, era ruido duplicado.

    Mexer nos campos ou na formatacao daqui invalida TODO `content_hash` ja
    gravado e faz a proxima varredura reindexar o cardapio inteiro. E o
    esperado quando a mudanca e proposital; so nao pode ser sem querer.
    """
    fields = [
        ("Nome", product.name),
        ("Codigo", product.code),
        ("Categoria", category_name),
        ("Descricao", product.description),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value not in (None, ""))


def build_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_product_metadata(product: Product, category_name: str | None) -> dict[str, Any]:
    """Os campos que viajam junto com o vetor, sem entrar nele.

    `price` e `is_available` continuam aqui, mesmo tendo saido do conteudo: e
    barato manter, e e o unico lugar em que a tabela do indice guarda o dado.
    Por isso o caminho "touched" tambem reescreve o metadata — senao o preco
    desta tabela congelaria no valor do dia em que o texto mudou pela ultima
    vez.
    """
    return {
        "product_id": str(product.id),
        "restaurant_id": str(product.restaurant_id),
        "category_id": str(product.category_id),
        "category_name": category_name,
        "name": product.name,
        "slug": product.slug,
        "price": _decimal_to_string(product.price),
        "is_available": product.is_available,
    }


def _decimal_to_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
