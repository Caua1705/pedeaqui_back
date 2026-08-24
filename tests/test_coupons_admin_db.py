"""Cupom e template precisam concordar no tipo E no valor do desconto.

Contra banco de verdade porque a checagem depende de LER o template: o que a
vitrine anuncia mora numa tabela, o que o checkout desconta mora em outra, e o
defeito que este arquivo tranca e justamente os dois divergirem.

Por que isso e propaganda enganosa, e nao detalhe de consistencia: nada do
template entra em `calculate_discount` — quem desconta e sempre o par
`coupon.discount_type` / `coupon.discount_value`. Sao dois eixos, e os dois
mentem sem erro em lugar nenhum:

- a arte de frete gratis com um cupom `percent` por baixo mostra "Frete gratis"
  na tela do cliente e tira 10% no pagamento;
- a arte de "10% OFF" com um cupom de 7% por baixo anuncia dez e tira sete.

O valor so passou a ser conferido em 23/08/2026. Ate ali, quem segurava a
coerencia era o painel, que copia o valor da arte ao montar o POST — protecao
de tela, que some no dia em que aquela tela ganhar um campo editavel.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.schemas.coupon_schema import CouponCreate, CouponUpdate
from src.services.coupon_service import CouponService
from tests.fabricas_db import criar_restaurante


pytestmark = pytest.mark.db


AGORA = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)


def valor_padrao_do_tipo(discount_type: str) -> Decimal:
    """O valor que uma arte daquele tipo teria no catalogo.

    Frete gratis nao tem percentual nem reais para anunciar, e a coluna e
    `NOT NULL` com default 0 no banco — entao 0 e o valor de verdade dela, nao
    um placeholder de teste. Os outros dois anunciam um numero, e 10 serve para
    os dois (10% ou R$ 10).
    """
    return Decimal("0") if discount_type == "free_delivery" else Decimal("10")


def criar_template(
    db,
    *,
    discount_type: str,
    nome: str = "Arte",
    is_active: bool = True,
    discount_value: Decimal | None = None,
) -> CouponTemplate:
    template = CouponTemplate(
        name=nome,
        image_path=f"coupons/{nome.lower()}.png",
        discount_type=discount_type,
        discount_value=valor_padrao_do_tipo(discount_type) if discount_value is None else discount_value,
        sort_order=0,
        is_active=is_active,
    )
    db.add(template)
    db.flush()
    return template


def mensagens(erro: HTTPException) -> str:
    """Todas as `msg` do 422 numa string so, para o assert ler bem.

    O `detail` do 422 desta rota e lista de `{loc, msg, type}` — a mesma forma
    que o FastAPI usa para erro de corpo.
    """
    return " | ".join(item["msg"] for item in erro.detail)


def payload_de_criacao(
    template: CouponTemplate,
    *,
    discount_type: str,
    code: str = "PROMO10",
    discount_value: Decimal | None = None,
) -> CouponCreate:
    """Por omissao, o corpo que o PAINEL monta: valor copiado da arte.

    Quem quer divergencia de valor a pede por escrito, com `discount_value`.
    Foi de proposito que o padrao virou "concorda": teste que diverge sem
    querer passaria a falhar por um motivo que nao e o dele.
    """
    return CouponCreate(
        coupon_template_id=template.id,
        code=code,
        title="Promocao",
        discount_type=discount_type,
        discount_value=template.discount_value if discount_value is None else discount_value,
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
    """So o tipo diverge: os dois valores sao 10, e o unico erro e o do tipo."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")

    with pytest.raises(HTTPException) as erro:
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(template, discount_type="percent"),
        )

    assert erro.value.status_code == 422
    assert len(erro.value.detail) == 1
    assert erro.value.detail[0]["loc"] == ["body", "discount_type"]
    assert erro.value.detail[0]["type"] == "coupon_template_discount_type_mismatch"
    assert "percent" in mensagens(erro.value)
    assert "fixed" in mensagens(erro.value)


def test_criar_com_valores_divergentes_responde_422(db):
    """A arte de "10% OFF" com um cupom de 7% por baixo: mesmo tipo, outro numero.

    Este e o buraco que o `discount_type` sozinho nao fechava. Grava e passa,
    e o cliente ve dez na vitrine e sete no pagamento.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="percent", nome="10% OFF")

    with pytest.raises(HTTPException) as erro:
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(template, discount_type="percent", discount_value=Decimal("7")),
        )

    assert erro.value.status_code == 422
    assert len(erro.value.detail) == 1
    assert erro.value.detail[0]["loc"] == ["body", "discount_value"]
    assert erro.value.detail[0]["type"] == "coupon_template_discount_value_mismatch"
    assert "7" in mensagens(erro.value)
    assert "10" in mensagens(erro.value)


def test_o_valor_divergente_nao_grava_nada(db):
    """422 e recusa, nao meia gravacao — a mesma prova que o tipo ja tinha."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="percent")

    with pytest.raises(HTTPException):
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(
                template,
                discount_type="percent",
                discount_value=Decimal("7"),
                code="NEMESTE",
            ),
        )

    assert CouponService(db).repository.get_by_code_and_restaurant("NEMESTE", restaurante.id) is None


