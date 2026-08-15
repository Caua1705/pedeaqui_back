"""A varredura que mantém o índice do Rapi em dia, contra o Postgres.

O que estes testes protegem é a propriedade central do desenho: **um produto
sai da lista de pendentes se, e somente se, o índice já reflete a versão atual
dele.** Errar para um lado deixa o Rapi falando de um cardápio velho em
silêncio; errar para o outro põe a varredura num laço eterno, reindexando o
mesmo produto para sempre.

Sobre as datas escritas à mão nos testes: dentro de UMA transação o `now()` do
Postgres não anda — é o instante em que a transação abriu. Como a fixture `db`
roda o teste inteiro numa transação só, o TRIGGER `trg_products_updated_at`
carimbaria todos os produtos com o mesmo horário e não haveria como encenar
"editado depois de indexado". Por isso o tempo entra pelo `source_updated_at`
que a indexação recebe, que é justamente o parâmetro sob teste.
"""

import importlib.util
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from src.ai.services.product_indexing import index_product
from src.models.ai_product_embedding_model import AIProductEmbedding
from src.models.category_model import Category
from src.models.product_model import Product
from src.repositories.ai_repository import AIRepository
from tests.fabricas_db import criar_categoria, criar_produto, criar_restaurante


pytestmark = pytest.mark.db

DIMENSOES_DO_VETOR = 1536
ROOT_DIR = Path(__file__).resolve().parents[1]


