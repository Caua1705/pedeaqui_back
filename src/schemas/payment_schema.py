from enum import Enum

from pydantic import BaseModel, Field


class PaymentErrorCode(str, Enum):
    """Os desfechos possiveis de uma cobranca que nao pode ser criada.

    Enum e nao `str` solto para o valor sair no /openapi.json: o frontend
    precisa da LISTA para escrever um texto proprio por caso, e nao so o
    `retryable` para decidir entre "tentar de novo" e "nao adianta". A lista
    tambem e a fonte unica dos codigos — PaymentService importa daqui.
    """

    # Instabilidade passageira do gateway (timeout, rede, 5xx deles). O
    # unico caso em que repetir a MESMA chamada tem chance de funcionar.
    GATEWAY_UNAVAILABLE = "gateway_unavailable"
    # Pagamento online indisponivel para ESTE restaurante: sem credencial
    # cadastrada, credencial recusada, metodo nao suportado. Insistir nao
    # muda nada — quem resolve e o lojista.
    PAYMENT_UNAVAILABLE = "payment_unavailable"
    # O gateway entendeu e RECUSOU a cobranca (dado invalido, conflito de
    # idempotencia). Tambem nao adianta insistir com a mesma cobranca.
    PAYMENT_REJECTED = "payment_rejected"


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

    code: PaymentErrorCode = Field(
        description=(
            "Identificador estavel do desfecho, para o frontend ligar a um "
            "texto proprio sem comparar `message`."
        ),
    )
    message: str = Field(
        description=(
            "Pronta para ser mostrada ao cliente: curta, em portugues, e "
            "dizendo o que fazer a seguir."
        ),
        examples=["Não foi possível gerar o pagamento agora. Tente de novo em alguns instantes."],
    )
    retryable: bool = Field(
        description=(
            "true = repetir a MESMA chamada daqui a pouco tem chance de "
            "funcionar. false = insistir nao muda nada."
        ),
    )
    provider_error_code: str | None = Field(
        default=None,
        description=(
            "Referencia do provedor quando ele deu alguma, para citar num "
            "chamado de suporte. E um codigo do catalogo deles "
            "('bad_request', '2062'), nunca a mensagem crua — essa pode "
            "ecoar o e-mail de quem pagou e fica so no log."
        ),
        examples=["2062"],
    )


class PaymentErrorResponse(BaseModel):
    """O CORPO INTEIRO do erro, com o envelope `detail` do FastAPI.

    Existe so para o /openapi.json publicar a forma certa. Declarar
    PaymentErrorDetail direto como `model` da resposta anunciava
    `{code, message, ...}` na raiz, mas HTTPException entrega
    `{"detail": {code, message, ...}}` — o frontend escreveria o parser
    contra um formato que a rota nunca devolve.
    """

    detail: PaymentErrorDetail


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
