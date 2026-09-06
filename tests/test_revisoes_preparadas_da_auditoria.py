"""As preparadas `20260906_0061` e `20260906_0062`, e as três coisas da armadilha 53.

As duas saem da auditoria de 05/09 (§2.1 `customers.cpf`, §2.2
`coupon_claims.claimed_at`) e são o mesmo tipo de mudança: uma coluna que
ninguém lê, derrubada por revisão escrita e **não aplicada**.

O que se cobra, em cada uma:

1. **o Alembic NÃO as conhece** — a pergunta é feita ao `ScriptDirectory`, que
   é quem o `upgrade` consulta, e não ao caminho do arquivo;
2. **elas ainda descrevem o schema real** — a coluna existe hoje, com o tipo e
   a nulidade que a revisão espera, e o índice do CPF está de pé;
3. **elas RODAM**, contra o Postgres 17 de teste, numa transação que volta.
   Sem isso, "pronta" quer dizer "nunca executada", e o primeiro lugar onde ela
   roda de verdade acaba sendo produção, de madrugada, com a API fora do ar.

**E há um quarto, que é o que cada cabeçalho promete**: a `0061` só é segura
porque a coluna está VAZIA (a revisão `20260812_0019` anulou os valores), e a
`0062` só é segura porque as duas colunas gravam o mesmo instante. As duas
premissas são verificáveis, e aqui elas são verificadas — com linha na mesa,
porque um `DROP COLUMN` sobre tabela vazia não exercita premissa nenhuma.
"""

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from src.models.coupon_claim_model import CouponClaim
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from tests import fabricas_db as fab


RAIZ = Path(__file__).resolve().parent.parent
PREPARADAS = RAIZ / "alembic" / "preparadas"

CPF = PREPARADAS / "20260906_0061_o_cpf_sai_do_schema.py"
CLAIMED_AT = PREPARADAS / "20260906_0062_coupon_claims_perde_o_claimed_at.py"

INDICE_DO_CPF = "idx_customers_cpf_unique"


def _carregar(caminho: Path):
    especificacao = importlib.util.spec_from_file_location(caminho.stem, caminho)
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


def _colunas(conexao, tabela: str) -> set[str]:
    return {coluna["name"] for coluna in inspect(conexao).get_columns(tabela)}


def _existe_indice(conexao, nome: str) -> bool:
    return conexao.execute(
        text("SELECT count(*) FROM pg_class WHERE relkind = 'i' AND relname = :n"),
        {"n": nome},
    ).scalar_one() > 0


@pytest.mark.parametrize("caminho", [CPF, CLAIMED_AT], ids=lambda p: p.stem)
def test_o_alembic_nao_conhece_estas_revisoes(caminho):
    """A guarda do `git mv` distraído, feita a quem o `upgrade` pergunta."""
    revisao = _carregar(caminho)
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    conhecidas = {conhecida.revision for conhecida in script.walk_revisions()}

    assert revisao.revision not in conhecidas, (
        f"{caminho.name} declara revision={revisao.revision!r} e o Alembic a "
        "conhece: ela ESTÁ na cadeia e o próximo `alembic upgrade head` a "
        "aplica, inclusive no container de produção."
    )


@pytest.mark.db
def test_as_duas_colunas_existem_hoje(engine_de_teste):
    """A guarda do envelhecimento: o que elas derrubam está lá.

    Uma revisão futura que já tivesse derrubado uma delas deixaria a preparada
    correspondente falhando no `alembic upgrade head` da madrugada.
    """
    with engine_de_teste.connect() as conexao:
        assert "cpf" in _colunas(conexao, "customers")
        assert _existe_indice(conexao, INDICE_DO_CPF)
        assert "claimed_at" in _colunas(conexao, "coupon_claims")
        assert "created_at" in _colunas(conexao, "coupon_claims")


@pytest.mark.db
def test_o_cpf_esta_vazio_em_producao_e_a_premissa_da_0061(engine_de_teste):
    """A premissa que o cabeçalho manda conferir, no banco de teste.

    Aqui ela é trivialmente verdadeira (o banco nasce do baseline e ninguém
    escreve CPF); o valor deste teste é o outro: se um dia alguém voltar a
    escrever `cpf` a partir do código, ele fica vermelho ANTES de a revisão ser
    aplicada em produção. A conferência que vale é a do cabeçalho, contra o
    banco de verdade.
    """
    with engine_de_teste.connect() as conexao:
        com_cpf = conexao.execute(
            text("SELECT count(cpf) FROM customers")
        ).scalar_one()

    assert com_cpf == 0


