"""`total_usage_count` na tela de cupons do painel.

Contra banco de verdade por dois motivos que o dublê não pega: a conta é um
`GROUP BY` de verdade, e o que se quer provar dela é quantas idas ao banco
custa — um contador por cupom dentro do `list_admin` seria N+1, e a tela que o
consome é justamente a do restaurante com muitas campanhas.

O número conta só redenção em `applied`, que é a mesma conta que `evaluate`
faz para decidir se o cupom ainda vale. Pedido cancelado estorna a redenção,
devolve a vaga, e some daqui junto: a tela mostra o número que barra o próximo
cliente, não um histórico de tentativas.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event

from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.repositories.coupon_repository import CouponRepository
from src.schemas.coupon_schema import CouponCreate, CouponUpdate
from src.services.coupon_service import CouponService
from tests.fabricas_db import criar_cliente, criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db


AGORA = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def criar_template(db, nome: str = "Desconto") -> CouponTemplate:
    """Uma arte. Dois cupons do MESMO restaurante nao cabem na mesma.

    `restaurant_coupons_restaurant_template_unique` e UNIQUE em
    `(restaurant_id, coupon_template_id)` no banco de producao, e o model nao a
    declara — descoberta ao escrever este arquivo. Por isso todo teste daqui
    que precisa de dois cupons cria dois templates.
    """
    template = CouponTemplate(
        name=nome,
        image_path=f"coupons/{nome.lower()}.png",
        discount_type="fixed",
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=True,
    )
    db.add(template)
    db.flush()
    return template


def criar_cupom(db, restaurante, template, *, code: str, total_usage_limit: int | None = None) -> RestaurantCoupon:
    cupom = RestaurantCoupon(
        restaurant_id=restaurante.id,
        coupon_template_id=template.id,
        code=code,
        title=f"Campanha {code}",
        discount_type="fixed",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        valid_from=AGORA - timedelta(days=1),
        valid_until=AGORA + timedelta(days=30),
        total_usage_limit=total_usage_limit,
        first_order_only=False,
        is_public=True,
        is_active=True,
    )
    db.add(cupom)
    db.flush()
    return cupom


def resgatar(db, cupom, restaurante, filial, *, status: str = "applied") -> None:
    """Uma redenção com o pedido que a FK exige."""
    cliente = criar_cliente(db)
    pedido = criar_pedido(db, restaurante, filial, cliente=cliente)
    CouponRepository(db).create_redemption(
        coupon_id=cupom.id,
        customer_id=cliente.id,
        order_id=pedido.id,
        discount_amount=Decimal("10.00"),
        idempotency_key=f"order:{pedido.id}",
    )
    if status != "applied":
        redemption = CouponRepository(db).get_redemption_by_order_id(pedido.id)
        CouponRepository(db).reverse_redemption(redemption)


def test_cupom_sem_uso_vem_zero_e_nao_nulo(db):
    """O `GROUP BY` não devolve grupo vazio; quem chama resolve com `.get(id, 0)`.

    Se esse default se perder, a tela do cupom novo mostra "usados: —" em vez
    de "usados: 0", que é a diferença entre "ninguém usou" e "não sei".
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db)
    criar_cupom(db, restaurante, template, code="NOVO", total_usage_limit=100)

    resposta = CouponService(db).list_admin(restaurante.id)[0]

    assert resposta.total_usage_count == 0
    assert resposta.total_usage_limit == 100


