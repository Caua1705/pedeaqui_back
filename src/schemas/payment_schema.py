from pydantic import BaseModel


class StartPaymentResponse(BaseModel):
    """Resposta da criacao da cobranca.

    `checkout_url` e `qr_code` sao alternativos e dependem do gateway e do
    metodo: pix costuma vir com qr_code, cartao com url. O sandbox nao
    devolve nenhum dos dois — nao ha para onde mandar o cliente.
    """

    provider: str
    provider_payment_id: str
    payment_status: str
    checkout_url: str | None = None
    qr_code: str | None = None


class PaymentErrorDetail(BaseModel):
    """O `detail` quando a cobranca nao pode ser criada.

    Antes daqui todo erro do gateway saia como 503 com uma mensagem interna
    ("Mercado Pago com erro interno (status 500)") — e o frontend, sem ter
    como distinguir uma coisa da outra, mostrava "erro interno" para tudo.
    Sao situacoes diferentes para quem esta com o pedido fechado esperando o
    pix: o gateway fora do ar por um minuto pede "tentar de novo", o
    restaurante sem credencial cadastrada pede outra coisa.

    `retryable` e o campo que separa as duas: `true` significa que a MESMA
    chamada tem chance de funcionar daqui a pouco, `false` significa que
    insistir nao muda nada e o cliente precisa de outro caminho (falar com o
    restaurante, ou pagar na entrega).
    """

    # Identificador estavel para o frontend ligar a um texto proprio, sem
    # depender de comparar `message`. Ver PAYMENT_ERROR_* em payment_service.
    code: str
    # Pronta para ser mostrada ao cliente: curta, em portugues, e dizendo o
    # que fazer a seguir.
    message: str
    retryable: bool
    # Referencia do provedor quando ele deu alguma ("bad_request", "2062"),
    # para citar num chamado de suporte. E um codigo do catalogo deles,
    # nunca a mensagem crua — essa pode ecoar o e-mail de quem pagou e fica
    # so no log.
    provider_error_code: str | None = None


class PaymentWebhookResponse(BaseModel):
    """O que o gateway recebe de volta.

    Curto e sem dado do pedido: e uma resposta para maquina, e qualquer
    coisa a mais seria informacao entregue a quem so tem o endereco do
    webhook.
    """

    status: str
    reason: str | None = None
    payment_status: str | None = None
    order_id: str | None = None
