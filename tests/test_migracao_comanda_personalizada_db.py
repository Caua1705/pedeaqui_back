"""Os defaults e o CHECK da revisão 0029, contra um Postgres de verdade.

**Por que esta revisão merece teste próprio, se ela não tem cópia de dados.**
A 0025 ganhou o dela pelo `UPDATE` que falha em silêncio. Aqui o risco é o
contrário e é igualmente mudo: o que a revisão promete é que **o deploy não
muda nada** — toda filial que já existe continua imprimindo uma via do
cliente e uma de cada setor, em entrega e em retirada.

Se um dos quatro `server_default` estivesse errado (ou ausente, deixando a
coluna nascer nula numa tabela `NOT NULL`), o `alembic upgrade head` não
falharia sobre uma tabela vazia — falharia sobre a do Júnior, no deploy, ou
pior: passaria, e a cozinha simplesmente pararia de receber comanda de
retirada sem nada no log. É o mesmo tipo de defeito da 0025, virado do avesso.

O `CHECK` está aqui pelo motivo oposto: ele é a única parte da regra que o
Pydantic **não** cobre. `MAX_PRINT_COPIES` responde 422 para quem passa pela
rota; quem escreve por fora (script, correção à mão no Supabase) só encontra
o teto se ele estiver no banco.

O estado do schema aqui já é o de depois da revisão — a fixture `db` roda
`alembic upgrade head`.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


# As quatro colunas com o valor que o deploy tem que dar a toda filial que já
# existe: o comportamento anterior à revisão, sem ninguém ter configurado nada.
COPIAS_ESPERADAS = {
    "print_customer_copies_delivery": 1,
    "print_production_copies_delivery": 1,
    "print_customer_copies_pickup": 1,
    "print_production_copies_pickup": 1,
}


def _filial_criada_pelo_banco(db: Session, restaurante) -> str:
    """Uma filial inserida por SQL cru, sem passar pelo ORM.

    A fábrica `fab.criar_filial` NÃO serve aqui, e a diferença é justamente o
    que este arquivo testa: o model tem `default=1` do lado do PYTHON, então
    uma filial criada por ele sairia com 1 nas quatro colunas mesmo que o
    `server_default` da revisão não existisse. O teste passaria verde sobre a
    migração errada — que é o pior resultado possível para um teste de
    migração.

    Inserindo só as colunas obrigatórias, quem preenche o resto é o banco, que
    é quem preenche em produção.
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


def _copias_da_filial(db: Session, branch_id) -> dict:
    colunas = ", ".join(COPIAS_ESPERADAS)
    linha = db.execute(
        text(f"SELECT {colunas} FROM branches WHERE id = :id"),
        {"id": branch_id},
    ).mappings().one()
    return dict(linha)


class TestOsDefaultsPreservamAOperacao:
    def test_filial_criada_sem_configurar_nada_imprime_uma_via_de_cada(self, db: Session):
        """O que o deploy faz com quem já estava lá.

        O INSERT não menciona nenhuma das quatro colunas, então quem as
        preenche é o banco — que é exatamente a situação de toda filial em
        produção no minuto seguinte ao `alembic upgrade head`.
        """
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        assert _copias_da_filial(db, filial_id) == COPIAS_ESPERADAS

    def test_a_retirada_nao_nasce_com_a_producao_desligada(self, db: Session):
        """A tentação que esta revisão recusa, isolada num teste.

        "Retirada não precisa da via da sacola" é o caso que motivou a
        feature, e faria `print_production_copies_pickup = 0` parecer o
        default certo. Mas a via de PRODUÇÃO da retirada é a comanda da
        COZINHA, e o pedido de retirada também é preparado: nascer em zero
        pararia a produção de todo pedido de balcão no deploy, sem uma linha
        de log. Quem decide desligar a via da sacola é o lojista, na tela.
        """
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        copias = _copias_da_filial(db, filial_id)

        assert copias["print_production_copies_pickup"] == 1

    def test_o_rodape_nasce_nulo_nos_dois_lados(self, db: Session):
        """Nulo na filial é "herda"; nulo no restaurante é "não há mensagem".

        Os dois juntos são "nenhuma comanda muda" — que é o estado em que o
        deploy tem que deixar a plataforma inteira.
        """
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        rodape_da_filial = db.execute(
            text("SELECT receipt_footer_message FROM branches WHERE id = :id"),
            {"id": filial_id},
        ).scalar_one()

        assert rodape_da_filial is None


class TestOTetoDeCopiasMoraNoBanco:
    def test_acima_do_teto_o_banco_recusa(self, db: Session):
        """O 422 do Pydantic protege quem passa pela rota. Este CHECK protege
        contra o script e contra a correção à mão — e é o que impede que um
        `50` digitado no lugar de `5` gaste a bobina inteira num pedido e pare
        a impressão dos seguintes."""
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        with pytest.raises(IntegrityError):
            db.execute(
                text(
                    "UPDATE branches SET print_customer_copies_delivery = 50 "
                    "WHERE id = :id"
                ),
                {"id": filial_id},
            )
            db.flush()

    def test_zero_passa_porque_zero_e_uma_escolha(self, db: Session):
        """Zero não é ausência de valor: é "esta via não sai neste tipo de
        pedido", que é o pedido original da feature."""
        restaurante = fab.criar_restaurante(db)
        filial_id = _filial_criada_pelo_banco(db, restaurante)

        db.execute(
            text("UPDATE branches SET print_customer_copies_pickup = 0 WHERE id = :id"),
            {"id": filial_id},
        )
        db.flush()

        assert _copias_da_filial(db, filial_id)["print_customer_copies_pickup"] == 0
