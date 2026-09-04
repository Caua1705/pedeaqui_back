"""Conectar, listar e desconectar o canal de WhatsApp pelo painel.

Ate aqui isso so existia em `scripts/register_whatsapp_channel.py`, rodado por
mim no container. O script continua — ele e o caminho de quem tem acesso ao
servidor e nao tem painel — e a rota passa a ser o caminho do lojista.

## As quatro recusas de cadastro, e por que nenhuma vem do banco

O schema ja impede as tres colisoes: `UNIQUE (phone_number_id)`,
`UNIQUE (branch_id)` e o indice unico parcial de uma queda por restaurante. Se
a rota so tentasse o INSERT, o lojista receberia um 500 com texto de
`IntegrityError` — e as tres colisoes pedem coisas DIFERENTES dele.

E a mesma divisao de trabalho de `exists_for` no aviso de pedido: **a trava
protege a tabela, a conferencia explica ao lojista.** Uma nao substitui a
outra — a trava continua sendo o que vale sob corrida.

## Recadastrar o MESMO numero e RECONECTAR, e nao um erro

E o que o `upsert` do repositorio ja fazia pelo script, e a rota precisa disso
por um motivo concreto: `DELETE` desativa. Sem o caminho de reconexao, o
lojista que desconectasse ficaria sem como religar pelo painel — conectar
responderia "ja cadastrado" sobre a propria linha dele, para sempre.

## O que a listagem responde e a lista de canais nao responde

"Por qual numero ESTA loja fala?" Uma filial sem linha propria aparece como
"sem WhatsApp" numa lista de canais, e pode estar herdando o numero do
restaurante e funcionando. Por isso `branches`, e por isso `can_send` sai de
`resolve_for_branch` — a MESMA consulta do envio, e nao um `if` equivalente
(armadilha 54).
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.whatsapp_model import WhatsAppChannel
from src.repositories.branch_repository import BranchRepository
from src.repositories.whatsapp_repository import WhatsAppChannelRepository
from src.schemas.admin_whatsapp_schema import (
    AdminWhatsAppBranchView,
    AdminWhatsAppChannelCreate,
    AdminWhatsAppChannelsResponse,
    AdminWhatsAppChannelView,
    WhatsAppBranchSource,
    WhatsAppChannelStatus,
)
from src.utils.crypto import encrypt_whatsapp_token


# Quantos digitos do `waba_id` sobrevivem a mascara. Quatro respondem "e a
# conta certa?" — que e a unica pergunta que a tela faz sobre ele — sem
# publicar o identificador inteiro no OpenAPI do painel.
DIGITOS_VISIVEIS_DO_WABA = 4

NUMERO_JA_CADASTRADO = (
    "Este número já está conectado. Se ele é da sua loja, desconecte o "
    "cadastro atual antes de conectar de novo."
)
NUMERO_DE_OUTRA_FILIAL = (
    "Este número já é o da filial {filial}. Desconecte-o lá antes de "
    "conectá-lo aqui."
)
FILIAL_JA_TEM_NUMERO = (
    "A filial {filial} já fala pelo número {numero}. Desconecte-o antes de "
    "conectar outro."
)
RESTAURANTE_JA_TEM_NUMERO = (
    "O restaurante já tem o número {numero} como padrão, e as filiais sem "
    "número próprio herdam dele. Desconecte-o antes de conectar outro."
)


class AdminWhatsAppService:
    def __init__(self, db: Session):
        self.db = db
        self.channel_repository = WhatsAppChannelRepository(db)
        self.branch_repository = BranchRepository(db)

    # --- Ler ------------------------------------------------------------------

    def list_channels(self, scope: AdminScope) -> AdminWhatsAppChannelsResponse:
        canais = self.channel_repository.list_by_restaurant(scope.restaurant_id)
        filiais = self.branch_repository.list_active_by_restaurant(scope.restaurant_id)
        nomes = {filial.id: filial.name for filial in filiais}

        return AdminWhatsAppChannelsResponse(
            channels=[self._channel_view(canal, nomes) for canal in canais],
            branches=[self._branch_view(scope, filial) for filial in self._visiveis(scope, filiais)],
        )

    def _visiveis(self, scope: AdminScope, filiais: list) -> list:
        """As filiais que ESTE lojista enxerga.

        Quem esta preso a uma filial nao ve a lista das outras — nem para
        saber por qual numero elas falam. E a mesma regra de
        `resolve_branch_filter`, aplicada a uma listagem que nao tem
        querystring de filial.
        """
        if scope.sees_all_branches:
            return filiais
        return [filial for filial in filiais if filial.id == scope.branch_id]

    def _branch_view(self, scope: AdminScope, filial) -> AdminWhatsAppBranchView:
        propria = self.channel_repository.get_by_branch(scope.restaurant_id, filial.id)
        # A MESMA consulta do envio. `can_send` que saisse de um `if` daqui
        # diria "tudo certo" no dia em que a regra do envio mudasse — e nenhum
        # cliente seria avisado.
        utilizavel = self.channel_repository.resolve_for_branch(scope.restaurant_id, filial.id)

        if propria is not None:
            return AdminWhatsAppBranchView(
                branch_id=filial.id,
                branch_name=filial.name,
                source=WhatsAppBranchSource.BRANCH,
                channel_id=propria.id,
                display_phone_number=propria.display_phone_number,
                can_send=utilizavel is not None,
            )

        if utilizavel is not None:
            return AdminWhatsAppBranchView(
                branch_id=filial.id,
                branch_name=filial.name,
                source=WhatsAppBranchSource.RESTAURANT,
                channel_id=utilizavel.id,
                display_phone_number=utilizavel.display_phone_number,
                can_send=True,
            )

        return AdminWhatsAppBranchView(
            branch_id=filial.id,
            branch_name=filial.name,
            source=WhatsAppBranchSource.NONE,
            can_send=False,
        )

    def _channel_view(
        self, canal: WhatsAppChannel, nomes: dict
    ) -> AdminWhatsAppChannelView:
        return AdminWhatsAppChannelView(
            id=canal.id,
            branch_id=canal.branch_id,
            branch_name=nomes.get(canal.branch_id),
            display_phone_number=canal.display_phone_number,
            phone_number_id=canal.phone_number_id,
            waba_id_masked=mascarar_waba(canal.waba_id),
            status=status_do_canal(canal),
            connected_at=canal.updated_at or canal.created_at,
            disconnected_at=canal.disconnected_at,
            disconnect_reason=canal.disconnect_reason,
        )

    # --- Conectar -------------------------------------------------------------

    def connect(
        self, scope: AdminScope, payload: AdminWhatsAppChannelCreate
    ) -> AdminWhatsAppChannelView:
        self._ensure_branch_in_scope(scope, payload.branch_id)

        existente = self.channel_repository.get_any_by_phone_number_id(
            payload.phone_number_id
        )
        if existente is not None:
            return self._reconectar(scope, existente, payload)

        self._ensure_slot_is_free(scope, payload.branch_id)
        return self._criar(scope, payload)

    def _ensure_branch_in_scope(
        self, scope: AdminScope, branch_id: uuid.UUID | None
    ) -> None:
        """Recusa filial de outro restaurante, e filial fora do escopo.

        **Nao ha guarda propria para `branch_id` nulo (a queda do
        restaurante), e a ausencia e deliberada.** Conectar e desconectar sao
        `SOMENTE_DONO`, e `build_admin_scope` deixa o dono SEM filial
        (`UNRESTRICTED_ROLE`): quem chega aqui enxerga a rede inteira, sempre.
        Um `if` protegendo a queda de um lojista preso a uma filial seria um
        ramo que nenhum dado alcanca — e ramo inalcancavel nao e protecao, e
        ruina que parece protecao (armadilha 13).

        O que fica valendo se um dia estas rotas abrirem para `GERENCIA` e a
        linha abaixo: `ensure_branch_allowed` ja recusa filial fora do escopo,
        e a queda passaria a precisar da decisao que hoje nao existe.
        """
        if branch_id is None:
            return

        scope.ensure_branch_allowed(branch_id)
        filial = self.branch_repository.get_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if filial is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial não encontrada"
            )

    def _reconectar(
        self,
        scope: AdminScope,
        existente: WhatsAppChannel,
        payload: AdminWhatsAppChannelCreate,
    ) -> AdminWhatsAppChannelView:
        """O numero ja tem linha. Ou e reconexao, ou e colisao.

        **A recusa de numero de OUTRO restaurante nao diz de quem ele e.** Quem
        chegou aqui ja tinha o `phone_number_id` em maos, entao a existencia da
        linha nao e novidade; de quem ela e seria.
        """
        if existente.restaurant_id != scope.restaurant_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=NUMERO_JA_CADASTRADO
            )
        if existente.branch_id != payload.branch_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=NUMERO_DE_OUTRA_FILIAL.format(
                    filial=self._nome_do_lugar(scope, existente.branch_id)
                ),
            )

        canal = self.channel_repository.upsert(
            restaurant_id=scope.restaurant_id,
            branch_id=payload.branch_id,
            waba_id=payload.waba_id,
            phone_number_id=payload.phone_number_id,
            display_phone_number=payload.display_phone_number,
            access_token_encrypted=encrypt_whatsapp_token(payload.access_token),
        )
        self.db.commit()
        return self._recem_escrito(scope, canal)

    def _ensure_slot_is_free(self, scope: AdminScope, branch_id: uuid.UUID | None) -> None:
        ocupante = self.channel_repository.get_by_branch(scope.restaurant_id, branch_id)
        if ocupante is None:
            return

        if branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=RESTAURANTE_JA_TEM_NUMERO.format(
                    numero=ocupante.display_phone_number
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=FILIAL_JA_TEM_NUMERO.format(
                filial=self._nome_do_lugar(scope, branch_id),
                numero=ocupante.display_phone_number,
            ),
        )

    def _criar(
        self, scope: AdminScope, payload: AdminWhatsAppChannelCreate
    ) -> AdminWhatsAppChannelView:
        canal = WhatsAppChannel(
            restaurant_id=scope.restaurant_id,
            branch_id=payload.branch_id,
            waba_id=payload.waba_id,
            phone_number_id=payload.phone_number_id,
            display_phone_number=payload.display_phone_number,
            access_token_encrypted=encrypt_whatsapp_token(payload.access_token),
            is_active=True,
        )
        self.db.add(canal)
        self.db.commit()
        return self._recem_escrito(scope, canal)

    # --- Desconectar ----------------------------------------------------------

    def disconnect(
        self, scope: AdminScope, channel_id: uuid.UUID
    ) -> AdminWhatsAppChannelView:
        """Desliga o canal. **NAO apaga a linha.**

        E a regra do cardapio ("nada e apagado, so desativado") pelo motivo
        mais forte que ela tem neste schema: `whatsapp_messages.channel_id` e
        FK **sem `ON DELETE`**, de proposito — apagar o canal apagaria o
        registro de que o cliente foi avisado, que e o unico lugar onde isso e
        visivel depois. Um `DELETE` de verdade aqui nem passaria: o banco
        recusaria assim que houvesse uma mensagem.

        As janelas de 24h ficam. Elas pendem do canal com `ON DELETE CASCADE`,
        mas a linha do canal continua existindo — e o prazo delas ja E o
        mecanismo de exclusao (armadilha 38): a mais nova morre em ate 24h no
        expurgo do `limpeza`. Apaga-las aqui seria um segundo mecanismo para a
        mesma coisa, e sem ganho: o que elas guardam vence sozinho antes de
        qualquer relatorio.
        """
        canal = self._find_in_scope(scope, channel_id)
        canal.is_active = False
        self.db.commit()
        return self._recem_escrito(scope, canal)

    def _find_in_scope(self, scope: AdminScope, channel_id: uuid.UUID) -> WhatsAppChannel:
        canal = self.db.get(WhatsAppChannel, channel_id)
        if canal is None or canal.restaurant_id != scope.restaurant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Canal não encontrado"
            )
        # Canal de outro restaurante e canal inexistente respondem a MESMA
        # coisa: um 403 confirmaria que aquele id existe.
        #
        # Sem guarda propria para a queda (`branch_id` nulo) pelo motivo de
        # `_ensure_branch_in_scope`: esta rota e `SOMENTE_DONO`, e dono nao tem
        # filial no escopo.
        if canal.branch_id is not None:
            scope.ensure_branch_allowed(canal.branch_id)
        return canal

    # --- Costura --------------------------------------------------------------

    def _recem_escrito(
        self, scope: AdminScope, canal: WhatsAppChannel
    ) -> AdminWhatsAppChannelView:
        self.db.refresh(canal)
        nomes = {
            filial.id: filial.name
            for filial in self.branch_repository.list_active_by_restaurant(
                scope.restaurant_id
            )
        }
        return self._channel_view(canal, nomes)

    def _nome_do_lugar(self, scope: AdminScope, branch_id: uuid.UUID | None) -> str:
        if branch_id is None:
            return "do restaurante"
        filial = self.branch_repository.get_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        return filial.name if filial is not None else "outra"


def mascarar_waba(waba_id: str) -> str:
    """`1234567890` vira `••••7890`.

    Curto demais para mascarar sai inteiro mascarado: devolver os quatro
    ultimos de um id de quatro digitos e nao mascarar nada.
    """
    if len(waba_id) <= DIGITOS_VISIVEIS_DO_WABA:
        return "•" * len(waba_id)
    return "•" * (len(waba_id) - DIGITOS_VISIVEIS_DO_WABA) + waba_id[-DIGITOS_VISIVEIS_DO_WABA:]


def status_do_canal(canal: WhatsAppChannel) -> WhatsAppChannelStatus:
    """Os tres estados, e a ORDEM em que se pergunta importa.

    A desconexao pela META vem primeiro mesmo quando o canal tambem esta
    desligado do nosso lado, e nao e detalhe: sao dois consertos, e so um
    deles depende de outra pessoa. Mostrar `disabled` a quem tambem perdeu o
    acesso na Meta faria o dono religar aqui e continuar sem receber nada, sem
    entender por que.

    A pergunta positiva (`canal_utilizavel`) fica para o `can_send`; aqui o que
    se quer e justamente distinguir os motivos, e para isso a negacao precisa
    ser lida em partes.
    """
    if canal.disconnected_at is not None:
        return WhatsAppChannelStatus.DISCONNECTED_BY_META
    if not canal.is_active:
        return WhatsAppChannelStatus.DISABLED
    return WhatsAppChannelStatus.CONNECTED
