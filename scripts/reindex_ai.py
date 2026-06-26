import hashlib
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ai.services.embedding_service import EmbeddingService
from src.db.session import SessionLocal
from src.models.category_model import Category
from src.models.product_model import Product
from src.repositories.ai_repository import AIRepository


@dataclass(frozen=True)
class ReindexStats:
    created: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def processed(self) -> int:
        return self.created + self.updated + self.skipped


def main() -> None:
    db = SessionLocal()
    try:
        stats = reindex_embeddings(db)
    finally:
        db.close()

    print(
        "AI embeddings reindex completed: "
        f"processed={stats.processed}, "
        f"created={stats.created}, "
        f"updated={stats.updated}, "
        f"skipped={stats.skipped}"
    )


def reindex_embeddings(db: Session) -> ReindexStats:
    repository = AIRepository(db)
    embedding_service = EmbeddingService()
    stats = ReindexStats()

    for product, category_name in _iter_active_products(db):
        content = _build_product_content(product, category_name)
        content_hash = _build_content_hash(content)
        current_embedding = repository.get_embedding_by_product(
            restaurant_id=product.restaurant_id,
            product_id=product.id,
        )

        if current_embedding and current_embedding["content_hash"] == content_hash:
            stats = ReindexStats(
                created=stats.created,
                updated=stats.updated,
                skipped=stats.skipped + 1,
            )
            continue

        embedding = embedding_service.generate_embedding(content)
        repository.save_embedding(
            restaurant_id=product.restaurant_id,
            product_id=product.id,
            content=content,
            content_hash=content_hash,
            metadata=_build_product_metadata(product, category_name),
            embedding=embedding,
        )

        if current_embedding:
            stats = ReindexStats(
                created=stats.created,
                updated=stats.updated + 1,
                skipped=stats.skipped,
            )
        else:
            stats = ReindexStats(
                created=stats.created + 1,
                updated=stats.updated,
                skipped=stats.skipped,
            )

    return stats


def _iter_active_products(db: Session) -> list[tuple[Product, str | None]]:
    stmt = (
        select(Product, Category.name)
        .outerjoin(Category, Product.category_id == Category.id)
        .where(Product.is_active.is_(True))
        .order_by(Product.restaurant_id.asc(), Product.name.asc())
    )
    return list(db.execute(stmt).all())


def _build_product_content(product: Product, category_name: str | None) -> str:
    fields = [
        ("Nome", product.name),
        ("Codigo", product.code),
        ("Slug", product.slug),
        ("Categoria", category_name),
        ("Descricao", product.description),
        ("Preco", _decimal_to_string(product.price)),
        ("Disponivel", _bool_to_text(product.is_available)),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value not in (None, ""))


def _build_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_product_metadata(product: Product, category_name: str | None) -> dict[str, Any]:
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


def _bool_to_text(value: bool | None) -> str | None:
    if value is None:
        return None
    return "sim" if value else "nao"


if __name__ == "__main__":
    main()
