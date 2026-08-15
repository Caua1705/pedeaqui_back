"""Acesso ao indice vetorial do Rapi (`ai_product_embeddings`).

SOBRE `ai_product_embeddings.updated_at`. A coluna nao significa "quando
escrevi esta linha", e sim **qual versao do produto este vetor reflete** — o
`GREATEST(products.updated_at, categories.updated_at)` que estava valendo
quando o conteudo foi lido. E o que faz `list_stale_products` funcionar; ver
`save_embedding` para o bug que a leitura ingenua causava.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from src.models.ai_product_embedding_model import AIProductEmbedding


class AIRepository:
    """Database access for AI product embeddings."""

    def __init__(self, db: Session):
        self.db = db

    def similarity_search(
        self,
        restaurant_id: uuid.UUID,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the most similar active products for one restaurant."""
        stmt = text(
            """
            SELECT
                p.id,
                p.restaurant_id,
                p.name,
                p.slug,
                p.description,
                p.price,
                p.image_path,
                ape.metadata AS metadata,
                1 - (ape.embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM ai_product_embeddings ape
            JOIN products p ON p.id = ape.product_id
            WHERE
                ape.restaurant_id = :restaurant_id
                AND p.restaurant_id = :restaurant_id
                AND p.is_active IS TRUE
                AND p.is_available IS TRUE
            ORDER BY ape.embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
            """
        )
        rows = self.db.execute(
            stmt,
            {
                "restaurant_id": restaurant_id,
                "embedding": self._format_vector(embedding),
                "top_k": top_k,
            },
        ).mappings()
        return [dict(row) for row in rows]

    def list_stale_products(self, limit: int) -> list[dict[str, Any]]:
        """Os produtos cujo indice esta atras do cardapio, do mais antigo ao menos.

        Tres condicoes, e cada uma cobre um caso diferente:

        - **`ape.product_id IS NULL`** — produto NOVO, nunca indexado. Era o
          pior efeito do reindex manual: o produto entrava no cardapio e o Rapi
          simplesmente nao o conhecia, sem erro nenhum em lugar nenhum.
        - **`ape.updated_at < p.updated_at`** — produto editado. Quem mantem
          esse timestamp e o TRIGGER `trg_products_updated_at`, no banco, e nao
          a aplicacao. E por isso que a varredura pega tambem o que muda por
          fora do painel: SQL manual, script de importacao, rota nova que
          ninguem lembrou de instrumentar.
        - **`ape.updated_at < c.updated_at`** — CATEGORIA renomeada.
          `update_category` nao toca em `products.updated_at`, entao sem esta
          condicao o `category_name` — que vai no conteudo indexado E no
          metadata, e e o que o modelo le — ficaria congelado para sempre.

        `source_updated_at` e o GREATEST das duas datas justamente porque e ele
        que volta para `ape.updated_at`. Gravar so `p.updated_at` depois de uma
        renomeacao de categoria deixaria `ape.updated_at < c.updated_at`
        verdadeiro para sempre, e o produto voltaria em toda varredura.

        O `p.created_at` no COALESCE e para o caso degenerado das duas datas
        nulas: sem ele a expressao cairia em `now()`, que avanca a cada ciclo e
        faria o produto nunca alcancar o proprio carimbo.
        """
        stmt = text(
            """
            WITH candidatos AS (
                SELECT
                    p.id AS product_id,
                    p.restaurant_id AS restaurant_id,
                    COALESCE(
                        GREATEST(p.updated_at, c.updated_at),
                        p.created_at,
                        now()
                    ) AS source_updated_at
                FROM products p
                LEFT JOIN categories c ON c.id = p.category_id
                WHERE p.is_active IS TRUE
            )
            SELECT
                candidatos.product_id,
                candidatos.restaurant_id,
                candidatos.source_updated_at
            FROM candidatos
            LEFT JOIN ai_product_embeddings ape
                   ON ape.product_id = candidatos.product_id
                  AND ape.restaurant_id = candidatos.restaurant_id
            WHERE
                ape.product_id IS NULL
                OR ape.updated_at IS NULL
                OR ape.updated_at < candidatos.source_updated_at
            ORDER BY candidatos.source_updated_at
            LIMIT :limit
            """
        )
        rows = self.db.execute(stmt, {"limit": limit}).mappings()
        return [dict(row) for row in rows]

    def save_embedding(
        self,
        restaurant_id: uuid.UUID,
        product_id: uuid.UUID,
        content: str,
        content_hash: str,
        metadata: dict[str, Any],
        embedding: list[float],
        source_updated_at: datetime,
    ) -> None:
        """Grava o vetor. NAO commita — quem commita e quem chama (regra de camadas).

        `updated_at` recebe `source_updated_at`, e nao `NOW()`. A diferenca
        parece cosmetica e nao e: entre LER o produto e ESCREVER o vetor passam
        algumas centenas de milissegundos gerando o embedding, e o lojista pode
        salvar uma edicao nesse meio. Com `NOW()`, o carimbo sairia DEPOIS
        dessa edicao e a varredura passaria a considerar atual um vetor gerado
        do texto anterior — a edicao se perderia em silencio, para sempre, ate
        alguem mexer no produto de novo.

        Gravando a versao lida, `products.updated_at` continua maior que
        `ape.updated_at` e o proximo ciclo pega o produto de novo.
        """
        stmt = (
            text(
                """
                INSERT INTO ai_product_embeddings (
                    restaurant_id,
                    product_id,
                    content,
                    content_hash,
                    metadata,
                    embedding,
                    updated_at
                )
                VALUES (
                    :restaurant_id,
                    :product_id,
                    :content,
                    :content_hash,
                    :metadata,
                    CAST(:embedding AS vector),
                    :source_updated_at
                )
                ON CONFLICT (restaurant_id, product_id)
                DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """
            )
            .bindparams(bindparam("metadata", type_=JSONB))
        )
        self.db.execute(
            stmt,
            {
                "restaurant_id": restaurant_id,
                "product_id": product_id,
                "content": content,
                "content_hash": content_hash,
                "metadata": metadata,
                "embedding": self._format_vector(embedding),
                "source_updated_at": source_updated_at,
            },
        )

    def touch_embedding(
        self,
        restaurant_id: uuid.UUID,
        product_id: uuid.UUID,
        metadata: dict[str, Any],
        source_updated_at: datetime,
    ) -> None:
        """Carimba a linha como atual sem gerar embedding. NAO commita.

        Sem isto a varredura tem um laco eterno: o produto cujo `updated_at`
        avancou mas cujo CONTEUDO nao mudou — mexeram no preco, no
        `sort_order`, na imagem — continuaria com `ape.updated_at <
        p.updated_at` e voltaria em TODO ciclo.

        Nao queimaria OpenAI (o `content_hash` bate antes), mas queimaria uma
        ida ao banco por item por ciclo e, pior, deixaria a contagem de
        pendentes permanentemente acima de zero — que e justamente a metrica
        que avisa quando o worker parou de rodar.

        O `metadata` e reescrito junto porque `price` e `is_available` sairam
        do conteudo indexado (ver `build_product_content`): se nao fosse aqui,
        eles nunca mais seriam atualizados nesta tabela.
        """
        stmt = (
            text(
                """
                UPDATE ai_product_embeddings
                SET
                    metadata = :metadata,
                    updated_at = :source_updated_at
                WHERE
                    restaurant_id = :restaurant_id
                    AND product_id = :product_id
                """
            )
            .bindparams(bindparam("metadata", type_=JSONB))
        )
        self.db.execute(
            stmt,
            {
                "restaurant_id": restaurant_id,
                "product_id": product_id,
                "metadata": metadata,
                "source_updated_at": source_updated_at,
            },
        )

    def get_embedding_by_product(
        self,
        restaurant_id: uuid.UUID,
        product_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Return sync metadata for one product embedding."""
        stmt = select(
            AIProductEmbedding.content_hash,
            AIProductEmbedding.updated_at,
        ).where(
            AIProductEmbedding.restaurant_id == restaurant_id,
            AIProductEmbedding.product_id == product_id,
        )
        row = self.db.execute(stmt).mappings().one_or_none()
        return dict(row) if row else None

    @staticmethod
    def _format_vector(embedding: list[float]) -> str:
        return "[" + ",".join(str(value) for value in embedding) + "]"
