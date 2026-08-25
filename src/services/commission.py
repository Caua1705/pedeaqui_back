"""A comissao da plataforma depois que o pedido acabou.

O CALCULO mora em `OrderService._calculate_commission`, na criacao do pedido.
Aqui esta so o outro extremo: o que acontece com as tres colunas quando o
dinheiro volta para o cliente.

**Por que em modulo proprio e nao junto do calculo.** Quem reverte a comissao
e o lado do PAGAMENTO — `PaymentService.handle_webhook` e
`PaymentRefundService` —, e nenhum dos dois pode importar `OrderService`, que
puxa meio repositorio atras de si. Um modulo sem dependencia de service
nenhum e o que permite os dois escritores de `payment_status='refunded'`
usarem a MESMA funcao, que e a propriedade que importa: duas copias da regra
fariam o estorno pelo painel do Mercado Pago zerar a comissao e o estorno
automatico nao (ou o contrario), sem ninguem conseguir explicar a diferenca
olhando o extrato.
"""

from decimal import Decimal


ZERO_COMMISSION = Decimal("0.00")


def zero_commission_for_refund(order) -> None:
    """Zera a comissao de um pedido cujo dinheiro voltou por inteiro.

    **Decisao de 25/08/2026, e ela reverteu o que este repositorio dizia
    antes.** Ate entao as tres colunas ficavam congeladas em qualquer
    desfecho, com o argumento de que sao um REGISTRO do que foi acordado no
    dia do pedido, e nao um saldo. O argumento continua verdadeiro e perdeu
    para um mais forte: **cobrar comissao de venda que nao existiu e
    indefensavel.** O cliente recebeu tudo de volta; nao ha venda sobre a
    qual cobrar percentual nenhum.

    O extrato ja nao mostrava esse pedido — `billable_order_conditions`
    exclui `payment_status = 'refunded'` —, entao **na fatura nada muda**. O
    que muda e o registro parar de contradizer a fatura: um pedido estornado
    tinha `commission_amount = 9,00` gravado e cobranca zero, e quem lesse a
    coluna sem conhecer o filtro chegava ao numero errado.

    **`commission_percent` NAO e zerado, e a distincao nao e sutileza.** Ele
    nao e dinheiro: e a TAXA contratada, que continua sendo a mesma taxa
    daquele restaurante naquele dia. Zera-lo faria o pedido estornado parecer
    um contrato de 0%, e apagaria a unica prova de qual percentual valia
    quando o pedido foi feito — que e o que permite ao lojista conferir o
    extrato meses depois. Com base e valor em zero, a identidade que as tres
    colunas mantem continua valendo (`0 = 0 x percent / 100`), e a linha
    continua legivel: "a taxa era 10%, a base virou zero porque o dinheiro
    voltou".

    **So estorno TOTAL passa por aqui.** Estorno parcial mantem o pagamento
    em `approved`/`paid` e o pedido continua faturavel pelo valor cheio — e
    isso e decisao tomada, nao esquecimento; ver `docs/pagamentos-e-comissao.md`.

    Idempotente por construcao: zerar duas vezes da zero. O gateway reenvia a
    mesma notificacao ate receber 2xx, e a varredura de estorno pode passar
    de novo pelo mesmo pedido.
    """
    order.commission_base_amount = ZERO_COMMISSION
    order.commission_amount = ZERO_COMMISSION
