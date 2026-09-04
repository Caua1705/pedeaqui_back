"""O canal de WhatsApp no painel: listar, conectar, desconectar.

## Os papeis, e por que conectar e SOMENTE_DONO

Conectar um canal e colar no nosso banco uma **credencial da Business Manager
do lojista** — o mesmo tipo de segredo que o access token do Mercado Pago, cujo
cadastro nunca teve tela e so existe por script. E o que ela habilita nao e uma
tela: e a plataforma **mandando mensagem no WhatsApp da loja**, para o telefone
de cliente, em nome dele.

Desconectar e SOMENTE_DONO pelo outro lado da mesma moeda: com o canal fora, o
cliente para de ser avisado e **nada quebra** — nenhum erro, nenhuma tela
vermelha, so pedido seguindo em silencio. Estrago silencioso pede a senha que
menos circula.

LER e `GERENCIA`, o mesmo do cashback e dos cupons, e nao `PESSOAS`. A resposta
carrega o numero comercial e o `phone_number_id` de cada loja da rede, e a
senha do balcao e a que mais circula. O gerente precisa porque e ele quem
responde ao cliente que diz nao ter recebido o aviso.

## Nao ha rota que devolva o token

Nem parcial, nem mascarado. Ele entra por `POST` e a partir dai so existe
cifrado em `whatsapp_channels.access_token_encrypted`. Segunda via e conectar
de novo com um token novo, que e o mesmo desenho do acesso do entregador e da
senha temporaria do lojista.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    SOMENTE_DONO,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_whatsapp_schema import (
    AdminWhatsAppChannelCreate,
    AdminWhatsAppChannelsResponse,
    AdminWhatsAppChannelView,
)
from src.services.admin_whatsapp_service import AdminWhatsAppService


# O restaurante sai do token. A FILIAL aparece no corpo e no path e nao
# autoriza nada: `AdminWhatsAppService` a confronta com o escopo antes de
# qualquer leitura ou escrita, e responde 404 — nunca 403 — para filial de
# outro restaurante e para filial fora do escopo deste lojista.
router = APIRouter(prefix="/admin/whatsapp", tags=["admin whatsapp"])


@router.get(
    "/channels",
    response_model=AdminWhatsAppChannelsResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def list_channels(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminWhatsAppChannelsResponse:
    """Os numeros conectados, e por qual deles cada loja fala.

    **`branches` nao e um resumo de `channels`.** Uma filial sem linha propria
    aparece em `channels` como ausencia — e pode estar herdando o numero do
    restaurante e funcionando. `source` diz de onde vem o numero de cada loja
    (`branch`, `restaurant`, `none`) e `can_send` diz se um aviso sairia por
    ela neste minuto.

    `status` separa os tres motivos de um numero estar fora, porque eles pedem
    coisas diferentes: `disabled` religa aqui; `disconnected_by_meta` exige que
    o lojista reconecte a Cloud API no aplicativo dele antes.
    """
    return AdminWhatsAppService(db).list_channels(scope)


@router.post(
    "/channels",
    response_model=AdminWhatsAppChannelView,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
    responses={
        409: {"description": "O número, a filial ou a linha do restaurante já ocupados"}
    },
)
def connect_channel(
    payload: AdminWhatsAppChannelCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminWhatsAppChannelView:
    """Conecta um numero. `branch_id` nulo e a linha do RESTAURANTE.

    **Conectar o mesmo `phone_number_id` de novo e RECONECTAR**: troca o token,
    religa o canal e limpa a desconexao. E o caminho da rotacao de token e o da
    volta depois de um `DELETE` — sem ele, desconectar seria irreversivel pelo
    painel.

    **As tres colisoes respondem 409 com FRASE, e nao com erro de banco.** O
    schema ja as impede (`UNIQUE (phone_number_id)`, `UNIQUE (branch_id)` e o
    indice unico parcial da queda), mas as tres pedem coisas diferentes do
    lojista, e um `IntegrityError` nao diz qual e nem o que fazer:

    - **numero de outro restaurante** — a frase nao diz de quem ele e;
    - **numero que ja e de outra filial SUA** — a frase nomeia a filial;
    - **lugar ocupado** (a filial, ou a queda do restaurante) — a frase diz por
      qual numero aquele lugar fala hoje.
    """
    return AdminWhatsAppService(db).connect(scope, payload)


@router.delete(
    "/channels/{channel_id}",
    response_model=AdminWhatsAppChannelView,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def disconnect_channel(
    channel_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminWhatsAppChannelView:
    """Desliga o canal. **A linha NAO e apagada**, e por isso devolve 200 com o
    canal — nao 204.

    E a regra do cardapio, com o motivo mais forte que ela tem neste schema:
    `whatsapp_messages.channel_id` e FK **sem `ON DELETE`**, de proposito.
    Apagar o canal apagaria o registro de que o cliente foi avisado — o unico
    lugar onde isso e visivel depois. O banco recusaria o DELETE de qualquer
    jeito assim que houvesse uma mensagem.

    **As janelas de 24h ficam**, e vencem sozinhas no expurgo do `limpeza` em
    ate 24h. O prazo delas ja E o mecanismo de exclusao; apaga-las aqui seria
    um segundo mecanismo para a mesma coisa.

    **O que muda na hora:** `resolve_for_branch` deixa de escolher este canal,
    entao nenhum aviso sai por ele a partir do commit. E a filial que falava
    por um numero PROPRIO desligado **nao cai** no do restaurante — o que se
    espera de um numero desligado e que aquela loja pare de mandar.
    """
    return AdminWhatsAppService(db).disconnect(scope, channel_id)
