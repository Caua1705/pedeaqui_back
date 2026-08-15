"""Mantem o indice do Rapi em dia, sem que ninguem precise lembrar.

O QUE ELE RESOLVE. Ate hoje os embeddings so eram gerados por
`scripts/reindex_ai.py`, rodado a mao. Na pratica ninguem rodava: produto novo
cadastrado no painel simplesmente nao existia para o Rapi, e descricao
reescrita continuava sendo encontrada — ou nao — pelo texto antigo. Sem erro,
sem log, sem sintoma do lado de quem cadastrou.

COMO ELE SABE O QUE MUDOU. Comparando `ai_product_embeddings.updated_at` com o
`GREATEST(products.updated_at, categories.updated_at)`. Nao ha fila, nem
gancho no painel, nem tabela de trabalho: `products.updated_at` e mantido pelo
TRIGGER `trg_products_updated_at`, dentro do banco. Isso da uma propriedade que
gancho na aplicacao nao teria — **nada consegue burlar**. Edicao por SQL
manual, script de importacao ou rota nova que ninguem instrumentou entram na
varredura do mesmo jeito. A consulta esta em `AIRepository.list_stale_products`,
com o porque de cada condicao.

POR QUE O LACO ESTA AQUI DENTRO, E NAO NO `command` DO COMPOSE. O container de
`limpeza` faz o contrario — laco em `sh`, um processo Python por rodada — e
esta certo la: ele roda uma vez por dia. Este roda a cada minuto, e um
interpretador novo por minuto significa recriar o pool de conexoes sessenta
vezes por hora para fazer, quase sempre, nada.
"""

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import Connection, select
from sqlalchemy.orm import Session

from src.ai.services.chat_cache import menu_generation
from src.ai.services.embedding_service import EmbeddingService
from src.ai.services.product_indexing import index_product
from src.core.config import settings
from src.db.session import SessionLocal, get_engine
from src.models.category_model import Category
from src.models.product_model import Product
from src.repositories.ai_repository import AIRepository


logger = logging.getLogger("reindex")

# Numero arbitrario e fixo. Advisory lock do Postgres nao tem namespace: o que
# impede colisao com outro uso e o proprio numero ser improvavel.
CHAVE_DO_LOCK = 8_314_921

_encerrar = False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _pedir_encerramento)
    signal.signal(signal.SIGINT, _pedir_encerramento)

    logger.info(
        "[AI reindex] worker iniciado | intervalo_s=%d | lote=%d",
        settings.AI_REINDEX_INTERVAL_SECONDS,
        settings.AI_REINDEX_BATCH_SIZE,
    )

    while not _encerrar:
        try:
            rodar_um_ciclo(settings.AI_REINDEX_BATCH_SIZE)
        except Exception:
            # O ciclo inteiro falhou (banco fora, por exemplo). Nao derruba o
            # processo: o proximo ciclo tenta de novo, e o `restart: always`
            # nao ajudaria em nada aqui alem de perder o backoff natural do
            # intervalo.
            logger.exception("[AI reindex] ciclo falhou")
        _dormir(settings.AI_REINDEX_INTERVAL_SECONDS)

    logger.info("[AI reindex] worker encerrado")


def rodar_um_ciclo(tamanho_do_lote: int) -> None:
    """Um lote de produtos atrasados, sob advisory lock."""
    with get_engine().connect() as conexao:
        if not _tomar_o_lock(conexao):
            logger.info("[AI reindex] outro processo esta varrendo; ciclo pulado")
            return
        try:
            _varrer(tamanho_do_lote)
        finally:
            # OBRIGATORIO, e nao educacao: advisory lock de sessao vive na
            # CONEXAO, e a conexao volta para o pool sem ser resetada. Sem este
            # unlock, o lock ficaria preso na conexao empoçada e o proximo
            # ciclo — deste mesmo processo — encontraria o banco travado por si
            # mesmo, para sempre.
            conexao.exec_driver_sql(f"SELECT pg_advisory_unlock({CHAVE_DO_LOCK})")


