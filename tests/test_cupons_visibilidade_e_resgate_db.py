"""Visibilidade em três valores, resgate sem sacola, e cupom sem código.

Contra banco de verdade porque as três coisas que este arquivo prova só
existem lá:

- **os CHECK da revisão `20260828_0043`**. `(visibility = 'segment') =
  (target_segment IS NOT NULL)` é uma regra do schema, e o schema é o único
  lugar onde ela vale para escrita que não passe pelo service — inclusive SQL
  cru numa janela de emergência;
- **o UNIQUE com `code` nulo.** Que `NULL` não colide com `NULL` num índice
  único é comportamento do Postgres, e a frente inteira de cupom automático
  depende dele. Um dublê concordaria com qualquer coisa que este arquivo
  afirmasse;
- **o segmento RFV**, que é uma agregação em SQL sobre `orders`. Ele é o gate
  do cupom de segmento, e testá-lo com dublê seria testar o dublê.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.repositories.coupon_repository import CouponRepository
from src.schemas.coupon_schema import CouponClaimRequest
from src.services.coupon_service import CouponService
from tests.fabricas_db import criar_cliente, criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db


AGORA = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def criar_template(db, nome: str = "Desconto") -> CouponTemplate:
    """Uma arte por cupom: `(restaurant_id, coupon_template_id)` é UNIQUE."""
    template = CouponTemplate(
        name=nome,
        image_path=f"coupons/{nome.lower().replace(' ', '-')}.png",
        discount_type="fixed",
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=True,
    )
    db.add(template)
    db.flush()
    return template


def criar_cupom(
    db,
    restaurante,
    template,
    *,
    code: str | None = "PROMO10",
    visibility: str = "public",
    target_segment: str | None = None,
    min_order_value: Decimal = Decimal("0"),
) -> RestaurantCoupon:
    cupom = RestaurantCoupon(
        restaurant_id=restaurante.id,
        coupon_template_id=template.id,
        code=code,
        title=f"Campanha {code or 'automatica'}",
        discount_type="fixed",
        discount_value=Decimal("10"),
        min_order_value=min_order_value,
        valid_from=AGORA - timedelta(days=1),
        valid_until=AGORA + timedelta(days=30),
        first_order_only=False,
        visibility=visibility,
        target_segment=target_segment,
        is_active=True,
        sort_order=0,
    )
    db.add(cupom)
    db.flush()
    return cupom


def servico(db, restaurante) -> CouponService:
    """O service com o relógio preso e o restaurante já resolvido pelo slug."""
    service = CouponService(db)
    service.clock = lambda: AGORA
    service.restaurant_service.get_active_restaurant = lambda slug: restaurante
    return service


# --------------------------------------------------------------------------
# Os CHECK da migração
# --------------------------------------------------------------------------


def test_segmento_sem_alvo_e_recusado_pelo_banco(db):
    """O CHECK, e não o schema Pydantic — SQL cru também tem que esbarrar."""
    restaurante = criar_restaurante(db)
    template = criar_template(db)
    with pytest.raises(IntegrityError):
        criar_cupom(db, restaurante, template, visibility="segment", target_segment=None)
    db.rollback()


def test_alvo_em_cupom_publico_e_recusado_pelo_banco(db):
    """O outro lado do CHECK, que é o que costuma faltar.

    Alvo preenchido num cupom público não filtra nada — a lista ignora — e o
    lojista veria na tela uma segmentação que não existe.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db)
    with pytest.raises(IntegrityError):
        criar_cupom(db, restaurante, template, visibility="public", target_segment="perdido")
    db.rollback()


def test_visibilidade_fora_dos_tres_valores_e_recusada(db):
    restaurante = criar_restaurante(db)
    template = criar_template(db)
    with pytest.raises(IntegrityError):
        criar_cupom(db, restaurante, template, visibility="talvez")
    db.rollback()


def test_varios_cupons_sem_codigo_convivem_no_mesmo_restaurante(db):
    """A propriedade do Postgres de que a auto-aplicação inteira depende.

    `NULL` é distinto de `NULL` num índice único, então os três índices de
    código de `restaurant_coupons` continuam valendo sem nenhuma mudança —
    e o restaurante pode ter mais de uma campanha automática.
    """
    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db, "Arte A"), code=None)
    criar_cupom(db, restaurante, criar_template(db, "Arte B"), code=None)
    db.flush()

    sem_codigo = [
        cupom
        for cupom in CouponRepository(db).list_in_window(restaurante.id, now=AGORA)
        if cupom.code is None
    ]
    assert len(sem_codigo) == 2


def test_codigo_repetido_continua_recusado(db):
    """O nullable não afrouxou o UNIQUE para quem TEM código."""
    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db, "Arte A"), code="PROMO10")
    with pytest.raises(IntegrityError):
        criar_cupom(db, restaurante, criar_template(db, "Arte B"), code="PROMO10")
    db.rollback()


