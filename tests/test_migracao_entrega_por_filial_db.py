"""O que a revisão 0030 promete ao banco, contra um Postgres de verdade.

Três garantias, e nenhuma delas é visível na suíte rápida — todas dependem
de o Postgres estar aplicando defaults e constraints de verdade:

1. **O deploy não muda operação.** Nenhuma filial ganha campanha de frete
   grátis, nenhuma nasce pausada. É o mesmo tipo de promessa da revisão 0029,
   e o mesmo modo de falha: um default errado não levanta exceção — ele dá a
   entrega de graça em nome de um lojista que não pediu.

2. **As faixas de prazo não aceitam duas respostas para a mesma distância.**
   O UNIQUE `(branch_id, max_distance_km)` é o que impede isso, e o Pydantic
   não substitui: quem escreve por SQL passa por fora dele.

3. **Faixa com máximo abaixo do mínimo não entra.** O CHECK existe porque um
   `delivery_time_max < delivery_time_min` gravado transforma o prazo do app
   numa faixa invertida ("40 a 15 min") sem erro em lugar nenhum.

O estado do schema aqui já é o de depois da revisão — a fixture `db` roda
`alembic upgrade head`.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _filial_criada_pelo_banco(db: Session, restaurante) -> str:
    """Filial inserida por SQL cru, sem passar pelo ORM.

    O model tem defaults do lado do PYTHON. Uma filial criada por ele sairia
    com os valores certos mesmo que a revisão não tivesse `server_default`
    nenhum, e o teste passaria verde sobre a migração errada.
    """
    return db.execute(
        text(
            "INSERT INTO branches "
            "(restaurant_id, name, slug, address, neighborhood, city, state) "
            "VALUES (:restaurant_id, 'Centro', :slug, 'Rua A, 100', "
            "'Centro', 'Fortaleza', 'CE') "
            "RETURNING id"
        ),
        {"restaurant_id": restaurante.id, "slug": f"centro-{restaurante.slug}"},
    ).scalar_one()


def _inserir_faixa(db: Session, branch_id, distancia, minimo, maximo) -> None:
    db.execute(
        text(
            "INSERT INTO branch_delivery_time_bands "
            "(branch_id, max_distance_km, delivery_time_min, delivery_time_max) "
            "VALUES (:branch_id, :distancia, :minimo, :maximo)"
        ),
        {
            "branch_id": branch_id,
            "distancia": distancia,
            "minimo": minimo,
            "maximo": maximo,
        },
    )
    db.flush()


class TestODeployNaoMudaOperacao:
    def test_filial_nao_nasce_com_campanha_de_frete_gratis(self, db: Session):
        """Os dois nulos são "ninguém configurou", que é a verdade sobre a
        plataforma inteira neste minuto.

        Um `free_delivery_enabled` com default `true` não quebraria nada de
        forma visível: ele daria entrega grátis em toda loja cujo lojista
        nunca abriu essa tela.
        """
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        linha = db.execute(
            text(
                "SELECT free_delivery_enabled, free_delivery_min_order_value "
                "FROM branches WHERE id = :id"
            ),
            {"id": filial_id},
        ).mappings().one()

        assert linha["free_delivery_enabled"] is None
        assert linha["free_delivery_min_order_value"] is None

    def test_filial_nao_nasce_com_a_entrega_pausada(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        pausada_ate = db.execute(
            text("SELECT delivery_paused_until FROM branches WHERE id = :id"),
            {"id": filial_id},
        ).scalar_one()

        assert pausada_ate is None

    def test_pedido_antigo_nao_teve_taxa_perdoada(self, db: Session):
        """`delivery_fee_waived` nasce `0` em todo pedido que já existe, e
        zero é a verdade sobre eles: nenhum passou por uma campanha que ainda
        não existia. Nulo permitiria que o primeiro relatório contasse
        "desconhecido" como se fosse dado."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)

        perdoado = db.execute(
            text("SELECT delivery_fee_waived FROM orders WHERE id = :id"),
            {"id": pedido.id},
        ).scalar_one()

        assert perdoado == 0


class TestAsFaixasDePrazo:
    def test_duas_faixas_com_o_mesmo_teto_sao_recusadas(self, db: Session):
        """Não é ambiguidade de apresentação: são duas respostas diferentes
        para a mesma distância, e a que vale mudaria entre duas consultas
        idênticas."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        _inserir_faixa(db, filial.id, 5, 15, 25)

        with pytest.raises(IntegrityError):
            _inserir_faixa(db, filial.id, 5, 30, 40)

    def test_o_mesmo_teto_em_outra_filial_e_valido(self, db: Session):
        # O UNIQUE é por filial: "até 5 km" da loja do Centro e "até 5 km" da
        # Aldeota são dois trajetos diferentes.
        restaurante = fab.criar_restaurante(db)
        centro = fab.criar_filial(db, restaurante, nome="Centro")
        aldeota = fab.criar_filial(db, restaurante, nome="Aldeota")

        _inserir_faixa(db, centro.id, 5, 15, 25)
        _inserir_faixa(db, aldeota.id, 5, 30, 40)

        total = db.execute(
            text("SELECT count(*) FROM branch_delivery_time_bands")
        ).scalar_one()
        assert total == 2

    def test_faixa_invertida_e_recusada(self, db: Session):
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)

        with pytest.raises(IntegrityError):
            _inserir_faixa(db, filial.id, 5, 40, 15)

    def test_faixa_com_distancia_zero_e_recusada(self, db: Session):
        # Um teto de zero não alcança endereço nenhum: é uma linha que existe
        # na tela do lojista e nunca vale para pedido nenhum.
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)

        with pytest.raises(IntegrityError):
            _inserir_faixa(db, filial.id, 0, 15, 25)