def test_conta_so_o_que_esta_aplicado(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    template = criar_template(db)
    cupom = criar_cupom(db, restaurante, template, code="USADO")
    resgatar(db, cupom, restaurante, filial)
    resgatar(db, cupom, restaurante, filial)
    resgatar(db, cupom, restaurante, filial, status="reversed")

    resposta = CouponService(db).list_admin(restaurante.id)[0]

    assert resposta.total_usage_count == 2


def test_o_contador_bate_com_o_que_o_limite_total_enxerga(db):
    """A tela e a regra precisam contar a mesma coisa.

    `evaluate` barra o cupom quando `count_applied_total >= total_usage_limit`.
    Se o painel contasse o estorno junto, mostraria 3/3 num cupom que ainda
    aceita cliente — e o lojista fecharia uma campanha viva.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    template = criar_template(db)
    cupom = criar_cupom(db, restaurante, template, code="LIMITE", total_usage_limit=3)
    resgatar(db, cupom, restaurante, filial)
    resgatar(db, cupom, restaurante, filial, status="reversed")

    resposta = CouponService(db).list_admin(restaurante.id)[0]

    assert resposta.total_usage_count == CouponRepository(db).count_applied_total(cupom.id)
    assert resposta.total_usage_count == 1


def test_cada_cupom_leva_o_proprio_numero(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    dez = criar_cupom(db, restaurante, criar_template(db, "Arte Dez"), code="DEZ")
    zero = criar_cupom(db, restaurante, criar_template(db, "Arte Zero"), code="ZERO")
    resgatar(db, dez, restaurante, filial)

    por_codigo = {item.code: item.total_usage_count for item in CouponService(db).list_admin(restaurante.id)}

    assert por_codigo == {"DEZ": 1, "ZERO": 0}
    assert zero.code == "ZERO"


def test_a_lista_conta_todos_os_cupons_numa_consulta_so(db):
    """A ressalva que motivou a agregação: `list_admin` não pode ser N+1.

    Conta as idas a `coupon_redemptions` durante a chamada. Com um contador por
    cupom seriam cinco; com o `GROUP BY` é uma, e continua sendo uma quando o
    restaurante tiver cinquenta campanhas.
    """
    restaurante = criar_restaurante(db)
    for indice in range(5):
        criar_cupom(db, restaurante, criar_template(db, f"Arte {indice}"), code=f"CAMPANHA{indice}")

    consultas: list[str] = []

    def anotar(conn, cursor, statement, parameters, context, executemany):
        if "coupon_redemptions" in statement:
            consultas.append(statement)

    conexao = db.get_bind()
    event.listen(conexao, "before_cursor_execute", anotar)
    try:
        respostas = CouponService(db).list_admin(restaurante.id)
    finally:
        event.remove(conexao, "before_cursor_execute", anotar)

    assert len(respostas) == 5
    assert len(consultas) == 1


def test_lista_vazia_nao_consulta_redencao(db):
    """`count_applied_by_coupon([])` volta sem SQL — `IN ()` não é consulta."""
    restaurante = criar_restaurante(db)

    consultas: list[str] = []

    def anotar(conn, cursor, statement, parameters, context, executemany):
        if "coupon_redemptions" in statement:
            consultas.append(statement)

    conexao = db.get_bind()
    event.listen(conexao, "before_cursor_execute", anotar)
    try:
        respostas = CouponService(db).list_admin(restaurante.id)
    finally:
        event.remove(conexao, "before_cursor_execute", anotar)

    assert respostas == []
    assert consultas == []


def test_o_cupom_do_vizinho_nao_entra_na_conta(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    vizinho = criar_restaurante(db, nome="Vizinho")
    filial_do_vizinho = criar_filial(db, vizinho)
    template = criar_template(db)
    meu = criar_cupom(db, restaurante, template, code="MEU")
    dele = criar_cupom(db, vizinho, template, code="DELE")
    resgatar(db, meu, restaurante, filial)
    resgatar(db, dele, vizinho, filial_do_vizinho)
    resgatar(db, dele, vizinho, filial_do_vizinho)

    resposta = CouponService(db).list_admin(restaurante.id)

    assert len(resposta) == 1
    assert resposta[0].total_usage_count == 1


def test_o_post_devolve_zero_sem_consultar_redencao(db):
    """Cupom recem-criado nao tem redencao: o zero e sabido, nao consultado.

    `coupon_redemptions.order_id` referencia um pedido, e nao existe pedido
    feito com um cupom que acabou de nascer.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db)

    consultas: list[str] = []

    def anotar(conn, cursor, statement, parameters, context, executemany):
        if "coupon_redemptions" in statement:
            consultas.append(statement)

    conexao = db.get_bind()
    event.listen(conexao, "before_cursor_execute", anotar)
    try:
        resposta = CouponService(db).create_admin(
            restaurante.id,
            CouponCreate(
                coupon_template_id=template.id,
                code="NOVO",
                title="Campanha nova",
                discount_type="fixed",
                discount_value=Decimal("10"),
                valid_from=AGORA - timedelta(days=1),
                valid_until=AGORA + timedelta(days=30),
            ),
        )
    finally:
        event.remove(conexao, "before_cursor_execute", anotar)

    assert resposta.total_usage_count == 0
    assert consultas == []


def test_o_patch_devolve_o_contador_e_nao_nulo(db):
    """A linha editada nao pode perder o numero bem na hora em que o lojista olha."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    template = criar_template(db)
    cupom = criar_cupom(db, restaurante, template, code="EDITADO")
    resgatar(db, cupom, restaurante, filial)

    resposta = CouponService(db).update_admin(restaurante.id, cupom.id, CouponUpdate(title="Outro titulo"))

    assert resposta.title == "Outro titulo"
    assert resposta.total_usage_count == 1
