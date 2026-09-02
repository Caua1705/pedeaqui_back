"""Cupom sem prazo (`valid_until` nulo) e campanha permanente, nao defeito.

O levantamento da armadilha 50 achou `restaurant_coupons.valid_until` entre as
16 colunas em que o ORM diz `NOT NULL` e o banco aceita `NULL`, e a classificou
como a **pior das oito de risco real**: `CouponService._aware(None)` levanta
`AttributeError`, e `evaluate` abre com ele — no caminho do dinheiro.

**A decisao foi consertar o CODIGO, e nao o schema.** Cupom sem data de fim e
uma campanha plausivel ("10% no canal proprio, sem prazo") e o precedente esta
no mesmo modulo: a revisao `20260828_0043` tornou `code` nulo **com
significado**. Aqui o banco esta certo e quem mente e o model.

Este arquivo cobra as tres coisas que isso exige, e a terceira e a que a rodada
anterior teria deixado passar:

1. o cupom sem prazo **atravessa o checkout inteiro** e aplica desconto;
2. ele **aparece** nas duas superficies que listam cupom — e nao so numa;
3. as duas formas da regra (SQL e Python) concordam nos mesmos casos, porque
   sao a mesma regra escrita duas vezes.
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services.coupon_service import CouponService
from src.schemas.coupon_schema import CouponPreviewRequest
from src.services.coupon_window import dentro_da_janela, ja_acabou, ja_comecou
from tests import fabricas


AGORA = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
ONTEM = AGORA - timedelta(days=1)
AMANHA = AGORA + timedelta(days=1)


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeCouponRepository:
    """Repositorio que RESPEITA o filtro de janela, como o SQL real.

    Ele recebe as condicoes prontas e nao as reinterpreta: a busca por codigo
    devolve o cupom so quando `dentro_da_janela` concorda. E o comportamento do
    `where()` visto de fora, e e o que faz este dublê nao mentir sobre a parte
    que o teste esta medindo.
    """

    def __init__(self, coupon):
        self.coupon = coupon
        self.buscas = []

    def _visivel(self, agora: datetime) -> bool:
        return dentro_da_janela(self.coupon.valid_from, self.coupon.valid_until, agora)

    def get_by_code_and_restaurant(self, code, restaurant_id, *, for_update=False, agora=None):
        """O CODIGO importa, e o dublê tem que respeita-lo.

        Ele ignorava o `code` e devolvia o cupom para qualquer string — o que
        fazia "codigo que nao existe" e "codigo que venceu" chegarem ao mesmo
        lugar, apagando justamente a distincao que esta classe de testes mede.
        O SQL real compara `lower(code)`.
        """
        self.buscas.append((code, agora))
        if self.coupon.code is None or self.coupon.code.lower() != code.strip().lower():
            return None
        if agora is not None and not self._visivel(agora):
            return None
        return self.coupon

    def get_by_id_and_restaurant(self, coupon_id, restaurant_id, *, for_update=False, agora=None):
        if agora is not None and not self._visivel(agora):
            return None
        return self.coupon

    def lock_coupon(self, restaurant_id, *, coupon_id=None, coupon_code=None, agora=None):
        if agora is not None and not self._visivel(agora):
            return None
        return self.coupon

    def count_applied_total(self, coupon_id):
        return 0

    def count_applied_by_customer(self, coupon_id, customer_id):
        return 0

    def get_last_applied_redemption_for_customer(self, coupon_id, customer_id):
        return None

    def customer_has_valid_order(self, customer_id, restaurant_id):
        return False

    def claimed_coupon_ids(self, customer_id):
        return set()

    def segment_of_customer(self, restaurant_id, customer_phone, now):
        return "fiel"


def build_service(coupon):
    service = CouponService.__new__(CouponService)
    service.db = FakeDb()
    service.repository = FakeCouponRepository(coupon)
    service.clock = lambda: AGORA
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: fabricas.restaurante(id=coupon.restaurant_id)
    )
    service.restaurant_repository = SimpleNamespace(
        get_by_id=lambda restaurant_id: fabricas.restaurante(id=coupon.restaurant_id)
    )
    return service


class CupomSemPrazoTests(unittest.TestCase):
    """O caminho do dinheiro, inteiro, com `valid_until` nulo."""

    def _cupom_permanente(self):
        return fabricas.cupom(
            code="SEMPRAZO",
            valid_from=ONTEM,
            valid_until=None,
            discount_type="percent",
            discount_value=Decimal("10.00"),
            min_order_value=Decimal("0.00"),
        )

    def test_o_cupom_sem_prazo_atravessa_a_avaliacao(self):
        """O teste que via vermelho antes: `_aware(None)` levantava aqui.

        `AttributeError` dentro de `evaluate`, que e por onde passam o preview
        E o `lock_and_validate_for_order`. Nao era 400 nem mensagem: era 500 no
        checkout de quem digitou um codigo de campanha permanente.
        """
        coupon = self._cupom_permanente()
        service = build_service(coupon)

        avaliacao = service.evaluate(
            coupon,
            restaurant_id=coupon.restaurant_id,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0.00"),
            customer=fabricas.cliente(),
        )

        self.assertTrue(avaliacao.valid, avaliacao.reason)
        self.assertEqual(avaliacao.discount, Decimal("10.00"))

    def test_o_cupom_sem_prazo_trava_e_valida_no_pedido(self):
        """`lock_and_validate_for_order` — A VALIDACAO QUE VALE.

        Tudo antes dela e preview. Se `_find_coupon` filtrasse a janela sem o
        `IS NULL`, o cupom permanente sumiria aqui e viraria 404 no fechamento
        do pedido — o defeito trocado de lugar, nao consertado.
        """
        coupon = self._cupom_permanente()
        service = build_service(coupon)

        travado, desconto = service.lock_and_validate_for_order(
            restaurant_id=coupon.restaurant_id,
            coupon_id=None,
            coupon_code="SEMPRAZO",
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0.00"),
            customer=fabricas.cliente(),
        )

        self.assertIs(travado, coupon)
        self.assertEqual(desconto, Decimal("10.00"))

    def test_o_cupom_VENCIDO_continua_recusado(self):
        """O outro lado: "sem prazo" nao pode virar "todo prazo e opcional".

        Sem este teste, a correcao passaria trocando o `>` por `if valid_until
        is not None` mal colocado e o cupom vencido voltaria a valer — que e
        pior que o 500, porque o lojista paga desconto de campanha encerrada.
        """
        coupon = fabricas.cupom(
            code="VENCIDO", valid_from=ONTEM - timedelta(days=30), valid_until=ONTEM
        )
        service = build_service(coupon)

        with self.assertRaises(HTTPException):
            service.lock_and_validate_for_order(
                restaurant_id=coupon.restaurant_id,
                coupon_id=None,
                coupon_code="VENCIDO",
                subtotal=Decimal("100.00"),
                delivery_fee=Decimal("0.00"),
                customer=fabricas.cliente(),
            )


class AsDuasFormasDaRegraTests(unittest.TestCase):
    """SQL e Python dizem a mesma coisa — e o `IS NULL` esta nas duas."""

    def test_nulo_nunca_acabou(self):
        self.assertFalse(ja_acabou(None, AGORA))
        self.assertFalse(ja_acabou(None, AGORA + timedelta(days=3650)))

    def test_data_no_passado_acabou(self):
        self.assertTrue(ja_acabou(ONTEM, AGORA))

    def test_data_no_futuro_nao_acabou(self):
        self.assertFalse(ja_acabou(AMANHA, AGORA))

    def test_o_instante_exato_do_fim_ainda_vale(self):
        """`>` e nao `>=`: o cupom que termina "hoje as 12h" vale as 12h em ponto.

        A comparacao espelha o `valid_until >= agora` do SQL. Trocar por `>=`
        aqui faria a mesma linha aparecer na vitrine e ser recusada no
        checkout — a divergencia que este modulo existe para impedir.
        """
        self.assertFalse(ja_acabou(AGORA, AGORA))

    def test_data_de_inicio_no_futuro_nao_comecou(self):
        self.assertFalse(ja_comecou(AMANHA, AGORA))

    def test_ingenuo_do_banco_e_lido_como_utc(self):
        """Linha gravada por fora pode chegar sem fuso, e subtrair ingenuo de
        consciente levanta TypeError — que aqui viraria 500 no checkout."""
        self.assertTrue(ja_comecou(ONTEM.replace(tzinfo=None), AGORA))
        self.assertTrue(ja_acabou(ONTEM.replace(tzinfo=None), AGORA))

    def test_o_filtro_sql_traz_o_IS_NULL(self):
        """A metade que faltava, afirmada na propria expressao.

        Sem o `IS NULL`, `NULL >= agora` nao e falso — e NULO —, e a campanha
        permanente simplesmente nao apareceria em consulta nenhuma. O defeito
        seria o cupom existir no painel e nao existir para o cliente.
        """
        from src.services.coupon_window import filtro_de_janela

        sql = " ".join(str(condicao) for condicao in filtro_de_janela(AGORA))

        self.assertIn("valid_until IS NULL", sql)
        self.assertIn("valid_from <=", sql)

    def test_dentro_da_janela_e_a_conjuncao_das_duas(self):
        self.assertTrue(dentro_da_janela(ONTEM, None, AGORA))
        self.assertTrue(dentro_da_janela(ONTEM, AMANHA, AGORA))
        self.assertFalse(dentro_da_janela(AMANHA, None, AGORA))
        self.assertFalse(dentro_da_janela(ONTEM, ONTEM, AGORA))


class ANaoExisteXVenceuTests(unittest.TestCase):
    """"Nao existe" e "existe e venceu" nao podem ser a mesma resposta.

    A diferenca decide o que a pessoa faz em seguida. **"Cupom nao encontrado"
    para um codigo que existe manda ela conferir se digitou errado e tentar de
    novo**; "cupom vencido" encerra o assunto. O primeiro e uma frase que o
    cliente nao tem como resolver.

    Isto foi consertado depois de a rodada anterior ter trocado as duas por um
    404 so — o preco de aplicar o filtro de janela dentro de `_find_coupon`.
    O filtro ficou; a mensagem voltou.
    """

    def _cupom_vencido(self):
        return fabricas.cupom(
            code="VENCIDO", valid_from=ONTEM - timedelta(days=30), valid_until=ONTEM
        )

    @staticmethod
    def _tentar(service, codigo):
        return service.lock_and_validate_for_order(
            restaurant_id=service.repository.coupon.restaurant_id,
            coupon_id=None,
            coupon_code=codigo,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0.00"),
            customer=fabricas.cliente(),
        )

    def test_codigo_que_existe_e_venceu_responde_VENCIDO(self):
        service = build_service(self._cupom_vencido())

        with self.assertRaises(HTTPException) as erro:
            self._tentar(service, "VENCIDO")

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(erro.exception.detail, "expired")

    def test_codigo_que_existe_e_nao_comecou_responde_NAO_COMECOU(self):
        """Os dois motivos sao distintos, e a distincao tambem serve ao cliente.

        "Ainda nao comecou" pede que ele volte depois; "venceu" nao. Colapsar
        os dois num 404 apagava as duas informacoes de uma vez.
        """
        service = build_service(
            fabricas.cupom(code="FUTURO", valid_from=AMANHA, valid_until=None)
        )

        with self.assertRaises(HTTPException) as erro:
            self._tentar(service, "FUTURO")

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(erro.exception.detail, "not_started")

    def test_codigo_que_NAO_existe_responde_404(self):
        """O outro lado da distincao, e sem ele o teste de cima nao prova nada.

        Se os dois casos respondessem 400 "expired", a mensagem teria voltado
        errada — e quem digitasse um codigo inexistente leria "cupom vencido"
        sobre um cupom que nunca existiu.
        """
        service = build_service(self._cupom_vencido())

        with self.assertRaises(HTTPException) as erro:
            self._tentar(service, "ESTE-NAO-EXISTE")

        self.assertEqual(erro.exception.status_code, 404)

    def test_o_preview_explica_em_vez_de_levantar(self):
        """O preview nao levanta: ele DEVOLVE a resposta com o motivo.

        E a superficie em que a distincao mais importa — o cliente esta com a
        sacola aberta, e o card precisa dizer o que houve sem tirar ele da
        tela.
        """
        coupon = self._cupom_vencido()
        service = build_service(coupon)

        resposta = service.preview(
            "junior",
            CouponPreviewRequest(
                coupon_code="VENCIDO",
                subtotal=Decimal("100.00"),
                delivery_fee=Decimal("0.00"),
                order_type="pickup",
            ),
            fabricas.cliente(),
        )

        self.assertFalse(resposta.valid)
        self.assertEqual(resposta.ineligibility_reason, "expired")
        self.assertEqual(resposta.coupon_id, coupon.id)


class ADefesaEmProfundidadeTests(unittest.TestCase):
    """As duas formas da regra se conferem — e o desacordo vira 409.

    O `dentro_da_janela` sai do SQL (`filtro_de_janela`); o
    `expired`/`not_started` sai do Python (`evaluate`). Sao as duas formas da
    MESMA regra, e `lock_and_validate_for_order` cobra que elas concordem.

    Nao deveria haver como divergirem — as duas moram lado a lado em
    `coupon_window.py`, exatamente para isso. Mas "nao deveria" foi o que a
    rodada anterior encontrou escrito de tres jeitos diferentes, e aqui o preco
    de estar errado e desconto aplicado num pedido pago.
    """

    def test_sql_dizendo_fora_e_python_dizendo_valido_e_409(self):
        """A guarda, com as duas formas em desacordo de proposito.

        O dublê do repositorio passa a devolver `None` quando ha filtro de
        janela e o cupom quando nao ha — enquanto o cupom esta, para o
        `evaluate`, perfeitamente dentro da janela. Isso reproduz a divergencia
        sem precisar de um bug real, e prova que a guarda responde 409 em vez
        de aplicar o desconto.
        """
        coupon = fabricas.cupom(code="OK", valid_from=ONTEM, valid_until=AMANHA)
        service = build_service(coupon)
        service.repository.lock_coupon = lambda *args, **kwargs: (
            None if kwargs.get("agora") is not None else coupon
        )

        with self.assertRaises(HTTPException) as erro:
            service.lock_and_validate_for_order(
                restaurant_id=coupon.restaurant_id,
                coupon_id=None,
                coupon_code="OK",
                subtotal=Decimal("100.00"),
                delivery_fee=Decimal("0.00"),
                customer=fabricas.cliente(),
            )

        self.assertEqual(erro.exception.status_code, 409)

    def test_a_segunda_consulta_nao_trava_a_linha(self):
        """`FOR UPDATE` num cupom que ja se sabe que nao vai aplicar seguraria
        a linha de outro pedido por nada.

        A primeira busca pede trava; a segunda — a que so existe para separar
        "nao existe" de "venceu" — nao pede.
        """
        coupon = fabricas.cupom(
            code="VENCIDO", valid_from=ONTEM - timedelta(days=30), valid_until=ONTEM
        )
        service = build_service(coupon)
        travas = []
        original = service.repository.lock_coupon

        def registrando(*args, **kwargs):
            travas.append(kwargs.get("agora"))
            return original(*args, **kwargs)

        service.repository.lock_coupon = registrando

        with self.assertRaises(HTTPException):
            service.lock_and_validate_for_order(
                restaurant_id=coupon.restaurant_id,
                coupon_id=None,
                coupon_code="VENCIDO",
                subtotal=Decimal("100.00"),
                delivery_fee=Decimal("0.00"),
                customer=fabricas.cliente(),
            )

        # UMA trava so, e a que pediu janela. A segunda busca foi pelo caminho
        # sem `for_update`.
        self.assertEqual(len(travas), 1)
        self.assertIsNotNone(travas[0])

    def test_o_painel_continua_enxergando_a_campanha_vencida(self):
        """O outro lado do `agora` opcional, e o motivo de ele ser opcional.

        Se o filtro fosse do repositorio para sempre, o lojista perderia a tela
        onde ele edita ou reativa a campanha que acabou.
        """
        coupon = fabricas.cupom(
            code="VENCIDO", valid_from=ONTEM - timedelta(days=30), valid_until=ONTEM
        )
        repositorio = FakeCouponRepository(coupon)

        sem_agora = repositorio.get_by_code_and_restaurant("VENCIDO", coupon.restaurant_id)
        com_agora = repositorio.get_by_code_and_restaurant(
            "VENCIDO", coupon.restaurant_id, agora=AGORA
        )

        self.assertIs(sem_agora, coupon)
        self.assertIsNone(com_agora)


# ---------------------------------------------------------------------------
# O lado SQL, contra o Postgres de verdade
# ---------------------------------------------------------------------------
#
# Os testes acima provam a regra em Python. Nenhum deles prova o `where()` — e
# e la que o `NULL >= agora` silencioso mora: ele nao e falso, e NULO, e a
# linha some da consulta sem nada reclamar. Dublê nenhum reproduz isso; so o
# banco.


@pytest.mark.db
def test_a_campanha_permanente_aparece_nas_duas_superficies(db):
    """`valid_until IS NULL` tem que passar pelos DOIS `where()`.

    `list_in_window` alimenta a lista do cliente e a auto-aplicacao;
    `get_active_coupons` alimenta a vitrine do cardapio. Eram duas copias da
    mesma regra, e consertar uma so faria o cupom sem prazo existir numa tela e
    nao na outra — com o lojista jurando que criou a campanha.
    """
    from src.models.coupon_model import CouponTemplate, RestaurantCoupon
    from src.repositories.coupon_repository import CouponRepository
    from src.repositories.menu_repository import MenuRepository
    from tests.fabricas_db import criar_restaurante

    restaurante = criar_restaurante(db)
    template = CouponTemplate(
        name="Permanente",
        image_path="coupons/permanente.png",
        discount_type="percent",
        discount_value=Decimal("10"),
        sort_order=0,
        is_active=True,
    )
    db.add(template)
    db.flush()

    cupom = RestaurantCoupon(
        restaurant_id=restaurante.id,
        coupon_template_id=template.id,
        code="SEMPRAZO",
        title="Campanha permanente",
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
    db.add(cupom)
    db.flush()

    na_janela = CouponRepository(db).list_in_window(restaurante.id, now=AGORA)
    na_vitrine = MenuRepository(db).get_active_coupons(restaurante.id)

    assert [c.id for c in na_janela] == [cupom.id]
    assert [c.id for c in na_vitrine] == [cupom.id]


@pytest.mark.db
def test_o_banco_aceita_valid_until_nulo(db):
    """A premissa da decisao inteira, afirmada onde ela vale.

    Se um dia alguem alinhar `restaurant_coupons.valid_until` para `NOT NULL`
    — o que as outras 15 colunas do levantamento vao receber —, este teste
    fica vermelho e diz que a campanha permanente deixou de existir. E o unico
    lugar do repositorio que registra que essa coluna e a EXCECAO da lista.
    """
    from sqlalchemy import inspect

    coluna = next(
        info
        for info in inspect(db.get_bind()).get_columns("restaurant_coupons")
        if info["name"] == "valid_until"
    )

    assert coluna["nullable"], (
        "restaurant_coupons.valid_until virou NOT NULL. Se foi de proposito, a "
        "campanha sem prazo deixou de ser possivel e src/services/coupon_window.py "
        "precisa ser reescrito; se foi de carona numa revisao de alinhamento, "
        "esta coluna e a excecao — ver docs/alinhamento-orm-schema.md."
    )


if __name__ == "__main__":
    unittest.main()
