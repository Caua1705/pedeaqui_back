"""Cupom e template precisam concordar no tipo de desconto.

Contra banco de verdade porque a checagem depende de LER o template: o tipo
que a vitrine anuncia mora numa tabela, o tipo que o checkout desconta mora em
outra, e o defeito que este arquivo tranca e justamente os dois divergirem.

Por que isso e propaganda enganosa, e nao detalhe de consistencia:
`template.discount_type` nao entra em `calculate_discount` — quem desconta e
sempre `coupon.discount_type`. A arte de frete gratis com um cupom `percent`
por baixo mostra "Frete gratis" na tela do cliente e tira 10% no pagamento,
sem erro em lugar nenhum.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.schemas.coupon_schema import CouponCreate, CouponUpdate
from src.services.coupon_service import CouponService
from tests.fabricas_db import criar_restaurante


pytestmark = pytest.mark.db


AGORA = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def criar_template(db, *, discount_type: str, nome: str = "Arte", is_active: bool = True) -> CouponTemplate:
    template = CouponTemplate(
        name=nome,
        image_path=f"coupons/{nome.lower()}.png",
        discount_type=discount_type,
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=is_active,
    )
    db.add(template)
    db.flush()
    return template


def payload_de_criacao(template: CouponTemplate, *, discount_type: str, code: str = "PROMO10") -> CouponCreate:
    return CouponCreate(
        coupon_template_id=template.id,
        code=code,
        title="Promocao",
        discount_type=discount_type,
        discount_value=Decimal("0") if discount_type == "free_delivery" else Decimal("10"),
        valid_from=AGORA - timedelta(days=1),
        valid_until=AGORA + timedelta(days=30),
    )


def gravar_cupom_divergente(db, restaurante, template: CouponTemplate) -> RestaurantCoupon:
    """O par errado que nenhuma rota grava mais — so SQL cru chega aqui.

    Escrito pelo model de proposito: passar pelo service e impossivel desde a
    checagem que este arquivo cobre, e o teste precisa da linha ja gravada para
    provar o que acontece com quem a herdou.
    """
    cupom = RestaurantCoupon(
        restaurant_id=restaurante.id,
        coupon_template_id=template.id,
        code="HERDADO",
        title="Gravado antes da regra",
        discount_type="percent",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        valid_from=AGORA - timedelta(days=1),
        valid_until=AGORA + timedelta(days=30),
        first_order_only=False,
        is_public=True,
        is_active=True,
    )
    db.add(cupom)
    db.flush()
    return cupom


def test_criar_com_tipos_iguais_passa(db):
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="free_delivery", nome="Frete Gratis")

    resposta = CouponService(db).create_admin(
        restaurante.id,
        payload_de_criacao(template, discount_type="free_delivery"),
    )

    assert resposta.discount_type == "free_delivery"
    assert resposta.coupon_template_id == template.id


def test_criar_com_tipos_divergentes_responde_422(db):
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="free_delivery", nome="Frete Gratis")

    with pytest.raises(HTTPException) as erro:
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(template, discount_type="percent"),
        )

    assert erro.value.status_code == 422
    assert "percent" in erro.value.detail
    assert "free_delivery" in erro.value.detail


def test_criar_divergente_nao_grava_nada(db):
    """422 e recusa, nao meia gravacao."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")

    with pytest.raises(HTTPException):
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(template, discount_type="percent", code="NAODEVEEXISTIR"),
        )

    encontrado = CouponService(db).repository.get_by_code_and_restaurant("NAODEVEEXISTIR", restaurante.id)
    assert encontrado is None


def test_template_invalido_continua_400_e_nao_422(db):
    """A ordem importa: template desativado e 400, divergencia e 422.

    Se a checagem de tipo rodasse antes, um template aposentado sairia como
    "tipo nao confere" e o painel mandaria o lojista trocar o campo errado.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="percent", is_active=False)

    with pytest.raises(HTTPException) as erro:
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(template, discount_type="percent"),
        )

    assert erro.value.status_code == 400


def test_editar_para_um_tipo_que_o_template_nao_tem_responde_422(db):
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(
            restaurante.id,
            cupom.id,
            CouponUpdate(discount_type="percent", discount_value=Decimal("10")),
        )

    assert erro.value.status_code == 422


def test_editar_trocando_a_arte_junto_com_o_tipo_passa(db):
    """Trocar os dois de uma vez e o caminho legitimo de mudar de campanha."""
    restaurante = criar_restaurante(db)
    template_fixo = criar_template(db, discount_type="fixed", nome="Desconto")
    template_frete = criar_template(db, discount_type="free_delivery", nome="Frete Gratis")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template_fixo, discount_type="fixed"))

    resposta = servico.update_admin(
        restaurante.id,
        cupom.id,
        CouponUpdate(
            coupon_template_id=template_frete.id,
            discount_type="free_delivery",
            discount_value=Decimal("0"),
        ),
    )

    assert resposta.discount_type == "free_delivery"
    assert resposta.coupon_template_id == template_frete.id


def test_cupom_ja_divergente_no_banco_recusa_ate_o_patch_que_nao_toca_no_tipo(db):
    """A aresta descrita no docstring de `update_admin`, travada aqui.

    A validacao roda sobre o MERGE, entao um par errado herdado do banco
    contamina qualquer PATCH. Nao ha par assim em producao — o SELECT foi
    rodado antes —, e este teste existe para o sintoma ser reconhecivel se
    alguem gravar um por SQL.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="free_delivery", nome="Frete Gratis")
    cupom = gravar_cupom_divergente(db, restaurante, template)

    with pytest.raises(HTTPException) as erro:
        CouponService(db).update_admin(restaurante.id, cupom.id, CouponUpdate(is_active=False))

    assert erro.value.status_code == 422


def test_cupom_ja_divergente_e_consertado_pelo_proprio_patch(db):
    """E a saida da aresta acima: o PATCH que alinha os dois passa."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="free_delivery", nome="Frete Gratis")
    cupom = gravar_cupom_divergente(db, restaurante, template)

    resposta = CouponService(db).update_admin(
        restaurante.id,
        cupom.id,
        CouponUpdate(discount_type="free_delivery", discount_value=Decimal("0"), is_active=False),
    )

    assert resposta.discount_type == "free_delivery"
    assert resposta.is_active is False
