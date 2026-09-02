"""A ordem da vitrine de cupons, e o `sort_order` que o painel nao gravava.

As duas superfícies públicas já ordenavam por `sort_order` — e ordenavam
**diferente**: `CouponRepository.list_in_window` desempatava por `created_at
desc` e `MenuRepository.get_active_coupons` não desempatava nada. Como
`CouponCreate` e `CouponUpdate` não tinham o campo, **todo cupom ficava no
`DEFAULT 0` da coluna** e o desempate decidia a lista inteira. Resultado: as
duas telas mostravam as mesmas campanhas em ordens diferentes, e uma delas
mudava de ordem entre requisições.

Três coisas, então, e cada uma tem teste:

1. o painel **escreve** (`CouponCreate`/`CouponUpdate`);
2. o painel **lê de volta** (`CouponAdminResponse`), senão não tem como
   desenhar a lista que acabou de gravar;
3. as duas consultas usam a **mesma** expressão, e ela é uma ordem **total**.

Os testes de banco são os que valem para (3): sem `ORDER BY` completo, a ordem
de um `SELECT` é decisão do planejador, e planejador não se dubla.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.models.coupon_model import CouponTemplate, RestaurantCoupon, ordem_dos_cupons
from src.repositories.coupon_repository import CouponRepository
from src.repositories.menu_repository import MenuRepository
from src.schemas.coupon_schema import CouponAdminResponse, CouponCreate, CouponUpdate
from tests.fabricas_db import criar_restaurante


AGORA = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def campos_de_campanha(**extras) -> dict:
    """O corpo mínimo que `CouponCreate` aceita, mais o que o teste quiser."""
    base = {
        "coupon_template_id": "11111111-1111-1111-1111-111111111111",
        "title": "Dez por cento",
        "discount_type": "percent",
        "discount_value": Decimal("10"),
        "valid_from": AGORA,
    }
    base.update(extras)
    return base


class TestOPainelEscreve:
    """(1) O campo existe na criação e no PATCH."""

    def test_a_criacao_aceita_sort_order(self):
        assert CouponCreate(**campos_de_campanha(sort_order=5)).sort_order == 5

    def test_sem_o_campo_o_cupom_nasce_em_zero(self):
        """Zero é a posição de quem nunca foi arrastado, e é o `DEFAULT` da
        coluna — o corpo antigo do painel continua válido."""
        assert CouponCreate(**campos_de_campanha()).sort_order == 0

    def test_negativo_e_recusado(self):
        with pytest.raises(ValidationError):
            CouponCreate(**campos_de_campanha(sort_order=-1))

    def test_e_a_recusa_acima_e_do_campo_certo(self):
        """A mesma chamada com dado correto NÃO levanta.

        Sem esta metade, um `ValidationError` vindo de qualquer outro campo
        deixaria o teste de cima verde descrevendo uma validação que não
        existe.
        """
        assert CouponCreate(**campos_de_campanha(sort_order=0)).sort_order == 0

    def test_o_patch_manda_so_o_que_veio(self):
        """`update_admin` funde por `exclude_unset`: o campo tem que chegar ao
        merge, e só ele."""
        assert CouponUpdate(sort_order=3).model_dump(exclude_unset=True) == {"sort_order": 3}

    def test_o_patch_sem_o_campo_nao_mexe_nele(self):
        assert "sort_order" not in CouponUpdate(title="Outro").model_dump(exclude_unset=True)


class TestOPainelLeDeVolta:
    """(2) Sem devolver a posição, o painel não desenha a lista que gravou."""

    def test_a_resposta_do_painel_tem_a_posicao(self):
        assert "sort_order" in CouponAdminResponse.model_fields


class TestAOrdemETotal:
    """(3), a metade que dá para afirmar sem banco."""

    def test_a_ordem_termina_em_chave_unica(self):
        """Sem uma chave única no fim, dois cupons com o mesmo `sort_order`
        criados na mesma transação (`created_at` é um `now()` só) podem sair
        trocados entre duas requisições."""
        ultima = ordem_dos_cupons()[-1]

        assert ultima.element.primary_key

    def test_a_ordem_tem_as_tres_chaves(self):
        assert len(ordem_dos_cupons()) == 3


# ---------------------------------------------------------------------------
# O lado SQL, contra o Postgres de verdade
# ---------------------------------------------------------------------------


def _arte(db, nome: str) -> CouponTemplate:
    template = CouponTemplate(
        name=nome,
        image_path=f"coupons/{nome}.png",
        discount_type="percent",
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=True,
    )
    db.add(template)
    db.flush()
    return template


def _vitrine_fora_de_ordem(db):
    """Três campanhas, cadastradas na ordem TROCADA de propósito.

    Cadastrar já na ordem certa faria o teste passar com ou sem `ORDER BY` —
    numa tabela pequena e recém-escrita o Postgres costuma devolver na ordem de
    inserção. Verde pelo motivo errado.
    """
    restaurante = criar_restaurante(db)

    def cupom(titulo: str, posicao: int) -> RestaurantCoupon:
        # UMA ARTE POR CUPOM. `restaurant_coupons_restaurant_template_unique`
        # so deixa uma campanha por arte em cada loja, entao reusar o mesmo
        # template daria violacao de unicidade — e nao a ordem que se quer
        # medir.
        linha = RestaurantCoupon(
            restaurant_id=restaurante.id,
            coupon_template_id=_arte(db, titulo).id,
            code=titulo.upper(),
            title=titulo,
            discount_type="percent",
            discount_value=Decimal("10"),
            min_order_value=Decimal("0"),
            valid_from=AGORA - timedelta(days=1),
            valid_until=None,
            first_order_only=False,
            visibility="public",
            is_active=True,
            sort_order=posicao,
        )
        db.add(linha)
        db.flush()
        return linha

    terceiro = cupom("Terceiro", 3)
    primeiro = cupom("Primeiro", 1)
    segundo = cupom("Segundo", 2)
    db.commit()
    return restaurante, [primeiro, segundo, terceiro]


@pytest.mark.db
def test_a_lista_do_cliente_respeita_o_sort_order(db):
    restaurante, esperados = _vitrine_fora_de_ordem(db)

    achados = CouponRepository(db).list_in_window(restaurante.id, now=AGORA)

    assert [c.title for c in achados] == [c.title for c in esperados]


@pytest.mark.db
def test_a_vitrine_do_cardapio_respeita_o_sort_order(db):
    """Esta era a que não desempatava nada."""
    restaurante, esperados = _vitrine_fora_de_ordem(db)

    achados = MenuRepository(db).get_active_coupons(restaurante.id)

    assert [c.title for c in achados] == [c.title for c in esperados]


@pytest.mark.db
def test_as_duas_superficies_concordam(db):
    """O ponto do item: as mesmas campanhas, na mesma ordem, nas duas telas."""
    restaurante, _ = _vitrine_fora_de_ordem(db)

    da_lista = CouponRepository(db).list_in_window(restaurante.id, now=AGORA)
    da_vitrine = MenuRepository(db).get_active_coupons(restaurante.id)

    assert [c.id for c in da_lista] == [c.id for c in da_vitrine]


@pytest.mark.db
def test_com_a_posicao_empatada_a_ordem_e_ESTAVEL(db):
    """O estado de hoje: todo cupom em `sort_order = 0`.

    Enquanto o lojista não arrastar nada, é o desempate que decide a lista
    inteira. Ele tem que devolver a mesma coisa toda vez — senão a vitrine
    pisca sem nada ter mudado.
    """
    restaurante = criar_restaurante(db)
    for indice in range(6):
        db.add(
            RestaurantCoupon(
                restaurant_id=restaurante.id,
                coupon_template_id=_arte(db, f"Empate {indice}").id,
                code=f"EMPATE{indice}",
                title=f"Empate {indice}",
                discount_type="percent",
                discount_value=Decimal("10"),
                min_order_value=Decimal("0"),
                valid_from=AGORA - timedelta(days=1),
                valid_until=None,
                first_order_only=False,
                visibility="public",
                is_active=True,
                sort_order=0,
            )
        )
    db.commit()

    repositorio = CouponRepository(db)
    primeira = [c.id for c in repositorio.list_in_window(restaurante.id, now=AGORA)]
    db.expire_all()
    segunda = [c.id for c in repositorio.list_in_window(restaurante.id, now=AGORA)]

    assert primeira == segunda
    assert [c.id for c in MenuRepository(db).get_active_coupons(restaurante.id)] == primeira
