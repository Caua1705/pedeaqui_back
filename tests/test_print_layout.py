"""Desenho das comandas (src/services/print_layout.py).

Este arquivo existe porque a formatacao foi TIRADA do agente local de
proposito. Um layout que mora no agente so pode ser conferido instalando o
agente; morando aqui, cada regra vira uma assercao.

O que estes testes protegem, em ordem de gravidade:

1. **Nenhuma linha estoura a largura.** E o defeito que nao aparece em
   revisao e aparece na bobina: a impressora quebra sozinha onde quiser, e
   "Refrigerante 2L" vira "Refrigerant" numa linha e "e 2L" na seguinte.
2. **A via de producao nao tem preco nenhum.** Quem esta na chapa nao
   precisa do valor e, com ele, tem mais uma linha para ler no aperto.
3. **O grupo do adicional aparece na via de producao.** "Acompanhamento:
   espaguete" e uma TROCA (o arroz nao vai) e "Adicional: espaguete" e uma
   porcao a mais. Sem o grupo, as duas chegam iguais na cozinha e sai
   pedido errado — foi por isso que o detalhe do pedido passou a agrupar.
4. **A via do cliente carrega o que o balcao confere**: numero, cliente,
   telefone, endereco, itens, pagamento, taxas e total.

Os modelos reais sao instanciados (sem sessao) e passam por
`OrderService.to_order_detail_response`, que e exatamente o caminho da rota:
a comanda e desenhada a partir do MESMO objeto que o painel recebe.
"""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.services.order_service import OrderService
from src.schemas.admin_printing_schema import clean_footer_message
from src.services.print_layout import (
    PRODUCTION_WIDTH,
    RECEIPT_WIDTH,
    build_customer_receipt,
    build_production_ticket,
    build_test_ticket,
    format_datetime,
    format_money,
    format_phone,
)


# 09/08/2026 17:32 UTC = 14:32 em America/Fortaleza, o fuso da operacao.
CREATED_AT = datetime(2026, 8, 9, 17, 32, tzinfo=timezone.utc)


def make_option(group_name, option_name, price="0.00", group_id=None):
    return OrderItemOption(
        id=uuid.uuid4(),
        option_group_id=group_id or uuid.uuid4(),
        option_id=uuid.uuid4(),
        option_group_name_snapshot=group_name,
        option_name_snapshot=option_name,
        additional_price_snapshot=Decimal(price),
    )


def make_item(name="Prato feito", quantity=1, total="32.00", options=(), observation=None):
    item = OrderItem(
        id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_code_snapshot=None,
        product_name_snapshot=name,
        product_description_snapshot=None,
        unit_price_snapshot=Decimal(total),
        quantity=quantity,
        observation=observation,
        total=Decimal(total),
        created_at=CREATED_AT,
    )
    item.options = list(options)
    return item