def test_a_mesma_escala_nao_e_exigida_10_bate_com_10_00(db):
    """`numeric(10,2)` no template, `numeric(12,2)` no cupom.

    A comparacao e numerica, nao textual: exigir a mesma representacao faria
    `Decimal("10")` recusar `Decimal("10.00")` e o painel nao teria como saber
    qual das duas mandar.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="percent", discount_value=Decimal("10"))

    resposta = CouponService(db).create_admin(
        restaurante.id,
        payload_de_criacao(template, discount_type="percent", discount_value=Decimal("10.00")),
    )

    assert resposta.discount_value == Decimal("10.00")


def test_trocar_so_a_arte_diverge_nos_dois_eixos_e_os_dois_erros_saem_juntos(db):
    """Uma lista com os dois, e nao um 422 de cada vez.

    Trocar a arte de "10% OFF" para "Frete gratis" sem mexer no cupom diverge
    no tipo E no valor de uma vez. Cuspir um por vez faria o lojista consertar
    o tipo, tomar 422 de novo pelo valor, e concluir que a tela esta quebrada.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="free_delivery", nome="Frete Gratis")

    with pytest.raises(HTTPException) as erro:
        CouponService(db).create_admin(
            restaurante.id,
            payload_de_criacao(template, discount_type="percent", discount_value=Decimal("10")),
        )

    assert erro.value.status_code == 422
    tipos = [item["type"] for item in erro.value.detail]
    assert tipos == [
        "coupon_template_discount_type_mismatch",
        "coupon_template_discount_value_mismatch",
    ]


def test_editar_para_um_valor_que_a_arte_nao_anuncia_responde_422(db):
    """O PATCH tem a mesma guarda do POST: a arte nao muda sozinha."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="percent", nome="10% OFF")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="percent"))

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(restaurante.id, cupom.id, CouponUpdate(discount_value=Decimal("7")))

    assert erro.value.status_code == 422
    assert erro.value.detail[0]["loc"] == ["body", "discount_value"]


def test_editar_o_valor_junto_com_a_arte_que_o_anuncia_passa(db):
    """A saida legitima: quem quer descontar outro numero troca de campanha."""
    restaurante = criar_restaurante(db)
    dez = criar_template(db, discount_type="percent", nome="10% OFF", discount_value=Decimal("10"))
    sete = criar_template(db, discount_type="percent", nome="7% OFF", discount_value=Decimal("7"))
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(dez, discount_type="percent"))

    resposta = servico.update_admin(
        restaurante.id,
        cupom.id,
        CouponUpdate(coupon_template_id=sete.id, discount_value=Decimal("7")),
    )

    assert resposta.discount_value == Decimal("7.00")
    assert resposta.coupon_template_id == sete.id


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


def aposentar(db, template: CouponTemplate) -> None:
    """A plataforma tira a arte do catalogo, DEPOIS de a campanha existir.

    E a ordem que importa: a arte estava ativa quando o cupom nasceu, entao
    nenhuma checagem de criacao chega perto deste caso.
    """
    template.is_active = False
    db.flush()


def test_desligar_a_campanha_com_a_arte_aposentada_passa(db):
    """O cupom preso: `{"is_active": false}` sozinho respondia 400.

    A checagem da arte roda sobre o resultado da MESCLA, e a mescla repete a
    arte que ja esta gravada. Bastava a plataforma desativar uma arte para o
    lojista nao conseguir nem editar nem desligar a campanha — e nao havia
    PATCH que o tirasse de la, porque trocar de arte tambem e edicao.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))
    aposentar(db, template)

    resposta = servico.update_admin(restaurante.id, cupom.id, CouponUpdate(is_active=False))

    assert resposta.is_active is False
    assert resposta.coupon_template_id == template.id


def test_editar_qualquer_campo_com_a_arte_aposentada_passa(db):
    """Nao era so o desligar: TODO PATCH morria, inclusive o que so muda texto."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))
    aposentar(db, template)

    resposta = servico.update_admin(restaurante.id, cupom.id, CouponUpdate(title="Promocao de agosto"))

    assert resposta.title == "Promocao de agosto"


def test_escolher_uma_arte_aposentada_continua_400(db):
    """A outra metade da regra: MANTER passa, ESCOLHER nao.

    Sem este teste, "deixe o PATCH passar" viraria "aceite qualquer arte", e o
    seletor do painel — que so lista arte ativa — deixaria de significar
    alguma coisa.
    """
    restaurante = criar_restaurante(db)
    atual = criar_template(db, discount_type="fixed", nome="Atual")
    aposentada = criar_template(db, discount_type="fixed", nome="Aposentada", is_active=False)
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(atual, discount_type="fixed"))

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(restaurante.id, cupom.id, CouponUpdate(coupon_template_id=aposentada.id))

    assert erro.value.status_code == 400
    assert erro.value.detail == "Template de cupom inválido"


def test_a_arte_aposentada_nao_dispensa_a_concordancia_de_tipo(db):
    """Manter a arte nao e passe livre: o tipo continua sendo conferido contra ela."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))
    aposentar(db, template)

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(
            restaurante.id,
            cupom.id,
            CouponUpdate(discount_type="percent", discount_value=Decimal("10")),
        )

    assert erro.value.status_code == 422


