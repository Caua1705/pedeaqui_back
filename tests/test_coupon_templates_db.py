"""A lista de templates de cupom que o painel consome, contra o Postgres.

Contra banco de verdade porque o que pode dar errado aqui é SQL: o filtro de
`is_active` e a ordem. A ordem importa mais do que parece — o painel monta um
seletor de arte, e `sort_order` repetido sem desempate faz a mesma tela sair
com os cartões trocados de lugar a cada requisição.

A tabela é da PLATAFORMA: não tem `restaurant_id`, não tem rota de escrita, e
por isso não há isolamento de restaurante a provar aqui.
"""

from decimal import Decimal

import pytest

from src.models.coupon_model import CouponTemplate
from src.repositories.coupon_repository import CouponRepository
from src.services.coupon_service import CouponService


pytestmark = pytest.mark.db


def criar_template(db, nome, *, sort_order=0, is_active=True, discount_type="fixed"):
    template = CouponTemplate(
        name=nome,
        image_path=f"coupons/{nome.lower()}.png",
        discount_type=discount_type,
        discount_value=Decimal("10"),
        sort_order=sort_order,
        is_active=is_active,
    )
    db.add(template)
    db.flush()
    return template


def test_template_desativado_fica_de_fora(db):
    criar_template(db, "Visivel")
    criar_template(db, "Aposentado", is_active=False)

    nomes = [template.name for template in CouponRepository(db).list_active_templates()]

    assert nomes == ["Visivel"]


def test_ordem_e_sort_order_e_o_nome_desempata(db):
    criar_template(db, "Bravo", sort_order=1)
    criar_template(db, "Alfa", sort_order=1)
    criar_template(db, "Primeiro", sort_order=0)

    nomes = [template.name for template in CouponRepository(db).list_active_templates()]

    assert nomes == ["Primeiro", "Alfa", "Bravo"]


def test_a_resposta_leva_o_caminho_e_a_url_do_bucket(db):
    """O painel precisa dos dois: o caminho para gravar, a URL para desenhar.

    Quem monta a URL do bucket e o backend (`build_storage_url`). Devolver so
    o `image_path` obrigaria o painel a ter a segunda copia da configuracao do
    Supabase, que e como as duas passam a divergir.
    """
    template = criar_template(db, "Frete Gratis", discount_type="free_delivery")

    resposta = CouponService(db).list_templates()[0]

    assert resposta.id == template.id
    assert resposta.name == "Frete Gratis"
    assert resposta.discount_type == "free_delivery"
    assert resposta.sort_order == 0
    assert resposta.image_path == "coupons/frete gratis.png"
    assert resposta.image_url is not None
    assert resposta.image_url.endswith("coupons/frete gratis.png")