def make_order(items=(), **overrides):
    values = {
        "id": uuid.uuid4(),
        "order_number": 1234,
        "restaurant_id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "customer_name_snapshot": "Maria da Conceicao",
        "customer_phone_snapshot": "85999998888",
        "order_type": "delivery",
        "status": "preparing",
        "payment_method": "cash",
        "payment_flow": "delivery",
        "payment_status": "on_delivery",
        "subtotal": Decimal("32.00"),
        "delivery_fee": Decimal("7.50"),
        "service_fee": Decimal("0.00"),
        # Os defaults destas colunas so valem no INSERT, e aqui o pedido
        # nunca chega ao banco.
        "coupon_discount_amount": Decimal("0.00"),
        "cashback_redeemed_amount": Decimal("0.00"),
        "discount_total": Decimal("0.00"),
        "total": Decimal("39.50"),
        "address_street": "Rua das Flores",
        "address_number": "1450",
        "address_neighborhood": "Papicu",
        "address_city": "Fortaleza",
        "address_state": "CE",
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    order = Order(**values)
    order.items = list(items) or [make_item()]
    order.status_history = []
    return order


def detail_of(items=(), **overrides):
    return OrderService.to_order_detail_response(make_order(items, **overrides))


class WidthTests(unittest.TestCase):
    """A regra que o agente NAO tem como consertar.

    Ele recebe texto e imprime; se a linha chegar maior que a bobina, a
    quebra fica a criterio da impressora.
    """

    def test_the_customer_receipt_never_exceeds_48_columns(self):
        order = detail_of([
            make_item(
                name="Prato feito de carne de sol com nata e macaxeira frita",
                quantity=2,
                total="128.00",
                options=[make_option("Acompanhamento", "Espaguete ao alho e oleo")],
                observation="sem cebola, sem pimenta e com bastante manteiga de garrafa",
            )
        ])

        content = build_customer_receipt(order)

        self.assertTrue(content)
        for line in content.split("\n"):
            self.assertLessEqual(len(line), RECEIPT_WIDTH, line)

    def test_the_production_ticket_never_exceeds_24_columns(self):
        # Metade das colunas porque a via de producao sai em fonte dupla.
        # Montada em 48, ela dobraria linha sozinha na impressora.
        order = detail_of([
            make_item(
                name="Prato feito de carne de sol com nata e macaxeira frita",
                quantity=2,
                total="128.00",
                options=[make_option("Acompanhamento", "Espaguete ao alho e oleo")],
                observation="sem cebola, sem pimenta e com bastante manteiga",
            )
        ])

        content = build_production_ticket(order, "Cozinha", order.items)

        for line in content.split("\n"):
            self.assertLessEqual(len(line), PRODUCTION_WIDTH, line)

    def test_a_word_longer_than_the_line_is_broken_instead_of_overflowing(self):
        order = detail_of([make_item(name="X" * 80)])

        content = build_production_ticket(order, "Chapa", order.items)

        for line in content.split("\n"):
            self.assertLessEqual(len(line), PRODUCTION_WIDTH, line)


class CustomerReceiptTests(unittest.TestCase):
    def test_it_carries_everything_the_counter_checks(self):
        order = detail_of(
            [make_item(name="Prato feito", quantity=2, total="64.00")],
            subtotal=Decimal("64.00"),
            delivery_fee=Decimal("7.50"),
            service_fee=Decimal("2.00"),
            total=Decimal("73.50"),
            address_complement="Bloco C ap 302",
        )

        content = build_customer_receipt(order)

        self.assertIn("PEDIDO #1234", content)
        self.assertIn("Maria da Conceicao", content)
        self.assertIn("(85) 99999-8888", content)
        self.assertIn("Rua das Flores, 1450", content)
        self.assertIn("Papicu", content)
        self.assertIn("Bloco C ap 302", content)
        self.assertIn("2x Prato feito", content)
        self.assertIn("Dinheiro", content)
        self.assertIn("64,00", content)
        self.assertIn("7,50", content)
        self.assertIn("2,00", content)
        self.assertIn("73,50", content)

    def test_the_add_ons_come_with_their_group_and_price(self):
        # O valor do adicional ja esta embutido no preco do item; sai aqui
        # so para o cliente que reclama do total ver de onde veio.
        order = detail_of([
            make_item(options=[make_option("Adicional", "Bacon", price="7.00")])
        ])

        content = build_customer_receipt(order)

        self.assertIn("Adicional: Bacon (+7,00)", content)

    def test_a_pickup_order_says_so_instead_of_printing_an_empty_address(self):
        # Campo em branco na bobina se le como "faltou imprimir".
        order = detail_of(order_type="pickup", delivery_fee=Decimal("0.00"))

        content = build_customer_receipt(order)

        self.assertIn("RETIRADA", content)
        self.assertIn("RETIRADA NO BALCAO", content)
        self.assertNotIn("Rua das Flores", content)

    def test_free_delivery_still_prints_the_line(self):
        # Zero impresso e "a entrega e gratis"; linha ausente e "esqueceram
        # de cobrar a taxa", e o cliente pergunta no balcao.
        order = detail_of(delivery_fee=Decimal("0.00"))

        self.assertIn("Taxa de entrega", build_customer_receipt(order))

    def test_a_discount_names_the_coupon_and_comes_negative(self):
        order = detail_of(
            coupon_code_snapshot="PROMO10",
            coupon_discount_amount=Decimal("6.40"),
            cashback_redeemed_amount=Decimal("2.00"),
            discount_total=Decimal("8.40"),
        )

        content = build_customer_receipt(order)

        self.assertIn("Cupom PROMO10", content)
        self.assertIn("-6,40", content)
        self.assertIn("Cashback", content)
        self.assertIn("-2,00", content)

    def test_an_unpaid_order_prints_the_warning_next_to_the_payment(self):
        # A via do cliente sai mesmo sem pagamento (serve de conferencia no
        # balcao); quem nao sai e a de producao. Mas ela precisa dizer que o
        # dinheiro nao entrou.
        order = detail_of(payment_status="pending", payment_method="pix")

        self.assertIn("AGUARDANDO PAGAMENTO", build_customer_receipt(order))


class ProductionTicketTests(unittest.TestCase):
    def test_it_prints_no_price_at_all(self):
        order = detail_of([
            make_item(
                name="Prato feito",
                quantity=2,
                total="64.00",
                options=[make_option("Adicional", "Bacon", price="7.00")],
            )
        ])

        content = build_production_ticket(order, "Cozinha", order.items)

        self.assertNotIn("64,00", content)
        self.assertNotIn("7,00", content)
        self.assertNotIn("39,50", content)

    def test_the_add_on_group_survives_into_the_kitchen(self):
        # O caso que motivou tudo: trocar o arroz por espaguete e uma
        # instrucao de preparo. Sem o nome do grupo, a cozinha nao sabe se
        # tira o arroz ou se acrescenta uma porcao.
        order = detail_of([
            make_item(options=[make_option("Acompanhamento", "Espaguete")])
        ])

        content = build_production_ticket(order, "Cozinha", order.items)

        self.assertIn("Acompanhamento", content)
        self.assertIn("Espaguete", content)

    def test_it_shows_only_the_items_it_was_given(self):
        # Cada praca recebe a bobina dela. O filtro por setor acontece no
        # service; aqui garantimos que o desenho respeita a lista recebida.
        order = detail_of([
            make_item(name="Prato feito"),
            make_item(name="Refrigerante lata"),
        ])

        content = build_production_ticket(order, "Cozinha", order.items[:1])

        self.assertIn("PRATO FEITO", content)
        self.assertNotIn("REFRIGERANTE", content)

    def test_it_names_the_sector_and_the_order(self):
        order = detail_of()

        content = build_production_ticket(order, "Chapa", order.items)

        self.assertIn("PEDIDO #1234", content)
        self.assertIn("CHAPA", content)

    def test_the_item_observation_goes_to_the_kitchen(self):
        order = detail_of([make_item(observation="ao ponto")])

        self.assertIn("ao ponto", build_production_ticket(order, "Chapa", order.items))


class FooterMessageTests(unittest.TestCase):
    """A mensagem que o lojista escreve para o fim da via do cliente.

    E o unico texto da comanda que uma pessoa escreve MIRANDO a bobina — e
    por isso o unico que pode chegar com quebra de linha, com emoji e com
    coisa que nao e texto. Cada teste aqui guarda uma dessas.
    """

    def test_it_comes_out_at_the_end_of_the_customer_copy(self):
        order = detail_of()

        content = build_customer_receipt(order, RECEIPT_WIDTH, "Peca direto e ganhe 5%")

        self.assertIn("Peca direto e ganhe 5%", content)
        # Depois do total e do pagamento: e rodape, nao mais um campo do
        # pedido no meio da conferencia do balcao.
        self.assertGreater(content.index("Peca direto"), content.index("TOTAL"))

    def test_the_line_break_the_shopkeeper_typed_survives(self):
        """`textwrap` trata quebra de linha como espaco. Sem partir o texto
        antes de embrulhar, "Peca no site" e "@nossaloja" sairiam grudados
        na mesma linha, e o campo de edicao mentiria sobre o resultado."""
        order = detail_of()

        content = build_customer_receipt(
            order, RECEIPT_WIDTH, "Peca no site\n@nossaloja"
        )

        linhas = [line.strip() for line in content.splitlines()]
        self.assertIn("Peca no site", linhas)
        self.assertIn("@nossaloja", linhas)

    def test_a_long_message_never_exceeds_the_width(self):
        # Mesma regra do resto do arquivo, e aqui ela morde mais: o texto e
        # do lojista, nao nosso.
        order = detail_of()
        recado = "Acompanhe o pedido pelo nosso Instagram @juniordapicanha e peca direto"

        content = build_customer_receipt(order, RECEIPT_WIDTH, recado)

        for line in content.splitlines():
            self.assertLessEqual(len(line), RECEIPT_WIDTH, line)

    def test_without_a_message_the_receipt_is_exactly_what_it_was(self):
        # Filial que nao configurou nada nao ganha linha nenhuma a mais: o
        # rodape e espaco de graca so enquanto ninguem o gasta a toa.
        order = detail_of()

        self.assertEqual(
            build_customer_receipt(order, RECEIPT_WIDTH, None),
            build_customer_receipt(order, RECEIPT_WIDTH, ""),
        )

    def test_no_control_byte_survives_into_the_receipt(self):
        """O teste que protege a impressora, e nao o layout.

        O agente escreve `content` direto no fluxo ESC/POS e a codepage
        passa `0x1B` adiante intacto: um ESC colado no meio da mensagem
        deixaria de ser texto e viraria comando, reprogramando a impressora
        no meio da comanda. `encode_text` nao defende contra isso — o
        trabalho dele e o que a TABELA nao tem (acento, emoji), e `0x1B` a
        tabela tem.

        Por isso a assercao e sobre o caminho inteiro: o que o validador
        aceita, desenhado na via, nao pode ter byte de controle nenhum alem
        da propria quebra de linha.
        """
        sujo = "Peca\x1b|8@ direto\x00 no site\x07"
        order = detail_of()

        content = build_customer_receipt(
            order, RECEIPT_WIDTH, clean_footer_message(sujo)
        )

        for character in content:
            if character == "\n":
                continue
            self.assertGreaterEqual(ord(character), 32, repr(character))


class TestTicketTests(unittest.TestCase):
    """A via que o botao "testar impressao" do painel produz.

    Ela existe para responder tres perguntas de uma vez, e cada teste aqui
    guarda uma delas.
    """

    def test_it_says_which_printer_and_which_sector(self):
        # Numa loja com tres pracas, uma bobina anonima nao diz qual botao a
        # produziu — e a pessoa em pe na impressora e quem confere o teste.
        content = build_test_ticket("Cozinha", "EPSON TM-T20", CREATED_AT)

        self.assertIn("Cozinha", content)
        self.assertIn("EPSON TM-T20", content)

    def test_it_exercises_the_accents(self):
        """A linha com acento e o unico jeito de o teste valer alguma coisa
        contra o par codepage/encoding trocado (armadilha 28): uma via sem
        acento passa com a configuracao errada."""
        content = build_test_ticket(None, None, CREATED_AT)

        self.assertIn("ÇÃÕÉÜ", content)
        self.assertIn("ção", content)

    def test_it_carries_the_shop_clock(self):
        content = build_test_ticket(None, None, CREATED_AT)

        self.assertIn("09/08/2026 14:32", content)

    def test_without_a_sector_or_printer_it_still_prints(self):
        """O teste disparado numa maquina recem-instalada, antes de existir
        setor nenhum, precisa sair do mesmo jeito."""
        content = build_test_ticket(None, None, CREATED_AT)

        self.assertIn("TESTE DE IMPRESSAO", content)

    def test_no_line_is_wider_than_the_production_width(self):
        """A via de teste sai em fonte dupla, onde cabe metade das colunas.
        Uma linha mais larga dobra sozinha na impressora e corta palavra no
        meio."""
        content = build_test_ticket("Praca Quente do Salao", "IMPRESSORA DE NOME ENORME", CREATED_AT)

        for line in content.splitlines():
            self.assertLessEqual(len(line), PRODUCTION_WIDTH, line)


class FormattingTests(unittest.TestCase):
    def test_money_uses_the_brazilian_separators(self):
        self.assertEqual(format_money(1234.5), "1.234,50")
        self.assertEqual(format_money(0), "0,00")

    def test_the_phone_is_printed_to_be_dialled(self):
        # `customer_phone_snapshot` guarda so digitos. Sem os parenteses e o
        # hifen, quem liga do balcao erra o DDD.
        self.assertEqual(format_phone("85999998888"), "(85) 99999-8888")
        self.assertEqual(format_phone("8532224444"), "(85) 3222-4444")
        self.assertEqual(format_phone("123"), "123")
        self.assertEqual(format_phone(None), "")

    def test_the_time_is_the_one_on_the_shop_clock(self):
        # created_at e UTC. Impresso cru, o pedido das 21h sairia com a data
        # do dia seguinte — justamente no horario de pico.
        self.assertEqual(format_datetime(CREATED_AT), "09/08/2026 14:32")


if __name__ == "__main__":
    unittest.main()
