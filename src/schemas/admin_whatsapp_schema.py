"""O canal de WhatsApp como o PAINEL o ve.

## O que nunca sai daqui

**O token, e nem numa forma parcial.** Ele e credencial da Business Manager do
lojista, cifrada com Fernet em `whatsapp_channels.access_token_encrypted`.
Nenhum campo deste modulo o carrega, e nao ha rota que o devolva — a mesma
regra do access token do Mercado Pago e da senha temporaria do lojista.

**O `waba_id` sai MASCARADO.** Ele nao e segredo — e um identificador de conta
que o proprio lojista ve no painel da Meta —, mas publica-lo no OpenAPI do
painel o transformaria em campo de tela, e campo de tela vira campo editavel.
Os quatro ultimos digitos respondem a unica pergunta que a tela precisa fazer
("e a conta certa?") sem que ele viaje inteiro.

## Por que a listagem tem DUAS listas

`channels` sao as linhas que existem. `branches` e a pergunta que o dono de
fato faz, e que nenhuma lista de canais responde: **"por qual numero esta loja
fala?"**

Sem a segunda, uma filial sem linha propria aparece como "sem WhatsApp" — e
ela pode estar herdando o numero do restaurante e funcionando perfeitamente. O
dono desligaria a campanha achando que ela nunca esteve no ar.

E ela e a mesma forma do `source` do cashback (`AdminCashbackRuleView`), de
proposito: as duas telas respondem a mesma pergunta de heranca, e duas formas
diferentes para a mesma pergunta e o que faz o painel parecer dois sistemas.
"""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class WhatsAppChannelStatus(str, Enum):
    """Em que estado esta o numero.

    `str, Enum` e nao literal solto para a LISTA sair no `/openapi.json`
    (armadilha 16): o painel precisa distinguir os tres, e o unico jeito de
    ele saber quais existem e o documento.

    Os tres pedem acoes diferentes, e e por isso que sao tres:

        connected             nada a fazer
        disabled              NOS desligamos. Religar e conosco
        disconnected_by_meta  o LOJISTA tirou o acesso pelo aplicativo dele.
                              Religar exige ele reconectar na Meta primeiro —
                              nao ha botao nosso que resolva
    """

    CONNECTED = "connected"
    DISABLED = "disabled"
    DISCONNECTED_BY_META = "disconnected_by_meta"


class WhatsAppBranchSource(str, Enum):
    """De onde vem o numero desta filial.

    `NONE` e "esta loja nao fala por numero nenhum" — e nao "nunca conectou".
    A distincao entre nunca-conectou e conectou-e-caiu esta no `status` do
    canal: sem canal nenhum, `channel_id` e nulo e `source` e `NONE`; com canal
    caido, `channel_id` aponta para a linha e o `status` dela diz o que houve.
    """

    BRANCH = "branch"
    RESTAURANT = "restaurant"
    NONE = "none"


class AdminWhatsAppChannelView(BaseModel):
    """Uma linha de `whatsapp_channels`, sem nada que seja credencial."""

    id: uuid.UUID
    # NULO e a linha do RESTAURANTE — a queda que toda filial sem numero
    # proprio herda. Nao e "filial desconhecida".
    branch_id: uuid.UUID | None = None
    branch_name: str | None = None
    # O numero legivel, como a Meta o devolve: "+55 85 99999-0000". E o que o
    # lojista reconhece; o `phone_number_id` nao diz nada a ele.
    display_phone_number: str
    phone_number_id: str
    waba_id_masked: str
    status: WhatsAppChannelStatus
    # O QUE ACONTECEU e O QUE FAZER, escritos aqui e nao na tela.
    #
    # O `status` sozinho nao e mensagem — e a mesma regra do
    # `provider_error_code` do pagamento (armadilha 49): o codigo serve para o
    # painel DECIDIR, e a frase serve para a pessoa LER. Deixar a tela montar
    # a frase a partir do enum e deixa-la adivinhar, e no dia em que entrar um
    # quarto estado ela cai no `default` do `switch` — que e onde a mensagem
    # errada mora.
    #
    # `status_action` e NULO quando nao ha o que fazer, e isso e informacao:
    # nulo quer dizer "esta certo", e nao "nao sei o que dizer".
    status_label: str
    status_action: str | None = None
    # Quando o canal foi cadastrado. Reconectar reescreve, porque o que a tela
    # pergunta e "desde quando este numero esta no ar", e um numero religado
    # depois de uma semana fora nao esta no ar desde a primeira vez.
    connected_at: datetime
    # So preenchidos quando a META desconectou. O `reason` e o texto dela
    # (`PARTNER_REMOVED`), repassado cru de proposito: e o que se cita num
    # chamado com o suporte deles.
    disconnected_at: datetime | None = None
    disconnect_reason: str | None = None


