"""Cartao salvo do cliente: listar, salvar e remover.

## As tres decisoes que este arquivo materializa

**1. Nao ha validacao no cadastro.** Salvar um cartao NAO cobra e nao
estorna R$ 1,00. O cartao e validado na primeira cobranca de verdade. O
preco disso e conhecido e aceito: um cartao sem limite ou bloqueado entra
na lista e so falha no checkout. A alternativa — cobrar e estornar —
aparece na fatura do cliente, gera pergunta ao lojista e ainda assim nao
prova que havera limite no dia do pedido.

**2. Nao ha 3DS.** O risco de contestacao e do restaurante, porque o
dinheiro cai na conta dele. Isso nao e um detalhe tecnico escondido: esta
escrito, com a frase para dizer ao lojista, em
`docs/cartao-o-risco-que-e-do-lojista.md`.

**3. O numero do cartao nunca chega aqui.** O que atravessa e o `token`
do SDK, gerado no navegador. Nenhuma funcao deste arquivo recebe, monta,
loga ou grava PAN ou CVV.

## Por que o gateway e chamado FORA da transacao

Mesmo motivo do `PaymentService.start_online_payment`: segurar uma conexao
do pool durante um I/O externo de segundos trava a API inteira quando o
pool enche. Aqui isso se soma a um segundo motivo — a ordem entre gravar e
chamar decide qual inconsistencia e possivel, e as duas nao sao
equivalentes. Ver `save_card` e `delete_card`.
"""

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.payment_gateway import (
    MERCADOPAGO_PROVIDER,
    PaymentGatewayError,
    delete_saved_card,
    find_or_create_gateway_customer,
    save_card as save_card_at_gateway,
)
from src.models.customer_model import Customer
from src.models.customer_saved_card_model import CustomerPaymentProfile, CustomerSavedCard
from src.repositories.customer_saved_card_repository import CustomerSavedCardRepository
from src.schemas.saved_card_schema import SaveCardRequest, SavedCardResponse
from src.services.payment_credential_service import PaymentCredentialService
from src.services.restaurant_service import RestaurantService


logger = logging.getLogger("uvicorn.error")

_CARD_UNAVAILABLE_MESSAGE = "Este restaurante não está aceitando cartão no momento."
_GATEWAY_FAILED_MESSAGE = (
    "Não foi possível salvar o cartão agora. Tente novamente em instantes."
)
_GATEWAY_DELETE_FAILED_MESSAGE = (
    "Não foi possível remover o cartão agora. Tente novamente em instantes."
)
_EMAIL_REQUIRED_MESSAGE = "Confirme o e-mail da sua conta antes de salvar um cartão."