def carregar_worker():
    """Carrega o script por caminho: `scripts/` não é um pacote importável.

    Mesmo caminho de `test_audit_indexes.py`.
    """
    path = ROOT_DIR / "scripts" / "reindex_worker.py"
    spec = importlib.util.spec_from_file_location("reindex_worker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = carregar_worker()


class EmbeddingServiceFalso:
    """Conta as chamadas. É como os testes provam que o carimbo não paga OpenAI."""

    def __init__(self) -> None:
        self.chamadas = 0

    def generate_embedding(self, text: str) -> list[float]:
        self.chamadas += 1
        return [0.1] * DIMENSOES_DO_VETOR


def indexar(db, produto, category_name, source_updated_at, embedding_service=None):
    servico = embedding_service or EmbeddingServiceFalso()
    resultado = index_product(
        repository=AIRepository(db),
        embedding_service=servico,
        product=produto,
        category_name=category_name,
        source_updated_at=source_updated_at,
    )
    db.flush()
    return resultado


def ids_pendentes(db, limite: int = 50) -> set[uuid.UUID]:
    return {linha["product_id"] for linha in AIRepository(db).list_stale_products(limit=limite)}


def pendente_de(db, produto) -> dict:
    linhas = AIRepository(db).list_stale_products(limit=50)
    return next(linha for linha in linhas if linha["product_id"] == produto.id)


def carimbo_gravado(db, produto):
    stmt = select(AIProductEmbedding.updated_at).where(
        AIProductEmbedding.product_id == produto.id
    )
    return db.execute(stmt).scalar_one()


class TestQuemEntraNaListaDePendentes:
    def test_produto_novo_nunca_indexado_esta_pendente(self, db):
        """O pior efeito do reindex manual: o produto entrava no cardápio e o
        Rapi simplesmente não o conhecia, sem erro em lugar nenhum."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        assert produto.id in ids_pendentes(db)

    def test_produto_recem_indexado_sai_da_lista(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        indexar(db, produto, "Carnes", produto.updated_at)

        assert produto.id not in ids_pendentes(db)

    def test_produto_indexado_numa_versao_anterior_volta_para_a_lista(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        indexar(db, produto, "Carnes", produto.updated_at - timedelta(minutes=5))

        assert produto.id in ids_pendentes(db)

    def test_produto_desativado_fica_de_fora(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Antigo", is_active=False)

        assert produto.id not in ids_pendentes(db)


class TestCategoriaRenomeada:
    """`update_category` não toca em `products.updated_at`.

    Sem a terceira condição da consulta, renomear uma categoria deixaria o
    `category_name` — que vai no conteúdo indexado E no metadata, e é o que o
    modelo lê — congelado para sempre no nome antigo.
    """

    def test_categoria_mais_nova_que_o_indice_deixa_o_produto_pendente(self, db):
        restaurante = criar_restaurante(db)
        categoria = self._categoria_renomeada_no_futuro(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        indexar(db, produto, "Carnes", produto.updated_at)

        assert produto.id in ids_pendentes(db)

    def test_o_carimbo_a_gravar_e_o_da_categoria_quando_ela_e_mais_nova(self, db):
        """O GREATEST das duas datas, e não a do produto.

        Gravar só `products.updated_at` depois de uma renomeação deixaria
        `ape.updated_at < categories.updated_at` verdadeiro para sempre, e o
        produto voltaria em toda varredura — a armadilha do laço eterno, pela
        porta da categoria.
        """
        restaurante = criar_restaurante(db)
        categoria = self._categoria_renomeada_no_futuro(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        pendente = pendente_de(db, produto)
        indexar(db, produto, categoria.name, pendente["source_updated_at"])

        assert pendente["source_updated_at"] == categoria.updated_at
        assert produto.id not in ids_pendentes(db)

    @staticmethod
    def _categoria_renomeada_no_futuro(db, restaurante) -> Category:
        """Categoria cujo `updated_at` é posterior ao dos produtos dela.

        Construída à mão porque o valor entra no INSERT: o trigger de
        `categories` é BEFORE UPDATE, então um UPDATE aqui devolveria o
        `now()` da transação e apagaria a diferença que o teste precisa.
        """
        categoria = criar_categoria(db, restaurante, nome="Carnes")
        renomeada = Category(
            restaurant_id=restaurante.id,
            name="Carnes Nobres",
            slug=f"{categoria.slug}-nova",
            is_active=True,
            updated_at=categoria.updated_at + timedelta(hours=1),
        )
        db.add(renomeada)
        db.flush()
        return renomeada


class TestArmadilha1OCarimboSemEmbedding:
    """Conteúdo igual, `updated_at` diferente: carimbar sem pagar embedding.

    Sem o carimbo, o produto cujo texto indexado não mudou — mexeram no preço,
    no `sort_order`, na imagem — continuaria com `ape.updated_at <
    p.updated_at` e voltaria em TODO ciclo, para sempre.
    """

    def test_reindexar_com_o_mesmo_conteudo_nao_chama_a_openai(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")
        servico = EmbeddingServiceFalso()

        indexar(db, produto, "Carnes", produto.updated_at - timedelta(minutes=5), servico)
        resultado = indexar(db, produto, "Carnes", produto.updated_at, servico)

        assert resultado == "touched"
        assert servico.chamadas == 1

    def test_o_carimbo_tira_o_produto_da_lista(self, db):
        """A parte que fecha o laço: carimbado, ele para de voltar."""
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        indexar(db, produto, "Carnes", produto.updated_at - timedelta(minutes=5))
        assert produto.id in ids_pendentes(db)

        indexar(db, produto, "Carnes", produto.updated_at)
        assert produto.id not in ids_pendentes(db)

    def test_o_carimbo_atualiza_o_metadata(self, db):
        """`price` e `is_available` saíram do conteúdo indexado.

        Se o carimbo não reescrevesse o metadata, os dois congelariam nesta
        tabela no valor do dia em que o TEXTO mudou pela última vez.
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")

        indexar(db, produto, "Carnes", produto.updated_at - timedelta(minutes=5))
        produto.price = produto.price + 10
        indexar(db, produto, "Carnes", produto.updated_at)

        stmt = select(AIProductEmbedding.embedding_metadata).where(
            AIProductEmbedding.product_id == produto.id
        )
        assert db.execute(stmt).scalar_one()["price"] == format(produto.price, "f")


class TestArmadilha2AEdicaoQueSePerdiaEmSilencio:
    """O carimbo é a VERSÃO indexada, nunca a hora da indexação.

    Entre ler o produto e gravar o vetor passam algumas centenas de
    milissegundos gerando o embedding, e o lojista pode salvar uma edição nesse
    meio. Com `NOW()`, o carimbo sairia DEPOIS dessa edição e a varredura
    passaria a considerar atual um vetor gerado do texto anterior — a edição se
    perderia para sempre, sem erro, até alguém mexer no produto de novo.
    """

    def test_o_carimbo_gravado_e_a_versao_lida(self, db):
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")
        versao_lida = produto.updated_at - timedelta(minutes=5)

        indexar(db, produto, "Carnes", versao_lida)

        assert carimbo_gravado(db, produto) == versao_lida

    def test_a_edicao_feita_durante_a_geracao_do_vetor_continua_pendente(self, db):
        """O teste que distingue o certo do errado.

        `versao_lida` é o que a varredura enxergou antes de gerar o embedding;
        `produto.updated_at` é a edição que entrou no meio. Gravando a versão
        lida, o produto continua pendente e o próximo ciclo o pega. Gravando
        `NOW()` — que aqui seria maior que as duas — ele sairia da lista e a
        edição estaria perdida.
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")
        versao_lida = produto.updated_at - timedelta(minutes=5)

        indexar(db, produto, "Carnes", versao_lida)

        assert carimbo_gravado(db, produto) < produto.updated_at
        assert produto.id in ids_pendentes(db)

    def test_o_carimbo_vale_tambem_na_primeira_indexacao(self, db):
        """O INSERT e o UPDATE do upsert precisam concordar.

        A coluna tem `DEFAULT now()`: se o INSERT deixasse o default agir, o
        produto novo nasceria carimbado com a hora da indexação e a mesma
        edição perdida aconteceria — só que na estreia do produto.
        """
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria, nome="Picanha")
        versao_lida = produto.updated_at - timedelta(minutes=5)

        resultado = indexar(db, produto, "Carnes", versao_lida)

        assert resultado == "created"
        assert carimbo_gravado(db, produto) == versao_lida


class TestOAdvisoryLock:
    """Um varredor por vez — e, principalmente, o unlock explícito.

    Durante um deploy os dois containers convivem por alguns segundos, e
    alguém pode rodar `reindex_ai.py` à mão no meio do expediente. Sem o lock,
    os dois pegam o mesmo lote e pagam o mesmo embedding duas vezes.
    """

    def test_o_segundo_processo_nao_toma_o_lock_do_primeiro(self, engine_de_teste):
        with engine_de_teste.connect() as primeira, engine_de_teste.connect() as segunda:
            try:
                assert worker._tomar_o_lock(primeira) is True
                assert worker._tomar_o_lock(segunda) is False
            finally:
                self._destravar(primeira)

    def test_sem_o_unlock_o_worker_travaria_a_si_mesmo(self, engine_de_teste):
        """O motivo de o `pg_advisory_unlock` estar num `finally`.

        Advisory lock de sessão vive na CONEXÃO, e a conexão volta para o pool
        sem ser resetada. Sem o unlock, o lock ficaria preso na conexão
        empoçada e o ciclo seguinte — do mesmo processo — encontraria o banco
        travado por ele mesmo, para sempre.
        """
        with engine_de_teste.connect() as conexao:
            assert worker._tomar_o_lock(conexao) is True
            self._destravar(conexao)

        with engine_de_teste.connect() as outro_ciclo:
            try:
                assert worker._tomar_o_lock(outro_ciclo) is True
            finally:
                self._destravar(outro_ciclo)

    @staticmethod
    def _destravar(conexao) -> None:
        conexao.exec_driver_sql(f"SELECT pg_advisory_unlock({worker.CHAVE_DO_LOCK})")


class TestAOrdemDaLista:
    def test_quem_mudou_ha_mais_tempo_vem_primeiro(self, db):
        """É o que faz o lote ser justo.

        Com mais produtos pendentes do que cabe num lote — uma importação de
        cardápio inteiro, por exemplo — a ordem decide quem espera. Sem ela,
        nada impede que o mesmo produto fique para trás ciclo após ciclo.
        """
        restaurante = criar_restaurante(db)
        recente = criar_produto(db, restaurante, criar_categoria(db, restaurante), nome="Recente")
        antigo = self._produto_no_passado(db, restaurante, recente.updated_at - timedelta(hours=2))

        pendentes = AIRepository(db).list_stale_products(limit=50)

        assert [linha["product_id"] for linha in pendentes] == [antigo.id, recente.id]

    @staticmethod
    def _produto_no_passado(db, restaurante, momento) -> Product:
        """Produto E categoria carimbados no passado.

        A categoria também, e não é detalhe do teste: a lista ordena pelo
        `GREATEST` das duas datas, então um produto velho numa categoria
        recém-mexida é — corretamente — tão pendente quanto um produto novo.
        Deixar a categoria no `now()` da transação empataria os dois.

        Os dois são construídos à mão porque os triggers de `products` e
        `categories` são BEFORE UPDATE: o valor só entra pelo INSERT.
        """
        categoria = Category(
            restaurant_id=restaurante.id,
            name="Categoria Antiga",
            slug=f"categoria-antiga-{uuid.uuid4().hex[:12]}",
            is_active=True,
            updated_at=momento,
        )
        db.add(categoria)
        db.flush()

        produto = Product(
            restaurant_id=restaurante.id,
            category_id=categoria.id,
            name="Antigo",
            slug=f"antigo-{uuid.uuid4().hex[:12]}",
            price=Decimal("10.00"),
            is_active=True,
            is_available=True,
            updated_at=momento,
        )
        db.add(produto)
        db.flush()
        return produto
