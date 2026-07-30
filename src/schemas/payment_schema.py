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