def _varrer(tamanho_do_lote: int) -> None:
    db = SessionLocal()
    try:
        pendentes = AIRepository(db).list_stale_products(limit=tamanho_do_lote)
        if not pendentes:
            return

        logger.info(
            "[AI reindex] pendentes_no_lote=%d | mais_antigo_s=%d",
            len(pendentes),
            _idade_em_segundos(pendentes[0]["source_updated_at"]),
        )
        contagem = {"created": 0, "updated": 0, "touched": 0, "sumiu": 0, "falhou": 0}
        embedding_service = EmbeddingService()
        for pendente in pendentes:
            resultado = _indexar_um(db, embedding_service, pendente)
            contagem[resultado] += 1

        logger.info(
            "[AI reindex] ciclo | criados=%d | atualizados=%d | carimbados=%d "
            "| sumiram=%d | falhas=%d",
            contagem["created"],
            contagem["updated"],
            contagem["touched"],
            contagem["sumiu"],
            contagem["falhou"],
        )
    finally:
        db.close()


class _ProdutoSumiu(Exception):
    pass


def _indexar_um(db: Session, embedding_service: EmbeddingService, pendente: dict) -> str:
    """Um produto, uma transacao. Falha de um nao leva o lote junto.

    E por isso que o commit esta aqui e nao no fim da varredura: um produto com
    descricao que a API de embedding recusa desfaria o trabalho dos outros
    quarenta e nove, e o lote inteiro voltaria pendente no ciclo seguinte —
    para falhar de novo no mesmo item.
    """
    product_id = pendente["product_id"]
    try:
        produto, category_name = _carregar_produto(db, product_id)
    except _ProdutoSumiu:
        # A linha nao existe mais. Nao deveria acontecer — nada e apagado no
        # cardapio, so desativado — mas custa uma linha e impede que um caso
        # que ninguem previu vire excecao no meio do lote.
        logger.info("[AI reindex] produto sumiu entre a consulta e a indexacao | product_id=%s", product_id)
        return "sumiu"

    try:
        resultado = index_product(
            repository=AIRepository(db),
            embedding_service=embedding_service,
            product=produto,
            category_name=category_name,
            source_updated_at=pendente["source_updated_at"],
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[AI reindex] falhou ao indexar | product_id=%s", product_id)
        return "falhou"

    # Depois do commit, nunca antes: invalidar o cache de busca de um
    # restaurante cuja escrita foi revertida deixaria o Rapi refazendo consulta
    # a toa por causa de uma indexacao que nao aconteceu.
    menu_generation.bump(produto.restaurant_id)
    return resultado


def _carregar_produto(db: Session, product_id) -> tuple[Product, str | None]:
    stmt = (
        select(Product, Category.name)
        .outerjoin(Category, Product.category_id == Category.id)
        .where(Product.id == product_id)
    )
    linha = db.execute(stmt).one_or_none()
    if linha is None:
        raise _ProdutoSumiu()
    return linha[0], linha[1]


def _tomar_o_lock(conexao: Connection) -> bool:
    """Garante um varredor por vez.

    Nao e paranoia de escala: durante um deploy os dois containers convivem por
    alguns segundos, e alguem pode rodar `reindex_ai.py` a mao no meio do
    expediente. Sem o lock, os dois pegam o mesmo lote e pagam o mesmo
    embedding duas vezes.
    """
    return bool(conexao.exec_driver_sql(f"SELECT pg_try_advisory_lock({CHAVE_DO_LOCK})").scalar())


def _idade_em_segundos(momento: datetime) -> int:
    return int((datetime.now(timezone.utc) - momento).total_seconds())


def _dormir(segundos: int) -> None:
    """Em fatias de um segundo para o SIGTERM nao esperar o intervalo inteiro.

    `time.sleep` nao e interrompido por sinal desde a PEP 475: o handler roda e
    o sleep continua de onde parou. Um `docker compose down` ficaria esperando
    o timeout de dez segundos a cada deploy.
    """
    for _ in range(segundos):
        if _encerrar:
            return
        time.sleep(1)


def _pedir_encerramento(_sinal: int, _quadro: FrameType | None) -> None:
    global _encerrar
    _encerrar = True


if __name__ == "__main__":
    main()