@pytest.mark.db
def test_o_upgrade_do_cpf_derruba_a_coluna_E_o_indice(engine_de_teste):
    """Ela roda, com CLIENTE na mesa — e o cliente sobrevive.

    Sem a linha, o `DROP COLUMN` seria sobre tabela vazia e não provaria que a
    tabela continua legível depois. É o que separa "a migração roda" de "a
    migração não estraga `customers`", que é a maior tabela de pessoas do banco.
    """
    revisao = _carregar(CPF)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        # `Session` presa a MESMA conexao: e nela que o DDL da migracao roda,
        # e o `rollback` no fim tem que levar as duas coisas juntas. Pelas
        # fabricas, e nao por SQL cru, porque a lista de colunas `NOT NULL` de
        # `customers` ja mudou duas vezes este mes (revisoes 0055 e 0056) e um
        # INSERT escrito a mao aqui envelheceria calado.
        sessao = Session(bind=conexao)
        cliente = fab.criar_cliente(sessao, nome="Cliente da revisao")
        sessao.flush()
        identificador = cliente.id

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        assert "cpf" not in _colunas(conexao, "customers")
        assert not _existe_indice(conexao, INDICE_DO_CPF)

        nome = conexao.execute(
            text("SELECT name FROM customers WHERE id = :id"), {"id": identificador}
        ).scalar_one()
        assert nome == "Cliente da revisao"
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_downgrade_do_cpf_devolve_a_coluna_e_o_indice_vazios(engine_de_teste):
    """Voltar dá a forma de hoje. O cabeçalho promete "vazios", e é isso."""
    revisao = _carregar(CPF)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.downgrade()

        assert "cpf" in _colunas(conexao, "customers")
        assert _existe_indice(conexao, INDICE_DO_CPF)
        assert conexao.execute(text("SELECT count(cpf) FROM customers")).scalar_one() == 0
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_indice_unico_do_cpf_volta_UNICO_e_parcial(engine_de_teste):
    """O downgrade não pode devolver um índice mais frouxo do que o que saiu.

    `UNIQUE` e `WHERE cpf IS NOT NULL` são as duas metades, e a segunda é o que
    permite vários clientes sem CPF conviverem sob um índice único. Recriá-lo
    sem ela faria o segundo cliente sem CPF colidir com o primeiro.
    """
    revisao = _carregar(CPF)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.downgrade()

        definicao = conexao.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": INDICE_DO_CPF},
        ).scalar_one()
    finally:
        transacao.rollback()
        conexao.close()

    assert "UNIQUE" in definicao
    assert "cpf IS NOT NULL" in definicao


@pytest.mark.db
def test_as_duas_colunas_do_resgate_gravam_o_MESMO_instante(engine_de_teste):
    """A premissa da `0062`, exercitada com um resgate de verdade.

    É o que torna a coluna descartável: nenhuma das duas é escrita pelo código,
    as duas caem no `DEFAULT now()` do mesmo INSERT, e `now()` no Postgres é
    `transaction_timestamp()` — o instante em que a TRANSAÇÃO começou. Se um
    dia alguém passar a escrever uma delas, este teste cai.
    """
    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        claim = _um_resgate(conexao)
        iguais = conexao.execute(
            text("SELECT claimed_at = created_at FROM coupon_claims WHERE id = :id"),
            {"id": claim},
        ).scalar_one()
    finally:
        transacao.rollback()
        conexao.close()

    assert iguais is True


@pytest.mark.db
def test_o_upgrade_do_resgate_leva_claimed_at_e_deixa_created_at(engine_de_teste):
    """Ela roda, com RESGATE na mesa, e o instante do resgate não se perde.

    É a promessa do cabeçalho: o que se apaga é uma cópia, e o original fica em
    `created_at`. Sobre tabela vazia isso não se prova.
    """
    revisao = _carregar(CLAIMED_AT)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        claim = _um_resgate(conexao)
        antes = conexao.execute(
            text("SELECT created_at FROM coupon_claims WHERE id = :id"), {"id": claim}
        ).scalar_one()

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()

        assert "claimed_at" not in _colunas(conexao, "coupon_claims")
        depois = conexao.execute(
            text("SELECT created_at FROM coupon_claims WHERE id = :id"), {"id": claim}
        ).scalar_one()
        assert depois == antes
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_o_downgrade_do_resgate_devolve_a_coluna_NOT_NULL_com_default(engine_de_teste):
    """Sem o `DEFAULT now()`, o `ADD COLUMN NOT NULL` falharia sobre as linhas
    que já existem — e falharia no meio do rollback, que é o pior lugar."""
    revisao = _carregar(CLAIMED_AT)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        _um_resgate(conexao)

        with Operations.context(MigrationContext.configure(conexao)):
            revisao.upgrade()
        with Operations.context(MigrationContext.configure(conexao)):
            revisao.downgrade()

        coluna = next(
            c
            for c in inspect(conexao).get_columns("coupon_claims")
            if c["name"] == "claimed_at"
        )
        assert coluna["nullable"] is False
        assert coluna["default"] is not None
    finally:
        transacao.rollback()
        conexao.close()


def _um_resgate(conexao) -> uuid.UUID:
    """Um cupom resgatado por um cliente, na conexao que roda o DDL.

    `Session(bind=conexao)` e nao uma sessao propria: e a conexao que carrega a
    transacao que volta, e duas transacoes fariam o `rollback` deixar metade do
    cenario para tras. As fabricas entram por cima dela pelo motivo do teste do
    CPF — INSERT cru aqui envelhece com o schema.
    """
    sessao = Session(bind=conexao)
    restaurante = fab.criar_restaurante(sessao, nome="Teste da preparada")
    cliente = fab.criar_cliente(sessao)
    sessao.flush()

    # A ARTE vem antes do cupom: `restaurant_coupons.coupon_template_id` e
    # `NOT NULL` desde a revisao `20260828_0043` — todo cupom sai de uma arte
    # do catalogo global.
    arte = CouponTemplate(
        name=f"Arte {uuid.uuid4().hex[:6]}",
        image_path="coupons/arte.png",
        discount_type="fixed",
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=True,
    )
    sessao.add(arte)
    sessao.flush()

    cupom = RestaurantCoupon(
        restaurant_id=restaurante.id,
        coupon_template_id=arte.id,
        code=f"CUP{uuid.uuid4().hex[:6].upper()}",
        title="Campanha da revisao preparada",
        discount_type="fixed",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        valid_from=datetime.now(timezone.utc) - timedelta(days=1),
        first_order_only=False,
        visibility="public",
    )
    sessao.add(cupom)
    sessao.flush()

    claim = CouponClaim(coupon_id=cupom.id, customer_id=cliente.id)
    sessao.add(claim)
    sessao.flush()
    return claim.id
