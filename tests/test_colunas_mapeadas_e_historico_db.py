"""As seis colunas que o ORM não enxergava, e o `payment_status` no histórico.

## As seis

`Modelo.created_at` era `AttributeError` em tabelas que **têm** `created_at` —
a terceira classe de `scripts/divergencias_orm_schema.py`, que agora está em
zero. Duas coisas que só aparecem contra o banco de verdade:

- **`product_options.updated_at` e `product_option_groups.updated_at` nunca se
  moviam.** Não há trigger nessas tabelas (ao contrário de `orders`), e a
  coluna não era mapeada — então toda linha tinha `updated_at == created_at`
  para sempre. Quem move agora é o `onupdate` do model;
- **`ai_product_embeddings.created_at` é NULÁVEL**, sozinha entre as seis. O
  `updated_at` ao lado dela **não** ganhou `onupdate` de propósito: ele não é
  "a hora em que indexou", é a versão do produto que indexou.

## O `payment_status` no histórico

`orders.status = 'pending'` é o mesmo valor para "pago, esperando a cozinha" e
para "a cobrança foi recusada". As duas ficavam na mesma linha da tela do
cliente, e a segunda ele resolve sozinho — se souber.
"""


import pytest

from src.models.order_item_option_model import OrderItemOption
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.services.customer_service import CustomerService
from tests.fabricas_db import (
    criar_categoria,
    criar_cliente,
    criar_grupo_de_opcoes,
    criar_opcao,
    criar_pedido,
    criar_produto,
    criar_restaurante,
    filial_padrao,
)


pytestmark = pytest.mark.db


class TestAsSeisColunasAgoraExistemNoORM:
    def test_o_grupo_e_a_opcao_carimbam_criacao_e_atualizacao(self, db) -> None:
        restaurante = criar_restaurante(db)
        categoria = criar_categoria(db, restaurante)
        produto = criar_produto(db, restaurante, categoria)
        grupo = criar_grupo_de_opcoes(db, produto, nome="Tamanho")
        opcao = criar_opcao(db, grupo, nome="Grande")
        db.commit()

        for linha in (grupo, opcao):
            assert linha.created_at is not None
            assert linha.updated_at is not None
            assert linha.created_at.tzinfo is not None

    def test_o_updated_at_das_duas_tem_onupdate_configurado(self, db) -> None:
        """Quem move o `updated_at` aqui é o model, e não o banco.

        **Não há trigger nestas tabelas** — conferido no `information_schema`,
        ao contrário de `orders`, que tem `trg_orders_updated_at`. Enquanto a
        coluna não era mapeada, ninguém a escrevia: toda linha ficava com
        `updated_at == created_at` para sempre.

        **Por que a asserção é sobre a configuração e não sobre o relógio.**
        `func.now()` no Postgres é `transaction_timestamp()` — o instante em que
        a TRANSAÇÃO começou, não o do comando. O fixture `db` roda o teste
        inteiro numa transação só, então o INSERT e o UPDATE recebem o mesmo
        valor e um `assert depois > antes` falha mesmo com tudo certo. Medido:
        os dois vieram idênticos ao microssegundo.

        Isso vale além do teste: **duas escritas na mesma requisição produzem o
        mesmo `updated_at`**, e é o comportamento certo — a requisição é a
        unidade de mudança."""
        for modelo in (ProductOption, ProductOptionGroup):
            coluna = modelo.__table__.c.updated_at
            assert coluna.onupdate is not None, modelo.__name__
            assert coluna.server_default is not None, modelo.__name__

    def test_o_item_de_pedido_carimba_criacao_e_NAO_tem_updated_at(self, db) -> None:
        """A linha é um snapshot do que foi pedido: ela não muda depois de
        gravada, e a tabela não tem a coluna."""
        assert hasattr(OrderItemOption, "created_at")
        assert not hasattr(OrderItemOption, "updated_at")

    def test_o_embedding_carimba_criacao_e_o_updated_at_NAO_e_relogio(
        self, db
    ) -> None:
        """`ai_product_embeddings.updated_at` é a VERSÃO DO PRODUTO que
        indexou, não a hora em que indexou. Um `onupdate` ali faria uma edição
        salva durante a geração do vetor se perder em silêncio."""
        from src.models.ai_product_embedding_model import AIProductEmbedding

        assert hasattr(AIProductEmbedding, "created_at")
        coluna = AIProductEmbedding.__table__.c.updated_at
        assert coluna.onupdate is None

    def test_o_varredor_nao_acha_mais_coluna_sem_mapeamento(self, db) -> None:
        """A trava da terceira classe. Coluna nova no banco que o ORM não
        enxergue volta a aparecer aqui — e `Modelo.coluna` é `AttributeError`,
        que só estoura em quem for usá-la."""
        from sqlalchemy import inspect

        from scripts.divergencias_orm_schema import comparar

        achados = comparar(inspect(db.get_bind()))

        assert achados.nao_mapeadas == []


class TestOHistoricoDizPorQueOPedidoEstaParado:
    def _historico(self, db, cliente):
        return CustomerService(db).list_orders(cliente)

    def test_a_cobranca_recusada_aparece_como_failed(self, db) -> None:
        """É o caso que o cliente resolve SOZINHO — e sem este campo ele não
        sabe que pode. `status` diz `pending` nos dois."""
        restaurante = criar_restaurante(db)
        filial = filial_padrao(db, restaurante)
        cliente = criar_cliente(db)
        criar_pedido(
            db,
            restaurante,
            filial,
            cliente=cliente,
            status="pending",
            payment_status="failed",
        )
        db.commit()

        item = self._historico(db, cliente)[0]

        assert item.status == "pending"
        assert item.payment_status == "failed"

    def test_o_pago_esperando_a_cozinha_tem_o_MESMO_status(self, db) -> None:
        """O par do teste acima, e é ele que mostra por que o campo era
        necessário: os dois são `pending`, e só o `payment_status` os separa."""
        restaurante = criar_restaurante(db)
        filial = filial_padrao(db, restaurante)
        cliente = criar_cliente(db)
        criar_pedido(
            db,
            restaurante,
            filial,
            cliente=cliente,
            status="pending",
            payment_status="paid",
        )
        db.commit()

        item = self._historico(db, cliente)[0]

        assert item.status == "pending"
        assert item.payment_status == "paid"
