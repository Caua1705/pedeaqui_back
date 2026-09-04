from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.cashback_schema import CashbackTransactionsResponse
from src.schemas.order_review_schema import CustomerReviewItem
from src.schemas.common_schema import BaseResponse
from src.schemas.order_schema import OrderItemResponse
from src.utils.normalization import (
    is_valid_email,
    normalize_digits,
    normalize_email,
    normalize_text,
)


class CustomerAddressBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    label: str | None = None
    street: str | None = None
    number: str | None = None
    neighborhood: str | None = None
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)

    @field_validator("zipcode")
    @classmethod
    def normalize_zipcode(cls, value: str | None) -> str | None:
        return normalize_digits(value) if value else value


class CreateCustomerAddressRequest(CustomerAddressBase):
    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    is_default: bool = False


class UpdateCustomerAddressRequest(CustomerAddressBase):
    is_default: bool | None = None


class CustomerAddressResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    customer_id: UUID
    client_reference: str | None = None
    label: str | None = None
    street: str
    number: str
    neighborhood: str
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    is_default: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ImportCustomerAddressRequest(CustomerAddressBase):
    zipcode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("zipcode", "zip_code"),
    )
    client_reference: str | None = Field(default=None, min_length=1, max_length=100)
    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    is_default: bool = False

    @field_validator("client_reference", mode="before")
    @classmethod
    def normalize_client_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("street", "number", "neighborhood", mode="before")
    @classmethod
    def normalize_required_address_text(cls, value, info):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} deve ser informado")
        return value.strip()

    @field_validator("label", "complement", "reference", "city", "state", "zipcode", mode="before")
    @classmethod
    def normalize_optional_address_text(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def normalize_optional_address_coordinate(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value


class ImportCustomerAddressesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addresses: list[ImportCustomerAddressRequest] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_single_default(self):
        if sum(address.is_default for address in self.addresses) > 1:
            raise ValueError("Apenas um endereço pode ser definido como padrão")
        return self


class IgnoredImportedAddress(BaseModel):
    client_reference: str | None = None
    reason: str


class ImportCustomerAddressesResponse(BaseModel):
    created: list[CustomerAddressResponse] = Field(default_factory=list)
    existing: list[CustomerAddressResponse] = Field(default_factory=list)
    ignored: list[IgnoredImportedAddress] = Field(default_factory=list)

class CurrentCustomerResponse(BaseResponse):
    id: UUID
    name: str
    email: str
    phone: str
    # `cpf` saiu daqui na frente 5 (LGPD). Era o unico lugar da API que
    # devolvia o documento, e ele nao era usado para mais nada — a coluna foi
    # anulada pela revisao 0019. Publicar campo que hoje seria sempre nulo so
    # convidaria a tela a mostrar espaco vazio.
    birth_date: date
    email_verified: bool
    marketing_opt_in: bool
    # SE HA SENHA UTILIZAVEL. Falso na conta criada pelo "entrar com Google"
    # que nunca definiu uma.
    #
    # `false` muda DUAS telas: "alterar senha" vira "definir senha" (por
    # `/auth/forgot-password`, que manda codigo para o e-mail que o Google ja
    # verificou), e a EXCLUSAO DE CONTA — que exige a senha atual — precisa
    # avisar que a senha tem que ser definida antes. Ver o docstring de
    # `DELETE /customers/me`.
    #
    # A LISTA DE PROVEDORES LIGADOS NAO ESTA AQUI, e a ausencia e escolha:
    # ela exige consulta, e `get_me` e traducao pura do objeto que ja chegou —
    # e o que faz `GET /customers/me` nao ir ao banco uma segunda vez, e o que
    # `tests/test_colunas_em_desacordo.py` usa ao construir o service com
    # sessao nenhuma. Quem quiser a lista tem ela na exportacao da LGPD, que
    # ja consulta tudo. `password_set` sai do proprio `customer`, sem consulta.
    password_set: bool = True


class CustomerSocialIdentityItem(BaseResponse):
    """Uma conta de provedor ligada a esta pessoa, na exportacao de dados.

    `provider_user_id` entra, e a decisao merece a linha: e o `sub` do Google,
    um identificador estavel da pessoa DENTRO do provedor. Omiti-lo faria a
    exportacao do Art. 18, II descrever um dado que a plataforma guarda e nao
    mostra — e ele e da propria pessoa, devolvido so a ela, numa rota
    autenticada que nao aceita id de terceiro.
    """

    provider: str
    provider_user_id: str
    created_at: datetime
    last_login_at: datetime | None = None


class LinkedSocialAccountResponse(BaseResponse):
    """Uma conta de provedor conectada, para a TELA de contas conectadas.

    **Nao leva `provider_user_id`, e a ausencia e a diferenca para
    `CustomerSocialIdentityItem`.** O `sub` e o identificador da pessoa dentro
    do Google: ele pertence a exportacao da LGPD, que e um pedido explicito e
    baixado uma vez — nao a uma tela de configuracoes que abre sozinha e cujo
    corpo passa por log de proxy, cache de app e captura de tela.
    """

    provider: str
    linked_at: datetime
    last_login_at: datetime | None = None


class LinkGoogleAccountRequest(BaseModel):
    """Ligar o Google a uma conta que JA ESTA logada.

    `password` e obrigatoria no servico e opcional no schema pelo mesmo motivo
    de `DeleteCustomerAccountRequest`: a conta sem senha utilizavel nao tem o
    que preencher, e a resposta dela e um 400 que ensina o caminho — nao um
    422 de campo faltando.

    O par `id_token`/`nonce_token` e o mesmo de `POST /auth/google`: peca o
    nonce em `POST /auth/google/nonce` antes de abrir o botao.
    """

    id_token: str = Field(min_length=1, max_length=8192)
    nonce_token: str = Field(min_length=1)
    password: str | None = None


class UnlinkSocialAccountRequest(BaseModel):
    """Desconectar um provedor. A senha atual, pelo motivo de sempre:
    desconectar mexe em forma de entrar."""

    password: str | None = None


class UpdateCurrentCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    email: str
    phone: str
    birth_date: date
    # Opcional, e nao obrigatorio como os quatro acima, por duas razoes:
    #
    # 1. campo obrigatorio novo em request que o painel e o app ja mandam faz
    #    toda versao instalada passar a receber 422 ate ser atualizada
    #    (armadilha 7: campo com default e de graca, obrigatorio nao);
    # 2. ausente e "nao mexi no consentimento", que e diferente de `false`
    #    ("revoguei"). Sem a distincao, um cliente que editasse o telefone
    #    revogaria o proprio opt-in sem pedir.
    #
    # Era o unico jeito de revogar: o consentimento era coletado no cadastro
    # e o `extra="forbid"` acima impedia qualquer alteracao depois.
    marketing_opt_in: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        # NFC alem do strip: `customer_name_snapshot` e procurado com ILIKE
        # no painel, e ILIKE compara bytes — "Antônio" decomposto nao seria
        # encontrado por quem digita a forma composta.
        name = normalize_text(value)
        if not name:
            raise ValueError("name is required")
        return name

    @field_validator("email")
    @classmethod
    def validate_and_normalize_email(cls, value: str) -> str:
        email = normalize_email(value)
        if not is_valid_email(email):
            raise ValueError("invalid email")
        return email

    @field_validator("phone")
    @classmethod
    def validate_and_normalize_phone(cls, value: str) -> str:
        phone = normalize_digits(value)
        if len(phone) < 8:
            raise ValueError("invalid phone")
        return phone


class ChangeCustomerPasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class DeleteCustomerAccountRequest(BaseModel):
    """A prova de que quem pede e o dono da conta. UMA das duas.

    Corpo em `DELETE` e incomum mas legal. A alternativa — senha na
    querystring — a colocaria no log de todo proxy no caminho.

    **Qual dos dois campos mandar nao e escolha de quem chama: e da conta.**
    `GET /customers/me` responde `password_set`, e ele decide:

        password_set: true   -> `password`, como sempre foi
        password_set: false  -> `email_code`, pedido em
                                POST /customers/me/delete-code

    Os dois sao opcionais NO SCHEMA e obrigatorios no servico, e a assimetria
    e proposital: um `password: str` obrigatorio deixaria a conta sem senha
    sem forma de preencher o corpo, e um `Union` de dois schemas faria o app
    escolher o formato — que e exatamente a decisao que nao pode ser dele.
    Mandar o campo errado e **400** com a frase que diz qual mandar.

    O e-mail com o codigo diz EXCLUIR, com todas as letras: o mesmo codigo de
    seis digitos serve a tres pedidos neste sistema, e a caixa de entrada e o
    unico lugar onde a pessoa ve o que esta confirmando.
    """

    password: str | None = None
    email_code: str | None = Field(default=None, min_length=6, max_length=6)


class OrdersInFlightDetail(BaseModel):
    """O corpo do 409 quando ha pedido a caminho.

    Os numeros de pedido vao junto porque a recusa e TEMPORARIA: sem eles o
    app so consegue dizer "tente mais tarde", e a pessoa nao tem como saber o
    que esta segurando a exclusao dela.
    """

    message: str
    orders_in_flight: list[int]


class OrdersInFlightResponse(BaseModel):
    """O ENVELOPE, que e o que a rota devolve de verdade.

    `HTTPException` embrulha tudo em `detail`. Anunciar `OrdersInFlightDetail`
    na raiz publicaria no OpenAPI um formato que a rota nunca entrega, e o
    front escreveria o parser contra ele — foi o que aconteceu com o 502 do
    pagamento (armadilha 16).
    """

    detail: OrdersInFlightDetail


class CustomerOrderHistoryItem(BaseModel):
    id: UUID
    order_number: int
    restaurant_name: str
    branch_name: str
    status: str
    # O QUE O `status` SOZINHO NAO SEPARA, e o cliente precisa saber.
    #
    # `orders.status = 'pending'` e o mesmo valor para tres situacoes que pedem
    # coisas diferentes dele:
    #
    #     payment_status  o que aconteceu           o que ele faz
    #     ---------------------------------------------------------------
    #     paid            pago, esperando a loja    espera
    #     on_delivery     paga na entrega           espera
    #     failed          cobranca recusada         **tenta outro cartao**
    #     pending         nunca chegou a pagar      **finaliza o pagamento**
    #
    # As duas ultimas ele resolve SOZINHO, e sem este campo ele nao sabe que
    # pode — ficava esperando uma cozinha que nunca recebeu o pedido.
    #
    # Campo NOVO e opcional, e nao um status novo em `ORDER_STATUSES`: um valor
    # a mais naquele conjunto atravessaria o CHECK espelhado (armadilha 15), a
    # maquina de estados, o faturamento e as quatro portas de escrita — para
    # dizer o que a coluna ao lado ja diz. E `OrderDetailResponse`, na rota do
    # link de acompanhamento, JA entrega `payment_status`: as duas superficies
    # do mesmo pedido e que discordavam sobre o que da para saber dele.
    payment_status: str | None = None
    order_type: str
    subtotal: float
    delivery_fee: float
    service_fee: float
    coupon_code: str | None = None
    coupon_discount_amount: Decimal = Decimal("0.00")
    cashback_redeemed_amount: Decimal = Decimal("0.00")
    discount_total: Decimal = Decimal("0.00")
    total: float
    created_at: datetime | None = None
    items: list[OrderItemResponse]


class CustomerDataExportResponse(BaseModel):
    """Tudo que a plataforma guarda sobre quem pediu, num pacote so.

    Existe para o direito de acesso e portabilidade (LGPD, Art. 18, II e V).
    As tres listas ja saiam por rotas proprias (`/me`, `/me/orders`,
    `/me/addresses`) — o que faltava era o pacote, e por isso esta resposta e
    montagem do que ja existe, e nao consulta nova.

    O escopo e sempre o dono do token. Nao ha parametro de cliente aqui, nem
    deve haver: uma rota de exportacao que aceitasse id viraria a maneira mais
    conveniente de baixar a base inteira.

    O que NAO entra, de proposito:

    - `password_hash`, que nao e dado do titular e sim credencial;
    - o pedido de convidado feito com o mesmo telefone. Ele nao esta ligado a
      conta nenhuma (e o que a frente 5 registrou como buraco 2.6), entao nao
      ha como saber que e da mesma pessoa sem passar a casar por telefone —
      e casar por telefone transformaria esta rota num jeito de ler o pedido
      de quem por acaso repetiu um numero.
    """

    exported_at: datetime
    profile: CurrentCustomerResponse
    addresses: list[CustomerAddressResponse]
    orders: list[CustomerOrderHistoryItem]
    cashback: CashbackTransactionsResponse
    # As avaliacoes que a pessoa escreveu. Entram porque sao dado DELA: sem
    # isto o direito de acesso ficaria incompleto justamente no campo de
    # texto livre, que e o que a exclusao de conta depois apaga.
    reviews: list[CustomerReviewItem]
    # As contas de provedor ligadas. Entram pelo mesmo motivo das avaliacoes:
    # sao linhas que a plataforma guarda sobre a pessoa, e que a exclusao de
    # conta depois apaga (`_delete_social_identities`). Uma tabela que sai na
    # exclusao e nao aparece no acesso e uma metade so do mesmo direito.
    social_identities: list[CustomerSocialIdentityItem]
