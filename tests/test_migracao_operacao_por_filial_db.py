"""A cópia de dados da revisão 0025, contra um Postgres de verdade.

**Por que ela merece teste próprio.** O resto da revisão é `ADD COLUMN` e
`DROP COLUMN`: erra alto, e o `docker-entrypoint.sh` transforma o erro em loop
de restart antes de o Uvicorn subir (armadilha 5). O `UPDATE` não. Ele não
levanta exceção nenhuma quando está errado — ele só **reabre lojas que
estavam fechadas**, e o sintoma é pedido entrando numa loja sem ninguém para
produzi-lo, sem uma linha de log dizendo por quê.

As três colunas nascem `NOT NULL DEFAULT true`. Se o `UPDATE` não rodar, ou
rodar com o `WHERE` errado, o resultado não é um banco quebrado: é um banco
em que **todo mundo está aberto**, que é indistinguível de um banco correto
onde ninguém tinha fechado.

**O estado do schema aqui já é o de depois da revisão** — a fixture `db` roda
`alembic upgrade head`, então `restaurant_settings.is_open` não existe mais.
Cada teste recria a coluna antiga, popula, roda o SQL e a apaga. DDL no
Postgres é transacional e a fixture reverte tudo no fim, então isso não
vaza para o teste seguinte.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _carregar_revisao():
    """O nome do arquivo começa com dígito, então `import` não serve."""
    caminho = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "20260818_0025_operacao_por_filial.py"
    )
    spec = importlib.util.spec_from_file_location("revisao_0025", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


REVISAO = _carregar_revisao()


def _restaura_as_colunas_antigas(db: Session) -> None:
    """Devolve a `restaurant_settings` o schema de antes da revisão."""
    for coluna in REVISAO.COLUNAS_DE_ESTADO:
        db.execute(text(
            f"ALTER TABLE restaurant_settings ADD COLUMN {coluna} boolean DEFAULT true"
        ))


def _apaga_as_colunas_antigas(db: Session) -> None:
    for coluna in REVISAO.COLUNAS_DE_ESTADO:
        db.execute(text(f"ALTER TABLE restaurant_settings DROP COLUMN {coluna}"))


def _rodar_a_copia(db: Session, restaurante, **valores_do_restaurante) -> None:
    """O caminho inteiro: schema antigo, valor antigo, cópia, schema novo."""
    _restaura_as_colunas_antigas(db)
    if valores_do_restaurante:
        atribuicoes = ", ".join(f"{campo} = :{campo}" for campo in valores_do_restaurante)
        db.execute(
            text(
                f"UPDATE restaurant_settings SET {atribuicoes}"
                " WHERE restaurant_id = :restaurant_id"
            ),
            {**valores_do_restaurante, "restaurant_id": restaurante.id},
        )
    db.execute(text(REVISAO.SQL_COPIA_O_ESTADO_DO_DIA))
    _apaga_as_colunas_antigas(db)
    db.expire_all()


def test_o_restaurante_fechado_nao_reabre_no_deploy(db: Session):
    """O caso que justifica o UPDATE existir.

    Sem ele, o `DEFAULT true` das colunas novas abriria a loja sozinho no
    `alembic upgrade head` — e o lojista descobriria pelos pedidos entrando.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    fab.criar_configuracoes(db, restaurante)

    _rodar_a_copia(db, restaurante, is_open=False)

    assert filial.is_open is False


def test_as_duas_filiais_do_junior_herdam_o_mesmo_estado(db: Session):
    """O Júnior tem duas lojas e um `is_open` só, hoje.

    A revisão dá a cada uma a própria cópia daquele valor. Depois do deploy
    elas divergem quando alguém quiser; antes dele, não havia o que
    preservar, porque não havia dois valores.
    """
    restaurante = fab.criar_restaurante(db, "Junior da Picanha")
    matriz = fab.criar_filial(db, restaurante, "Matriz")
    aldeota = fab.criar_filial(db, restaurante, "Aldeota")
    fab.criar_configuracoes(db, restaurante)

    _rodar_a_copia(db, restaurante, is_open=False, accepts_delivery=False)

    assert (matriz.is_open, aldeota.is_open) == (False, False)
    assert (matriz.accepts_delivery, aldeota.accepts_delivery) == (False, False)
    # `accepts_pickup` ficou nulo no restaurante, e nulo sempre significou
    # "aceita" na leitura antiga (`if settings.accepts_pickup is False`).
    assert (matriz.accepts_pickup, aldeota.accepts_pickup) == (True, True)


def test_restaurante_sem_linha_de_configuracao_fica_aberto(db: Session):
    """O LEFT JOIN não casa e o `DEFAULT true` vale — que é o certo.

    A linha de `restaurant_settings` sempre foi opcional, e restaurante sem
    ela aceitava pedido normalmente. Fechá-lo aqui tiraria do ar quem nunca
    passou pelo painel.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)

    _rodar_a_copia(db, restaurante)

    assert filial.is_open is True
    assert filial.accepts_delivery is True
    assert filial.accepts_pickup is True


def test_a_copia_nao_atravessa_para_outro_restaurante(db: Session):
    """É o `WHERE` do UPDATE, e ele é a única coisa que separa os dois.

    Sem essa condição o UPDATE seria um produto cartesiano: a filial de todo
    mundo receberia o `is_open` de um restaurante qualquer, e a plataforma
    inteira abriria ou fecharia junto.
    """
    fechado = fab.criar_restaurante(db, "Fechado")
    filial_fechada = fab.criar_filial(db, fechado)
    fab.criar_configuracoes(db, fechado)

    aberto = fab.criar_restaurante(db, "Aberto")
    filial_aberta = fab.criar_filial(db, aberto)
    fab.criar_configuracoes(db, aberto)

    _rodar_a_copia(db, fechado, is_open=False)

    assert filial_fechada.is_open is False
    assert filial_aberta.is_open is True


def test_os_seis_campos_comerciais_nascem_nulos(db: Session):
    """E nulo é a resposta certa: significa "herda".

    Copiar o valor do restaurante para cada filial deixaria uma cópia
    congelada em cada uma, e a próxima edição do padrão não chegaria a
    nenhuma — o lojista mudaria a taxa de serviço no painel e nada
    aconteceria.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    fab.criar_configuracoes(db, restaurante)

    _rodar_a_copia(db, restaurante)

    assert filial.min_order_value is None
    assert filial.service_fee_enabled is None
    assert filial.service_fee_amount is None
    assert filial.estimated_delivery_time_min is None
    assert filial.estimated_delivery_time_max is None
    assert filial.default_delivery_fee is None