def test_arte_ja_usada_responde_409_falando_da_arte(db):
    """O 409 que mandava o lojista mexer no campo errado.

    `restaurant_coupons_restaurant_template_unique` e UNIQUE em
    `(restaurant_id, coupon_template_id)`: uma arte por restaurante. A violacao
    dela saia como "Codigo de cupom ja existe neste restaurante", entao o
    lojista trocava o codigo e tomava 409 de novo — para sempre, porque o campo
    que a mensagem apontava nao era o que estava colidindo.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed", code="PRIMEIRO"))

    with pytest.raises(HTTPException) as erro:
        servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed", code="SEGUNDO"))

    assert erro.value.status_code == 409
    assert "arte" in erro.value.detail
    assert "Código" not in erro.value.detail


def test_a_mesma_arte_em_restaurantes_diferentes_passa(db):
    """O UNIQUE e por restaurante: a arte e do catalogo da plataforma."""
    template = criar_template(db, discount_type="fixed")
    primeiro = criar_restaurante(db)
    segundo = criar_restaurante(db, nome="Outro")
    servico = CouponService(db)

    servico.create_admin(primeiro.id, payload_de_criacao(template, discount_type="fixed"))
    resposta = servico.create_admin(segundo.id, payload_de_criacao(template, discount_type="fixed"))

    assert resposta.restaurant_id == segundo.id


def test_editar_para_uma_arte_ja_usada_responde_409_da_arte(db):
    restaurante = criar_restaurante(db)
    ocupada = criar_template(db, discount_type="fixed", nome="Ocupada")
    livre = criar_template(db, discount_type="fixed", nome="Livre")
    servico = CouponService(db)
    servico.create_admin(restaurante.id, payload_de_criacao(ocupada, discount_type="fixed", code="OCUPA"))
    meu = servico.create_admin(restaurante.id, payload_de_criacao(livre, discount_type="fixed", code="MEU"))

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(restaurante.id, meu.id, CouponUpdate(coupon_template_id=ocupada.id))

    assert erro.value.status_code == 409
    assert "arte" in erro.value.detail


def test_o_model_declara_os_dois_unique_com_o_nome_do_banco(db):
    """O nome nao e decorativo: e por ele que `_raise_conflict` decide a mensagem.

    Conferido contra o banco de verdade — o `schema_baseline.sql` e um pg_dump
    de producao, entao o que esta aqui e o que existe la.
    """
    declarados = {
        constraint.name
        for constraint in RestaurantCoupon.__table__.constraints
        if constraint.name is not None
    }
    no_banco = set(
        db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'restaurant_coupons' AND indexdef LIKE 'CREATE UNIQUE%'"
            )
        ).scalars()
    )

    assert "restaurant_coupons_restaurant_code_unique" in declarados
    assert "restaurant_coupons_restaurant_template_unique" in declarados
    assert declarados - {"restaurant_coupons_pkey"} <= no_banco


def test_patch_que_invalida_a_mescla_responde_422_e_nao_500(db):
    """`valid_until` antes do `valid_from` respondia "Internal Server Error".

    A revalidacao da mescla levanta `ValidationError` do pydantic, e o FastAPI
    so traduz a que ele mesmo levanta ao montar o corpo — a que sai de dentro do
    handler subia como excecao qualquer.
    """
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(
            restaurante.id,
            cupom.id,
            CouponUpdate(valid_until=AGORA - timedelta(days=10)),
        )

    assert erro.value.status_code == 422
    assert "valid_until" in mensagens(erro.value)


def test_o_422_da_mescla_tem_a_mesma_forma_do_422_do_template(db):
    """Uma forma so na rota: o painel nao precisa adivinhar qual chegou."""
    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))

    with pytest.raises(HTTPException) as da_mescla:
        servico.update_admin(restaurante.id, cupom.id, CouponUpdate(discount_value=Decimal("0")))
    with pytest.raises(HTTPException) as do_template:
        servico.update_admin(
            restaurante.id,
            cupom.id,
            CouponUpdate(discount_type="percent", discount_value=Decimal("10")),
        )

    for erro in (da_mescla.value, do_template.value):
        assert erro.status_code == 422
        assert isinstance(erro.detail, list)
        for item in erro.detail:
            assert set(item) == {"loc", "msg", "type"}
            assert item["loc"][0] == "body"


def test_o_422_da_mescla_atravessa_o_json(db):
    """`input` e `ctx` do pydantic carregam Decimal e datetime, que nao serializam."""
    import json

    restaurante = criar_restaurante(db)
    template = criar_template(db, discount_type="fixed")
    servico = CouponService(db)
    cupom = servico.create_admin(restaurante.id, payload_de_criacao(template, discount_type="fixed"))

    with pytest.raises(HTTPException) as erro:
        servico.update_admin(
            restaurante.id,
            cupom.id,
            CouponUpdate(valid_from=AGORA + timedelta(days=90)),
        )

    assert json.dumps({"detail": erro.value.detail})