# --------------------------------------------------------------------------
# O segmento, medido em SQL sobre os pedidos de verdade
# --------------------------------------------------------------------------


def test_cliente_sem_pedido_nenhum_e_novo(db):
    restaurante = criar_restaurante(db)
    cliente = criar_cliente(db, phone="85911110000")
    assert (
        CouponRepository(db).segment_of_customer(restaurante.id, cliente.phone, AGORA) == "novo"
    )


def test_quem_sumiu_ha_muito_tempo_e_perdido(db):
    """Dois pedidos com cadência de 7 dias, o último há 90: `perdido`.

    A escada é a de `customer_segment.py` — a mesma da lista de clientes do
    painel. É esse acoplamento que faz o lojista ver "perdido" na tela e o
    cupom de `perdido` chegar exatamente naquela pessoa.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    cliente = criar_cliente(db, phone="85911110001")
    for dias in (97, 90):
        criar_pedido(
            db,
            restaurante,
            filial,
            cliente=cliente,
            created_at=AGORA - timedelta(days=dias),
            customer_phone_snapshot=cliente.phone,
        )
    db.flush()

    assert (
        CouponRepository(db).segment_of_customer(restaurante.id, cliente.phone, AGORA)
        == "perdido"
    )


def test_o_segmento_e_por_TELEFONE_entao_pedido_de_convidado_conta(db):
    """Quem pediu como convidado antes de criar a conta não é `novo`.

    O recorte por telefone é o mesmo do painel, e é o que impede o cupom de
    "primeira compra por segmento" de ir parar na mão de quem já pediu três
    vezes sem estar logado.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    telefone = "85911110002"
    for dias in (40, 33, 26):
        criar_pedido(
            db,
            restaurante,
            filial,
            cliente=None,
            created_at=AGORA - timedelta(days=dias),
            customer_phone_snapshot=telefone,
        )
    db.flush()

    assert CouponRepository(db).segment_of_customer(restaurante.id, telefone, AGORA) != "novo"


# --------------------------------------------------------------------------
# O gate, ponta a ponta
# --------------------------------------------------------------------------


