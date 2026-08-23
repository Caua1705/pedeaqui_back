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
    # Cartao pedido em pedido de convidado. O front precisa mandar o cliente
    # fazer login (ou pagar por pix) — nao e falha de gateway nenhuma.
    LOGIN_REQUIRED = "login_required"
    # Cartao pedido sem o token gerado no navegador. Erro de integracao do
    # front, nao do cliente.
    CARD_TOKEN_REQUIRED = "card_token_required"


class CardPaymentPayload(BaseModel):
    """O que o navegador produziu com o SDK do Mercado Pago.

    **Nao existe campo para numero, CVV ou validade do cartao, e nunca deve
    existir.** Esses dados vao do navegador direto para o gateway, que
    devolve o `token` — e o token e a unica coisa que este backend enxerga.
    Acrescentar aqui qualquer parte do cartao muda o perimetro de PCI do
    projeto inteiro.
    """

    token: str = Field(
        description="Token de uso unico gerado pelo SDK no navegador.",
        min_length=1,
        max_length=256,
    )
    payment_method_id: str = Field(
        description=(
            "Bandeira que o SDK resolveu ('visa', 'master', 'elo'). NAO e o "
            "`payment_method` do pedido ('credit_card') — sao vocabularios "
            "diferentes, e o gateway quer o dele."
        ),
        min_length=1,
        max_length=64,
        examples=["master"],
    )
    issuer_id: str | None = Field(
        default=None,
        description="Emissor, quando o SDK o resolve.",
        max_length=64,
    )
    payer_document_type: str | None = Field(
        default=None,
        description="Tipo do documento do portador, normalmente 'CPF'.",
        max_length=16,
        examples=["CPF"],
    )
    payer_document_number: str | None = Field(
        default=None,
        description=(
            "Documento do portador. Atravessa para o gateway e NAO e gravado "
            "em lugar nenhum daqui."
        ),
        max_length=32,
    )


class PaymentConfigResponse(BaseModel):
    """O que o navegador precisa para tokenizar um cartao.

    **`public_key` e publica por desenho** — o proprio Mercado Pago manda
    expor no frontend, e e por isso que ela e a unica coluna de
    `restaurant_payment_credentials` guardada em texto puro. O `access_token`
    e o `webhook_secret` da mesma linha sao cifrados e nao passam nem perto
    desta resposta.

    `card_enabled` falso significa "nao ofereca cartao nesta tela": ou o
    restaurante nao tem credencial cadastrada para o ambiente ativo, ou o
    provider ativo nao processa cartao (o sandbox nao processa, de
    proposito). Sem esse campo o front so descobriria isso no 503, com o
    cliente ja tendo digitado o cartao.
    """

    provider: str
    public_key: str | None = Field(
        default=None,
        description=(
            "Chave publica do restaurante no gateway, para inicializar o SDK. "
            "Nula quando nao ha credencial cadastrada."
        ),
    )
    card_enabled: bool = Field(
        description="Se o front deve oferecer cartao nesta tela.",
    )


class StartPaymentRequest(BaseModel):
    """Corpo da criacao da cobranca.

    OPCIONAL, e isso e o contrato: pix continua sendo um POST sem corpo, como
    era antes do cartao existir, entao quem ja integrou nao muda nada. `card`
    so e exigido quando a forma de pagamento do pedido e cartao.
    """

    card: CardPaymentPayload | None = None


class StartPaymentResponse(BaseModel):
    """Resposta da criacao da cobranca.

    `checkout_url` e `qr_code` sao do pix (o "copia e cola" e a pagina
    hospedada). O sandbox nao devolve nenhum dos dois — nao ha para onde
    mandar o cliente. Cartao nao devolve nenhum dos dois tampouco: nao ha
    para onde ir, o desfecho ja esta em `payment_status`.

    **`payment_status` e o campo que muda de significado por metodo**, e o
    front precisa saber disso:

        pix       sempre "pending" — o veredito vem por webhook depois
        cartao    o VEREDITO, ja: "paid", "failed" ou "in_review"
    """

    provider: str
    provider_payment_id: str
    payment_status: str = Field(
        description=(
            "Estado do pagamento. No pix e sempre 'pending'; no cartao ja e o "
            "desfecho ('paid', 'failed' ou 'in_review')."
        ),
    )
    checkout_url: str | None = None
    qr_code: str | None = None
    status_detail: str | None = Field(
        default=None,
        description=(
            "Motivo cru do gateway, so no cartao. Distingue recusas que pedem "
            "coisas diferentes do cliente — 'cc_rejected_insufficient_amount' "
            "(tentar outro cartao) de 'cc_rejected_bad_filled_security_code' "
            "(redigitar o CVV)."
        ),
        examples=["cc_rejected_insufficient_amount"],
    )


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
