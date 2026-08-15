"""Varredura COMPLETA do indice do Rapi, rodada a mao.

O dia a dia nao depende mais deste arquivo: quem mantem o indice em dia e
`scripts/reindex_worker.py`, que roda em container proprio a cada minuto. Este
aqui continua existindo para as duas situacoes em que se quer o trabalho todo
agora, sem esperar os lotes:

    restaurante novo, com o cardapio inteiro recem-importado;
    troca de EMBEDDING_MODEL, que invalida todo vetor ja gravado.

Ele usa a MESMA consulta do worker (`AIRepository.list_stale_products`) e o
MESMO codigo de indexacao (`src.ai.services.product_indexing`), so que em lotes
seguidos ate nao sobrar pendencia. Duplicar a regra aqui — como era antes —
significaria duas definicoes do que e "estar em dia", e a que estivesse errada
so apareceria no dia em que as duas discordassem.
"""

import sys
from dataclasses import dataclass, replace
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ai.services.chat_cache import menu_generation
from src.ai.services.embedding_service import EmbeddingService
from src.ai.services.product_indexing import index_product
from src.db.session import SessionLocal
from src.models.category_model import Category
from src.models.product_model import Product
from src.repositories.ai_repository import AIRepository


TAMANHO_DO_LOTE = 100


@dataclass(frozen=True)
class ReindexStats:
    created: int = 0
    updated: int = 0
    touched: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.created + self.updated + self.touched + self.failed


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
        f"touched={stats.touched}, "
        f"failed={stats.failed}"
    )


def reindex_embeddings(db: Session, batch_size: int = TAMANHO_DO_LOTE) -> ReindexStats:
    """Lotes ate nao sobrar produto atrasado.

    Produto que ja esta em dia nao volta na consulta, entao nao ha o que pular:
    o laco termina quando a consulta vem vazia.

    A outra saida — lote inteiro sem nenhuma escrita — existe para o unico caso
    em que o laco nao terminaria sozinho: um produto que falha sempre (uma
    descricao que a API de embedding recusa, por exemplo) continuaria pendente
    para sempre e seria devolvido em todo lote. Sem esta condicao, `python
    scripts/reindex_ai.py` giraria indefinidamente contra o mesmo item.
    """
    repository = AIRepository(db)
    embedding_service = EmbeddingService()
    stats = ReindexStats()

    while True:
        pendentes = repository.list_stale_products(limit=batch_size)
        if not pendentes:
            return stats

        anterior = stats
        for pendente in pendentes:
            stats = _indexar_um(db, repository, embedding_service, pendente, stats)

        if _nada_foi_escrito(anterior, stats):
            print(f"AI embeddings reindex interrompido: {len(pendentes)} produto(s) falham sempre")
            return stats


def _indexar_um(
    db: Session,
    repository: AIRepository,
    embedding_service: EmbeddingService,
    pendente: dict,
    stats: ReindexStats,
) -> ReindexStats:
    """Um produto, uma transacao — igual ao worker, e pelo mesmo motivo."""
    linha = _carregar_produto(db, pendente["product_id"])
    if linha is None:
        return stats

    produto, category_name = linha
    try:
        resultado = index_product(
            repository=repository,
            embedding_service=embedding_service,
            product=produto,
            category_name=category_name,
            source_updated_at=pendente["source_updated_at"],
        )
        db.commit()
    except Exception as erro:
        db.rollback()
        print(f"AI embeddings reindex falhou | product_id={pendente['product_id']} | {erro}")
        return replace(stats, failed=stats.failed + 1)

    menu_generation.bump(produto.restaurant_id)
    if resultado == "created":
        return replace(stats, created=stats.created + 1)
    if resultado == "updated":
        return replace(stats, updated=stats.updated + 1)
    return replace(stats, touched=stats.touched + 1)


def _carregar_produto(db: Session, product_id) -> tuple[Product, str | None] | None:
    stmt = (
        select(Product, Category.name)
        .outerjoin(Category, Product.category_id == Category.id)
        .where(Product.id == product_id)
    )
    linha = db.execute(stmt).one_or_none()
    if linha is None:
        return None
    return linha[0], linha[1]


def _nada_foi_escrito(anterior: ReindexStats, agora: ReindexStats) -> bool:
    escritas_antes = anterior.created + anterior.updated + anterior.touched
    escritas_agora = agora.created + agora.updated + agora.touched
    return escritas_agora == escritas_antes


if __name__ == "__main__":
    main()
