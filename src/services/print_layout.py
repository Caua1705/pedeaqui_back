"""Desenho das comandas em texto de largura fixa.

Este modulo e a razao de existir da rota de impressao. O agente que roda na
loja e burro DE PROPOSITO: ele recebe texto ja quebrado na largura certa e
manda para a impressora. Toda a regra — o que entra em cada via, como o
adicional aparece, quanto e o total — mora aqui, num lugar so, sem banco e
sem HTTP, e por isso da para testar linha a linha.

Se a formatacao vivesse no agente, cada loja com uma versao instalada
imprimiria uma comanda diferente, e corrigir um bug de layout viraria uma
operacao de campo em vez de um deploy.

Tres decisoes valem para o arquivo inteiro:

1. **A entrada e o `OrderDetailResponse`**, o mesmo objeto que o painel
   recebe em `GET /admin/orders/{id}`. Nao e economia de codigo: e a
   garantia de que a comanda mostra exatamente o que o lojista ve na tela,
   inclusive os adicionais ja agrupados por grupo de complemento. Duas
   leituras do pedido seriam duas chances de divergir.

2. **Rotulo fixo nao leva acento.** O nome do cliente, o endereco e o nome
   do produto saem como estao no banco, com acento e tudo; ja "ENDERECO",
   "TAXA DE SERVICO" e afins ficam em ASCII porque a codepage da impressora
   termica varia de modelo para modelo e um rotulo virando "ENDEREO" e um
   defeito que aparece em toda comanda, nao so numa.

3. **Fonte grande diminui a largura.** A via de producao e impressa em
   fonte dupla, e nela cabe METADE das colunas. Por isso ela e montada com
   `PRODUCTION_WIDTH`, e nao com `RECEIPT_WIDTH` — texto quebrado em 48
   colunas sairia dobrando linha sozinho na impressora, e "1x FILE COM
   FRITAS" viraria duas linhas cortadas no meio da palavra.
"""

import textwrap
from datetime import datetime
from zoneinfo import ZoneInfo

from src.core.constants import PLATFORM_TIMEZONE
from src.schemas.order_schema import (
    OrderDetailResponse,
    OrderItemOptionGroupResponse,
    OrderItemResponse,
)


# Largura da bobina de 80mm em fonte normal. E o formato quase universal das
# impressoras termicas de balcao; a de 58mm usa 32 e nao esta em uso aqui.
RECEIPT_WIDTH = 48

# A via de producao sai em fonte dupla para ser lida de longe, no calor, por
# quem esta com as maos ocupadas. Fonte dupla = metade das colunas.
PRODUCTION_WIDTH = RECEIPT_WIDTH // 2

RECEIPT_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)

ORDER_TYPE_LABELS = {
    "delivery": "ENTREGA",
    "pickup": "RETIRADA",
}

# Nomes das formas de pagamento de PAYMENT_METHODS em portugues. Sem acento
# pelo motivo 2 do cabecalho: sao rotulo fixo, nao dado do cliente.
PAYMENT_METHOD_LABELS = {
    "pix": "PIX",
    "credit_card": "Cartao de credito",
    "debit_card": "Cartao de debito",
    "cash": "Dinheiro",
    "voucher": "Voucher",
    "meal_voucher": "Vale-refeicao",
    "other": "Outro",
}

# Situacao do dinheiro, impressa embaixo da forma de pagamento. E o que o
# entregador precisa saber antes de sair: "PAGO" nao cobra nada na porta,
# "A RECEBER" cobra.
PAYMENT_STATUS_LABELS = {
    "paid": "PAGO",
    "on_delivery": "A RECEBER NA ENTREGA",
    "pending": "AGUARDANDO PAGAMENTO",
    "failed": "PAGAMENTO RECUSADO",
    "refunded": "ESTORNADO",
}


def format_money(value: float) -> str:
    """Valor em reais no formato brasileiro, sem o "R$".

    O simbolo fica de fora porque quem monta a linha decide se ele cabe: em
    24 colunas, "R$" ocupa espaco que o nome do produto precisa mais.
    """
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def format_phone(digits: str | None) -> str:
    """Telefone gravado so com digitos, devolvido legivel.

    `customer_phone_snapshot` guarda apenas numeros (ver
    CustomerInput.validate_and_normalize_phone). Na comanda ele existe para
    ser DISCADO por uma pessoa que vai olhar uma vez e digitar — sem os
    parenteses e o hifen, quem liga erra o DDD.
    """
    if not digits:
        return ""
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return digits


def format_datetime(moment: datetime | None) -> str:
    """Data e hora no fuso da operacao.

    `orders.created_at` e gravado em UTC. Imprimir o valor cru colocaria o
    pedido das 21h como 00h do dia seguinte, e a comanda passaria a mentir
    sobre a data justamente no horario de pico.
    """
    if moment is None:
        return ""
    return moment.astimezone(RECEIPT_TIMEZONE).strftime("%d/%m/%Y %H:%M")


