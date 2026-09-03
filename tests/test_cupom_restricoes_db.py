"""As restricoes do cupom (forma de pagamento e horario) contra o Postgres.

O que so o banco prova: que as colunas existem com o CHECK certo — forma
fora da lista nao entra, lista vazia nao entra, horario pela metade nao
entra —, e que o painel grava e le a restricao de ponta a ponta pelo
service.
"""

from datetime import time
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.schemas.coupon_schema import CouponCreate, CouponUpdate
from src.services.coupon_service import CouponService
from tests.fabricas_db import criar_restaurante
from tests.test_coupons_admin_db import AGORA, criar_template


pytestmark = pytest.mark.db


def _inserir_cupom(db: Session, restaurante, template, **extras) -> None:
    colunas = {
        "restaurant_id": restaurante.id,
        "coupon_template_id": template.id,
        "title": "x",
        "discount_type": "fixed",
        "discount_value": 10,
        "min_order_value": 0,
        "valid_from": AGORA,
        "allowed_payment_methods": None,
        "valid_hours_from": None,
        "valid_hours_until": None,
    }
    colunas.update(extras)
    db.execute(
        text(
            "INSERT INTO restaurant_coupons (restaurant_id, coupon_template_id, title, "
            "discount_type, discount_value, min_order_value, valid_from, "
            "allowed_payment_methods, valid_hours_from, valid_hours_until) "
            "VALUES (:restaurant_id, :coupon_template_id, :title, :discount_type, "
            ":discount_value, :min_order_value, :valid_from, "
            "CAST(:allowed_payment_methods AS text[]), :valid_hours_from, :valid_hours_until)"
        ),
        colunas,
    )
    db.flush()


class TestOCheckDaFormaDePagamento:
    def test_forma_fora_da_lista_nao_entra(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")

        with pytest.raises(IntegrityError):
            _inserir_cupom(db, restaurante, template, allowed_payment_methods=["banana"])

    def test_lista_vazia_nao_entra(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")

        with pytest.raises(IntegrityError):
            _inserir_cupom(db, restaurante, template, allowed_payment_methods=[])

    def test_e_a_lista_certa_entra(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")

        _inserir_cupom(db, restaurante, template, allowed_payment_methods=["pix", "cash"])


class TestOCheckDoHorario:
    def test_horario_pela_metade_nao_entra(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")

        with pytest.raises(IntegrityError):
            _inserir_cupom(db, restaurante, template, valid_hours_from=time(15, 0))

    def test_inicio_igual_ao_fim_nao_entra(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")

        with pytest.raises(IntegrityError):
            _inserir_cupom(db, restaurante, template, valid_hours_from=time(15, 0), valid_hours_until=time(15, 0))

    def test_faixa_que_vira_a_noite_entra(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")

        _inserir_cupom(db, restaurante, template, valid_hours_from=time(22, 0), valid_hours_until=time(2, 0))


class TestOPainelGravaELe:
    def test_cria_edita_e_le_as_restricoes(self, db: Session):
        restaurante = criar_restaurante(db)
        template = criar_template(db, discount_type="fixed")
        service = CouponService(db)
        service.clock = lambda: AGORA

        criado = service.create_admin(
            restaurante.id,
            CouponCreate(
                coupon_template_id=template.id,
                code="PIXTARDE",
                title="Pix da tarde",
                discount_type="fixed",
                discount_value=Decimal("10"),
                valid_from=AGORA,
                allowed_payment_methods=["pix"],
                valid_hours_from=time(15, 0),
                valid_hours_until=time(18, 0),
            ),
        )
        assert criado.allowed_payment_methods == ["pix"]
        assert criado.valid_hours_from == time(15, 0)
        assert criado.valid_hours_until == time(18, 0)

        editado = service.update_admin(
            restaurante.id, criado.id, CouponUpdate(allowed_payment_methods=None)
        )
        assert editado.allowed_payment_methods is None
        assert editado.valid_hours_from == time(15, 0)

        lido = service.list_admin(restaurante.id)[0]
        assert lido.allowed_payment_methods is None
        assert lido.valid_hours_until == time(18, 0)