class AdminWhatsAppBranchView(BaseModel):
    """Por qual numero ESTA filial fala, agora."""

    branch_id: uuid.UUID
    branch_name: str
    source: WhatsAppBranchSource
    # A FRASE da faixa, escrita aqui e nao na tela — o mesmo que
    # `status_label` faz com o estado do canal, pelo mesmo motivo (armadilha
    # 49): o enum serve para o painel DECIDIR, a frase serve para a pessoa
    # LER.
    #
    # E aqui ela e o ponto inteiro da tela. `source: "restaurant"` deixado
    # para a tela traduzir vira "sem WhatsApp" no primeiro `switch` que
    # esquecer o terceiro valor — e a filial que herda o numero do
    # restaurante esta COBERTA. O dono com duas lojas e um numero so
    # desligaria a campanha achando que ela nunca esteve no ar, que e
    # exatamente o engano que a lista `branches` existe para desfazer.
    #
    # `source` sozinho tambem nao basta para escrever a frase: numero proprio
    # NO AR e numero proprio FORA DO AR sao o mesmo `source` e faixas
    # opostas. Quem separa os dois e `can_send`, e por isso a frase sai do
    # par — nunca do enum sozinho.
    source_label: str
    channel_id: uuid.UUID | None = None
    display_phone_number: str | None = None
    # A resposta de "um aviso sairia por esta loja neste minuto?". Sai da MESMA
    # consulta que o envio usa (`resolve_for_branch`), e nao de um `if` na
    # tela: duas formas da mesma pergunta divergem no dia em que alguem mexe
    # numa (armadilha 54), e aqui divergir significa a tela dizer que esta tudo
    # certo enquanto nenhum cliente e avisado.
    can_send: bool


class AdminWhatsAppChannelsResponse(BaseModel):
    """As linhas que existem, e a heranca resolvida loja a loja."""

    channels: list[AdminWhatsAppChannelView]
    branches: list[AdminWhatsAppBranchView]


class AdminWhatsAppChannelCreate(BaseModel):
    """Conectar um numero.

    `branch_id` nulo e deliberado e significa **a linha do restaurante**: o
    numero que toda filial sem o seu proprio herda. Restaurante de uma loja so
    usa essa forma.

    ## As descricoes dizem DE ONDE sai cada valor, e isso e a tela

    Quem cola os quatro valores e quem conecta o restaurante na mao —
    enquanto nao formos Tech Provider, nao ha Embedded Signup e nao ha de onde
    eles virem sozinhos. Entao o painel da Meta tem que estar escrito onde a
    pessoa esta olhando na hora de colar, e nao num documento que ela vai
    procurar tres meses depois.

    O lugar certo para isso e o `description` de cada campo: o painel consome
    o `/openapi.json` (armadilha 16), entao o que esta aqui chega ao
    formulario. Escrito no docstring da rota, nao chegaria.

    **A do `access_token` e a unica que fala do caminho ERRADO**, e ela e a
    linha mais importante das quatro: o token que a tela de Configuracao da
    API oferece primeiro, ja preenchido e com botao de copiar, e temporario.
    Com ele os avisos funcionam na demonstracao e param sozinhos no dia
    seguinte — sem nada errado deste lado para achar.
    """

    branch_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "A filial deste número. VAZIO significa o número do restaurante: "
            "toda filial que não tiver o seu próprio passa a falar por ele."
        ),
    )
    waba_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Meta → WhatsApp → Configuração da API, com o número escolhido no "
            "seletor: o campo “ID da conta do WhatsApp Business”."
        ),
    )
    phone_number_id: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Mesma tela do waba_id (WhatsApp → Configuração da API): o campo "
            "“ID do número de telefone”. Não é o número em si."
        ),
    )
    display_phone_number: str = Field(
        min_length=1,
        max_length=32,
        description=(
            "O número como se lê, na mesma tela: “+55 85 99999-0000”. É só "
            "para a tela — quem roteia o webhook é o phone_number_id."
        ),
    )
    # Vai no CORPO e nunca no path nem na querystring: querystring aparece no
    # log do proxy e no historico do navegador, e isto e credencial do lojista.
    access_token: str = Field(
        min_length=1,
        description=(
            "Token PERMANENTE: Configurações do Negócio → Usuários → Usuários "
            "do sistema → o usuário com o app e o WABA atribuídos → Gerar novo "
            "token, validade “Nunca expira”, com whatsapp_business_messaging e "
            "whatsapp_business_management marcadas. NÃO use o token da tela de "
            "Configuração da API: ele vence em 24h e os avisos param sozinhos "
            "no dia seguinte."
        ),
    )