def test_privado_so_aparece_para_quem_resgatou(db):
    """O item 1 da frente, travado onde ele de fato vale.

    Antes do resgate o cupom não está na lista — nem como card cinza. Depois
    do resgate ele aparece inteiro, com código.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db)
    criar_cupom(db, restaurante, template, code="SECRETO", visibility="private")
    cliente = criar_cliente(db, phone="85911110003")
    service = servico(db, restaurante)

    def listar():
        return service.list_for_customer(
            restaurante.slug,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            order_type="pickup",
            customer=cliente,
        ).coupons

    assert listar() == []

    resgatado = service.claim(restaurante.slug, CouponClaimRequest(code="SECRETO"), cliente)
    assert resgatado.coupon.code == "SECRETO"
    assert [card.code for card in listar()] == ["SECRETO"]


def test_resgate_e_idempotente_e_nao_conta_como_uso(db):
    """RESGATE não é USO — a distinção que a tabela nova existe para manter.

    Duas chamadas geram UMA linha em `coupon_claims` (o UNIQUE), e nenhuma
    em `coupon_redemptions`: o teto da campanha continua intacto para quem
    ainda não fechou pedido.
    """
    restaurante = criar_restaurante(db)
    cupom = criar_cupom(
        db, restaurante, criar_template(db), code="SECRETO", visibility="private"
    )
    cliente = criar_cliente(db, phone="85911110004")
    service = servico(db, restaurante)

    service.claim(restaurante.slug, CouponClaimRequest(code="SECRETO"), cliente)
    service.claim(restaurante.slug, CouponClaimRequest(code="SECRETO"), cliente)

    repositorio = CouponRepository(db)
    assert repositorio.claimed_coupon_ids(cliente.id) == {cupom.id}
    assert repositorio.count_applied_total(cupom.id) == 0


def test_resgate_de_codigo_que_nao_existe_e_de_segmento_alheio_respondem_igual(db):
    """Uma frase só para todos os jeitos de o código não servir.

    Distinguir os casos transformaria a rota num oráculo de quais códigos
    existem, e é isso que o limite por IP encarece e esta resposta torna
    inútil (armadilha 18).
    """
    restaurante = criar_restaurante(db)
    criar_cupom(
        db,
        restaurante,
        criar_template(db),
        code="VOLTA15",
        visibility="segment",
        target_segment="perdido",
    )
    # Sem pedido nenhum, este cliente é `novo`, e não `perdido`.
    cliente = criar_cliente(db, phone="85911110005")
    service = servico(db, restaurante)

    respostas = []
    for codigo in ("NAOEXISTE", "VOLTA15"):
        with pytest.raises(Exception) as capturado:
            service.claim(restaurante.slug, CouponClaimRequest(code=codigo), cliente)
        respostas.append((capturado.value.status_code, capturado.value.detail))

    assert respostas[0] == respostas[1]
    assert respostas[0][0] == 404


def test_cupom_de_segmento_chega_a_quem_se_encaixa_com_a_etiqueta(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_cupom(
        db,
        restaurante,
        criar_template(db),
        code=None,
        visibility="segment",
        target_segment="perdido",
    )
    cliente = criar_cliente(db, phone="85911110006")
    for dias in (97, 90):
        criar_pedido(
            db,
            restaurante,
            filial,
            cliente=cliente,
            created_at=AGORA - timedelta(days=dias),
            customer_phone_snapshot=cliente.phone,
        )
    db.flush()

    cards = servico(db, restaurante).list_for_customer(
        restaurante.slug,
        subtotal=Decimal("100"),
        delivery_fee=Decimal("0"),
        order_type="pickup",
        customer=cliente,
    ).coupons

    assert len(cards) == 1
    assert cards[0].label == "selected_for_you"
    assert cards[0].state == "applicable"
    # Sem código: este é o que aplica sozinho no checkout.
    assert cards[0].code is None


def test_a_vitrine_do_cardapio_nao_mostra_segmento_nem_privado(db):
    """`= 'public'`, e nunca `!= 'private'`.

    `GET /{slug}/menu` é anônima e não tem cliente para avaliar. Se o filtro
    fosse por exclusão, a campanha de reativação apareceria com código para
    qualquer pessoa que abrisse o cardápio.
    """
    from src.repositories.menu_repository import MenuRepository

    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db, "Arte A"), code="ABERTO")
    criar_cupom(
        db, restaurante, criar_template(db, "Arte B"), code="SECRETO", visibility="private"
    )
    criar_cupom(
        db,
        restaurante,
        criar_template(db, "Arte C"),
        code="VOLTA15",
        visibility="segment",
        target_segment="perdido",
    )
    db.flush()

    vitrine = MenuRepository(db).get_active_coupons(restaurante.id)
    assert [cupom.code for cupom in vitrine] == ["ABERTO"]


# --------------------------------------------------------------------------
# Auto-aplicação
# --------------------------------------------------------------------------


def test_auto_aplicacao_escolhe_o_maior_desconto(db):
    """Entre dois automáticos que cabem, vale o maior.

    O cliente não tem tela para escolher — ele nem sabe que há dois. Dar o
    menor seria a casa anunciando um desconto e entregando outro.
    """
    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db, "Arte A"), code=None)
    maior = criar_cupom(db, restaurante, criar_template(db, "Arte B"), code=None)
    maior.discount_value = Decimal("25")
    cliente = criar_cliente(db, phone="85911110007")
    db.flush()

    escolhido, desconto = servico(db, restaurante).auto_apply_for_order(
        restaurant_id=restaurante.id,
        subtotal=Decimal("100"),
        delivery_fee=Decimal("0"),
        customer=cliente,
    )
    assert escolhido.id == maior.id
    assert desconto == Decimal("25.00")


def test_cupom_com_codigo_nunca_aplica_sozinho(db):
    """Ter código é a declaração de que a pessoa precisa digitá-lo."""
    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db), code="PROMO10")
    cliente = criar_cliente(db, phone="85911110008")
    db.flush()

    assert (
        servico(db, restaurante).auto_apply_for_order(
            restaurant_id=restaurante.id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=cliente,
        )
        is None
    )


def test_auto_aplicacao_respeita_o_minimo_da_campanha(db):
    """A mesma `evaluate` da listagem e do checkout — não há segunda regra."""
    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db), code=None, min_order_value=Decimal("80"))
    cliente = criar_cliente(db, phone="85911110009")
    db.flush()
    service = servico(db, restaurante)

    assert (
        service.auto_apply_for_order(
            restaurant_id=restaurante.id,
            subtotal=Decimal("50"),
            delivery_fee=Decimal("0"),
            customer=cliente,
        )
        is None
    )
    assert service.auto_apply_for_order(
        restaurant_id=restaurante.id,
        subtotal=Decimal("80"),
        delivery_fee=Decimal("0"),
        customer=cliente,
    ) is not None


def test_convidado_nao_ganha_cupom_automatico(db):
    """Não é política: `coupon_redemptions.customer_id` é NOT NULL.

    Um desconto automático para convidado não teria onde registrar o uso, e o
    teto por cliente da campanha deixaria de existir para quem não entrasse
    na conta.
    """
    restaurante = criar_restaurante(db)
    criar_cupom(db, restaurante, criar_template(db), code=None)
    db.flush()

    assert (
        servico(db, restaurante).auto_apply_for_order(
            restaurant_id=restaurante.id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=None,
        )
        is None
    )