class SavedCardService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = CustomerSavedCardRepository(db)
        self.restaurant_service = RestaurantService(db)
        self.payment_credential_service = PaymentCredentialService(db)

    def list_cards(self, customer: Customer, restaurant_slug: str) -> list[SavedCardResponse]:
        """Os cartoes que esta pessoa salvou NESTE restaurante.

        Lista do NOSSO banco, sem chamar o gateway. Consultar o Mercado Pago
        a cada abertura de tela pagaria latencia de rede numa tela que abre
        em todo checkout, e devolveria exatamente os mesmos cartoes — quem
        escreve nos dois lados e este service.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        profile = self.repository.get_profile(
            customer.id, restaurant.id, settings.MERCADOPAGO_ENVIRONMENT
        )
        if profile is None:
            return []
        return [
            SavedCardResponse.model_validate(card)
            for card in self.repository.list_cards(profile.id)
        ]

    def save_card(self, customer: Customer, payload: SaveCardRequest) -> SavedCardResponse:
        """Pendura o cartao tokenizado na conta do restaurante e grava o espelho.

        ## A ordem: gateway PRIMEIRO, banco DEPOIS

        Gravar antes de o gateway confirmar criaria uma linha apontando para
        um `card_id` que nao existe — e ela so falharia no checkout, que e o
        pior lugar para descobrir. Na ordem inversa, a falha possivel e um
        cartao salvo no Mercado Pago sem linha aqui: invisivel para o
        cliente, sem efeito nenhum na cobranca (que exige o nosso id), e
        recuperavel — salvar de novo reaproveita o mesmo `card_id`, porque o
        UNIQUE (perfil, provider_card_id) faz o segundo cadastro devolver a
        linha existente em vez de duplicar.
        """
        restaurant = self.restaurant_service.get_active_restaurant(payload.restaurant_slug)
        access_token = self._resolve_access_token(restaurant.id)
        email = self._require_email(customer)
        restaurant_id = restaurant.id

        profile = self.repository.get_profile(
            customer.id, restaurant_id, settings.MERCADOPAGO_ENVIRONMENT
        )
        provider_customer_id = profile.provider_customer_id if profile else None

        # Transacao de leitura fechada antes do I/O externo.
        self.db.commit()

        if provider_customer_id is None:
            provider_customer_id = self._create_gateway_customer(access_token, email)
        card_data = self._save_at_gateway(access_token, provider_customer_id, payload.token)

        try:
            profile = self._ensure_profile(customer.id, restaurant_id, provider_customer_id)
            existing = self.repository.get_card_by_provider_id(
                profile.id, card_data.provider_card_id
            )
            if existing is not None:
                # Mesmo cartao salvo duas vezes: o Mercado Pago devolve o
                # mesmo `card_id`, e uma segunda linha faria a tela mostrar o
                # cartao em duplicata.
                response = SavedCardResponse.model_validate(existing)
                self.db.commit()
                return response

            card = self.repository.add_card(
                CustomerSavedCard(
                    payment_profile_id=profile.id,
                    provider_card_id=card_data.provider_card_id,
                    brand=card_data.brand,
                    last_four_digits=card_data.last_four_digits,
                    expiration_month=card_data.expiration_month,
                    expiration_year=card_data.expiration_year,
                )
            )
            # Montado ANTES do commit: depois dele o objeto do SQLAlchemy
            # esta expirado e cada atributo lido dispara um SELECT novo.
            response = SavedCardResponse.model_validate(card)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            "[Pagamento] cartao salvo customer_id=%s restaurant_id=%s brand=%s",
            customer.id,
            restaurant_id,
            card_data.brand,
        )
        return response

    def delete_card(self, customer: Customer, card_id: uuid.UUID) -> None:
        """Remove o cartao NOS DOIS LADOS: no Mercado Pago e no nosso banco.

        ## A ordem: gateway PRIMEIRO, banco DEPOIS

        Apagar a linha daqui primeiro deixaria o cartao pendurado na conta do
        restaurante **sem nenhuma referencia nossa a ele** — ninguem mais
        conseguiria remove-lo, nem o cliente nem o suporte, porque o
        `provider_card_id` teria ido embora junto. Uma pessoa que pede "tira
        meu cartao" e informada que ele saiu, e ele fica la.

        Por isso o gateway responde antes. Se ele estiver fora do ar, a
        remocao FALHA inteira e a linha continua aqui — o cliente tenta de
        novo e o estado permanece coerente nos dois lados. 404 do gateway
        conta como sucesso (ver `delete_saved_card`).

        ## Pedido em analise que usava este cartao

        Nada acontece com ele, e isso e proposital. A cobranca ja existe no
        Mercado Pago e vive por conta propria: ela foi criada com um TOKEN,
        nao com o `card_id`, e apagar o cartao salvo nao a cancela, nao a
        estorna e nao muda o resultado da analise antifraude. O `in_review`
        segue seu curso e o webhook decide o pedido — aprovado ou recusado —
        como decidiria se o cartao continuasse salvo. O que o cliente perde
        e a comodidade de nao redigitar no PROXIMO pedido.
        """
        card = self.repository.get_card_of_customer(customer.id, card_id)
        if card is None:
            # 404, e nao 403, para nao confirmar que aquele id existe na
            # conta de outra pessoa.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Cartão não encontrado"
            )

        profile = card.profile
        access_token = self._resolve_access_token(profile.restaurant_id)
        provider_customer_id = profile.provider_customer_id
        provider_card_id = card.provider_card_id

        self.db.commit()

        self._delete_at_gateway(access_token, provider_customer_id, provider_card_id, card_id)

        try:
            card = self.repository.get_card_of_customer(customer.id, card_id)
            if card is not None:
                self.repository.delete_card(card)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.info("[Pagamento] cartao removido customer_id=%s", customer.id)

    def _ensure_profile(
        self,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        provider_customer_id: str,
    ) -> CustomerPaymentProfile:
        existing = self.repository.get_profile(
            customer_id, restaurant_id, settings.MERCADOPAGO_ENVIRONMENT
        )
        if existing is not None:
            return existing
        return self.repository.create_profile(
            CustomerPaymentProfile(
                customer_id=customer_id,
                restaurant_id=restaurant_id,
                environment=settings.MERCADOPAGO_ENVIRONMENT,
                provider_customer_id=provider_customer_id,
            )
        )

    def _resolve_access_token(self, restaurant_id: uuid.UUID) -> str:
        """503 quando este restaurante nao processa cartao no ambiente ativo.

        O sandbox cai aqui de proposito: ele nao simula cartao (ver
        SANDBOX_SUPPORTED_PAYMENT_METHODS), e deixar salvar um cartao que
        nunca podera ser cobrado e a mesma demonstracao falsa que a
        armadilha 39 registra.
        """
        if settings.PAYMENT_PROVIDER != MERCADOPAGO_PROVIDER:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_CARD_UNAVAILABLE_MESSAGE,
            )
        credential = self.payment_credential_service.get_active_credential(restaurant_id)
        if credential is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_CARD_UNAVAILABLE_MESSAGE,
            )
        return credential.access_token

    @staticmethod
    def _require_email(customer: Customer) -> str:
        """O customer do Mercado Pago e criado POR E-MAIL, e sem ele nao ha o que criar."""
        if customer.email:
            return customer.email
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_EMAIL_REQUIRED_MESSAGE
        )

    @staticmethod
    def _create_gateway_customer(access_token: str, email: str) -> str:
        try:
            return find_or_create_gateway_customer(access_token=access_token, email=email)
        except PaymentGatewayError as exc:
            logger.warning("[Pagamento] falha ao criar customer no gateway: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_GATEWAY_FAILED_MESSAGE
            ) from exc

    @staticmethod
    def _save_at_gateway(access_token: str, provider_customer_id: str, token: str):
        try:
            return save_card_at_gateway(
                access_token=access_token,
                provider_customer_id=provider_customer_id,
                token=token,
            )
        except PaymentGatewayError as exc:
            logger.warning("[Pagamento] falha ao salvar cartao no gateway: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_GATEWAY_FAILED_MESSAGE
            ) from exc

    @staticmethod
    def _delete_at_gateway(
        access_token: str,
        provider_customer_id: str,
        provider_card_id: str,
        card_id: uuid.UUID,
    ) -> None:
        try:
            delete_saved_card(
                access_token=access_token,
                provider_customer_id=provider_customer_id,
                provider_card_id=provider_card_id,
            )
        except PaymentGatewayError as exc:
            logger.warning(
                "[Pagamento] falha ao remover cartao no gateway card_id=%s: %s",
                card_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_GATEWAY_DELETE_FAILED_MESSAGE,
            ) from exc