def format_time(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(RECEIPT_TIMEZONE).strftime("%H:%M")


def rule(width: int, char: str = "-") -> str:
    return char * width


def center(text: str, width: int) -> list[str]:
    return [line.center(width) for line in wrap(text, width)]


def wrap(text: str, width: int, indent: str = "", hanging: str | None = None) -> list[str]:
    """Quebra o texto na largura, sem nunca estourar a coluna.

    `break_long_words` fica ligado (o padrao) de proposito: um nome de
    produto sem espaco e maior que a largura precisa ser cortado, porque a
    alternativa e a impressora cortar sozinha no lugar que ela quiser.
    """
    if not text:
        return []
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=hanging if hanging is not None else indent,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [indent.rstrip()]


def row(left: str, right: str, width: int) -> list[str]:
    """Rotulo a esquerda e valor a direita, na mesma linha.

    Quando os dois nao cabem juntos, o rotulo quebra em varias linhas e o
    valor vai sozinho na ultima, alinhado a direita. Truncar o rotulo nao e
    opcao: "Refrigerante 2L Zero" virando "Refrigeran" e como a conferencia
    do pedido comeca a dar errado.
    """
    if not right:
        return wrap(left, width)
    space = width - len(right) - 1
    if space < 1:
        return wrap(left, width) + [right.rjust(width)]

    # `or [""]` para o rotulo vazio: sem isso, o valor nao teria linha em que
    # ser escrito e a conta sairia da via sem o numero.
    lines = wrap(left, space) or [""]
    last = lines[-1]
    if len(last) <= space:
        lines[-1] = last.ljust(space + 1) + right
        return lines
    return lines + [right.rjust(width)]


def field(label: str, value: str, width: int, label_width: int) -> list[str]:
    """Linha "ROTULO: valor" com o valor alinhado numa coluna fixa.

    A coluna fixa e o que faz o bloco do cliente ser lido de relance: com
    rotulos de tamanhos diferentes, o olho tem que procurar onde comeca cada
    dado em cada linha.
    """
    prefix = f"{label}:".ljust(label_width)
    # `value or "-"` porque um valor vazio faria a linha inteira sumir, e
    # rotulo ausente na comanda se le como "esqueceram de imprimir", nao
    # como "nao ha o que imprimir".
    return wrap(value or "-", width, indent=prefix, hanging=" " * label_width)


def build_customer_receipt(
    order: OrderDetailResponse,
    width: int = RECEIPT_WIDTH,
) -> str:
    """A via do cliente: quem pediu, o que pediu e quanto deu.

    E a unica via com dinheiro. A de producao nao tem preco nenhum — quem
    esta na chapa nao precisa saber quanto custa e, se souber, tem mais uma
    linha para ler no meio do aperto.
    """
    # Coluna onde o valor comeca em todo o bloco de cabecalho. Sai do rotulo
    # MAIS LONGO ("PAGAMENTO:") mais o espaco: calculado no menor rotulo, o
    # maior encostaria no proprio valor ("PAGAMENTO:Dinheiro").
    label_width = len("PAGAMENTO: ")
    lines: list[str] = [rule(width, "=")]
    lines += center(f"PEDIDO #{order.order_number}", width)
    lines += center(format_datetime(order.created_at), width)
    lines += center(ORDER_TYPE_LABELS.get(order.order_type, order.order_type.upper()), width)
    lines.append(rule(width, "="))

    lines += field("CLIENTE", order.customer_name_snapshot, width, label_width)
    lines += field("TELEFONE", format_phone(order.customer_phone_snapshot), width, label_width)
    lines += _address_block(order, width, label_width)

    lines.append(rule(width))
    lines.append("ITENS")
    lines.append(rule(width))
    for item in order.items:
        lines += _customer_item_block(item, width)

    lines.append(rule(width))
    lines += _totals_block(order, width)

    lines.append(rule(width))
    lines += field(
        "PAGAMENTO",
        PAYMENT_METHOD_LABELS.get(order.payment_method or "", order.payment_method or "-"),
        width,
        label_width,
    )
    payment_status = PAYMENT_STATUS_LABELS.get(order.payment_status, order.payment_status)
    lines += wrap(payment_status, width, indent=" " * label_width)

    if order.notes:
        lines.append(rule(width))
        lines.append("OBS. DO PEDIDO:")
        lines += wrap(order.notes, width)

    lines.append(rule(width, "="))
    return "\n".join(lines)


def build_production_ticket(
    order: OrderDetailResponse,
    sector_name: str,
    items: list[OrderItemResponse],
    width: int = PRODUCTION_WIDTH,
) -> str:
    """A comanda de uma praca: so os itens dela, sem preco.

    O nome do setor vem no topo porque a mesma bobina pode ser conferida
    depois, longe da impressora que a cuspiu.
    """
    lines: list[str] = [rule(width, "=")]
    lines += wrap(f"PEDIDO #{order.order_number}", width)
    lines += wrap(sector_name.upper(), width)
    header = format_time(order.created_at)
    order_type = ORDER_TYPE_LABELS.get(order.order_type, order.order_type.upper())
    lines += wrap(f"{header} {order_type}".strip(), width)
    lines.append(rule(width, "="))

    for position, item in enumerate(items):
        if position:
            lines.append(rule(width))
        lines += _production_item_block(item, width)

    lines.append(rule(width, "="))
    return "\n".join(lines)


def _address_block(order: OrderDetailResponse, width: int, label_width: int) -> list[str]:
    """Endereco da entrega, uma informacao por linha.

    Pedido de retirada nao tem endereco para imprimir; a via diz isso com
    todas as letras em vez de deixar um campo vazio, que na impressao se
    confunde com dado que faltou.
    """
    if order.order_type != "delivery":
        return field("ENTREGA", "RETIRADA NO BALCAO", width, label_width)

    street = ", ".join(part for part in (order.address_street, order.address_number) if part)
    lines = field("ENDERECO", street or "-", width, label_width)
    padding = " " * label_width
    for value in (
        order.address_neighborhood,
        f"Compl.: {order.address_complement}" if order.address_complement else None,
        f"Ref.: {order.address_reference}" if order.address_reference else None,
        _city_line(order),
    ):
        if value:
            lines += wrap(value, width, indent=padding)
    return lines


def _city_line(order: OrderDetailResponse) -> str | None:
    city = "/".join(part for part in (order.address_city, order.address_state) if part)
    if order.address_zipcode:
        return f"{city} - CEP {order.address_zipcode}".strip(" -")
    return city or None


def _customer_item_block(item: OrderItemResponse, width: int) -> list[str]:
    lines = row(f"{item.quantity}x {item.product_name_snapshot}", format_money(item.total), width)
    lines += _option_lines(item.option_groups, width, with_price=True)
    if item.observation:
        lines += wrap(f"Obs.: {item.observation}", width, indent="  ", hanging="        ")
    return lines


def _production_item_block(item: OrderItemResponse, width: int) -> list[str]:
    # Nome em caixa alta: em fonte dupla, com a bobina na mao e o fogao
    # ligado, maiuscula e o que se le de relance.
    lines = wrap(f"{item.quantity}x {item.product_name_snapshot.upper()}", width, hanging="   ")
    lines += _option_lines(item.option_groups, width, with_price=False)
    if item.observation:
        lines += wrap(f"OBS.: {item.observation}", width, indent="  ", hanging="     ")
    return lines


def _option_lines(
    groups: list[OrderItemOptionGroupResponse],
    width: int,
    with_price: bool,
) -> list[str]:
    """Os adicionais do item, um grupo por linha.

    O nome do GRUPO e impresso junto e nao e enfeite: "Acompanhamento:
    espaguete" e uma TROCA (o arroz nao vai) e "Adicional: espaguete" e uma
    porcao a mais. Sem o grupo as duas chegam na cozinha como a mesma linha,
    e sai pedido errado — foi para isso que o detalhe do pedido passou a
    agrupar por grupo de complemento.

    O preco so aparece na via do cliente. Ele ja esta embutido no
    `unit_price_snapshot` do item; vai aqui por conferencia, para o cliente
    que reclama do valor ver de onde saiu.
    """
    lines: list[str] = []
    for group in groups:
        chosen = []
        for option in group.options:
            price = option.additional_price_snapshot
            if with_price and price:
                chosen.append(f"{option.option_name_snapshot} (+{format_money(price)})")
            else:
                chosen.append(option.option_name_snapshot)
        text = f"> {group.option_group_name_snapshot}: {', '.join(chosen)}"
        lines += wrap(text, width, indent="  ", hanging="    ")
    return lines


def _totals_block(order: OrderDetailResponse, width: int) -> list[str]:
    """Subtotal, taxas, descontos e total.

    Linha de valor zerado nao e impressa (com uma excecao: a taxa de
    entrega de um pedido de entrega, que sai como 0,00 para deixar claro que
    a entrega e gratis, e nao que a taxa foi esquecida). Cada linha inutil
    na via e um centimetro de bobina e uma linha a mais para o cliente
    conferir.
    """
    lines = row("Subtotal", format_money(order.subtotal), width)
    if order.delivery_fee or order.order_type == "delivery":
        lines += row("Taxa de entrega", format_money(order.delivery_fee), width)
    if order.service_fee:
        lines += row("Taxa de servico", format_money(order.service_fee), width)
    if order.coupon_discount_amount:
        label = f"Cupom {order.coupon_code}" if order.coupon_code else "Desconto"
        lines += row(label, f"-{format_money(float(order.coupon_discount_amount))}", width)
    if order.cashback_redeemed_amount:
        lines += row(
            "Cashback",
            f"-{format_money(float(order.cashback_redeemed_amount))}",
            width,
        )
    lines.append(rule(width))
    lines += row("TOTAL", format_money(order.total), width)
    return lines
