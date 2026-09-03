"""Exclusao de conta do cliente, por ANONIMIZACAO (LGPD, Art. 18, VI).

Desenho completo em `docs/lgpd-fase2-exclusao-de-conta.md`. O resumo do que
decide tudo: **fica o que e da VENDA, sai o que e da PESSOA.** O restaurante
vendeu, faturou e pagou comissao sobre aquele pedido, e esses numeros tem que
continuar batendo em relatorio e em fiscalizacao. Nome, telefone, endereco e
recado nao entram em nenhum deles.

## Por que nao e um DELETE

Nao e preferencia. `coupon_redemptions.customer_id` e NOT NULL e sem
`ON DELETE`, entao `DELETE FROM customers` FALHA para toda pessoa que ja usou
um cupom. As duas saidas seriam apagar a redencao junto — e ai o cupom de uso
unico volta a estar disponivel, o que transforma "apagar a conta" numa forma
de reciclar desconto — ou relaxar o NOT NULL, que deixaria o controle de "uma
vez por cliente" sem cliente.

E `cashback_transactions.customer_id` e `ON DELETE CASCADE`: um DELETE levaria
o extrato inteiro junto, credito nao usado incluido, sem aviso nenhum.

## Por que o service e proprio, e nao um metodo do AuthService

O `AuthService` ja e o maior do projeto, e isto aqui e uma operacao destrutiva
de transacao unica. Ela se le melhor sozinha, de cima a baixo.
"""

import logging
import secrets
import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.customer_model import Customer
from src.models.order_model import Order
from src.repositories.cashback_repository import CashbackRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.customer_saved_card_repository import CustomerSavedCardRepository
from src.repositories.customer_social_identity_repository import (
    CustomerSocialIdentityRepository,
)
from src.repositories.order_review_repository import OrderReviewRepository
from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.repositories.order_repository import OrderRepository
from src.integrations.payment_gateway import PaymentGatewayError, delete_saved_card
from src.services.order_state_machine import TERMINAL_ORDER_STATUSES
from src.services.payment_credential_service import PaymentCredentialService
from src.utils.security import hash_password, utcnow, verify_password


logger = logging.getLogger("uvicorn.error")


# O nome que sobra na venda e na lista do lojista. E texto, e nao NULL, porque
# `orders.customer_name_snapshot` e NOT NULL — e porque a tela do painel
# precisa mostrar alguma coisa naquela coluna.
ANONYMIZED_NAME = "Cliente removido"

# Data de nascimento sentinela. A coluna e NOT NULL e nada no produto le
# aniversario hoje; 1900 e obviamente falsa para quem olhar.
ANONYMIZED_BIRTH_DATE = date(1900, 1, 1)


def anonymized_email(customer_id: uuid.UUID) -> str:
    """O e-mail sentinela, derivado do id — unico por construcao.

    `.invalid` e reservado pela RFC 2606 e nunca podera ser um dominio de
    verdade: nenhuma anonimizacao vai colidir com um e-mail real, hoje ou
    depois. Derivar do id, e nao usar um valor fixo, e o que permite anonimizar
    duas contas sem violar o UNIQUE da coluna.
    """
    return f"removido+{customer_id}@rapidex.invalid"


def anonymized_phone(customer_id: uuid.UUID) -> str:
    """O telefone sentinela. NAO e numerico, de proposito.

    Qualquer coisa que trate este valor como telefone falha alto, em vez de
    mandar um SMS para um numero inventado que pode ser de outra pessoa.
    """
    return f"removido-{customer_id.hex}"


class CustomerAnonymizationService:
    def __init__(self, db: Session):
        self.db = db
        self.customer_repository = CustomerRepository(db)
        self.order_repository = OrderRepository(db)
        self.delivery_estimate_repository = DeliveryEstimateRepository(db)
        self.cashback_repository = CashbackRepository(db)
        self.order_review_repository = OrderReviewRepository(db)
        self.saved_card_repository = CustomerSavedCardRepository(db)
        self.social_identity_repository = CustomerSocialIdentityRepository(db)
        self.payment_credential_service = PaymentCredentialService(db)

    def anonymize(self, customer: Customer, password: str) -> None:
        """Apaga a pessoa e mantem a venda. Uma transacao, um commit.

        Nao ha desfazer: quando esta funcao volta, o e-mail e o telefone
        antigos nao existem em lugar nenhum do banco vivo.

        A ordem dos passos nao e arbitraria — ver `_anonymize_customer`.
        """
        self._ensure_password_matches(customer, password)
        self._ensure_no_order_in_flight(customer)

        # Lido ANTES de anonimizar, e nao depois: e o saldo que a pessoa
        # esta perdendo neste instante, e depois do commit ele passa a ser o
        # saldo de um fantasma.
        cashback_perdido = self.cashback_repository.get_available_balance(customer.id)

        # FORA da transacao, e ANTES dela, porque e I/O de rede: uma chamada
        # ao Mercado Pago dentro do `try` seguraria a conexao do banco
        # durante segundos e, pior, um gateway fora do ar impediria alguem
        # de exercer o direito de apagar a propria conta. Falha aqui e
        # registrada e engolida — ver _forget_cards_at_gateway.
        cartoes_orfaos = self._forget_cards_at_gateway(customer)

        try:
            self._anonymize_orders(customer)
            self._clear_review_comments(customer)
            self._delete_saved_cards(customer)
            self._delete_addresses(customer)
            self._delete_delivery_estimates(customer)
            self._delete_verification_codes(customer)
            self._delete_social_identities(customer)
            self._anonymize_customer(customer)
            self.db.commit()
        except Exception:
            # Um commit no meio deixaria o pior estado possivel: pedidos ja
            # anonimizados e o cliente ainda ativo — o lojista perde o
            # historico e a pessoa continua logada. Ou tudo, ou nada.
            self.db.rollback()
            raise

        # `customer_id` continua no log, e continua sendo o que liga as linhas
        # de uma requisicao. Depois desta funcao ele e um pseudonimo que nao
        # resolve mais para pessoa nenhuma.
        logger.info("[LGPD] conta anonimizada customer_id=%s", customer.id)
        self._log_forfeited_cashback(customer.id, cashback_perdido)
        self._log_orphan_cards(customer.id, cartoes_orfaos)

    @staticmethod
    def _log_forfeited_cashback(customer_id: uuid.UUID, balance: Decimal) -> None:
        """O rastro do saldo que a pessoa perdeu ao excluir a conta.

        POR QUE EXISTE. A anonimizacao nao toca em `cashback_transactions` —
        e a decisao certa, e esta em `docs/lgpd-fase2-exclusao-de-conta.md`:
        o extrato e financeiro e nao e dado de identificacao. Mas o efeito
        pratico e que o saldo fica inalcancavel: `_registration_conflicts`
        procura por e-mail e telefone, os dois deixaram de existir na tabela,
        e a pessoa volta com **id novo**. O cashback continua ligado ao id
        velho, e nao ha caminho de volta.

        Fora do `try`, DEPOIS do commit, pelo mesmo motivo da linha acima:
        log e efeito colateral, e nao pode fazer parte da transacao unica.

        LINHA PROPRIA e so quando ha saldo, em vez de um campo na linha de
        cima. Um campo obrigaria a filtrar os zeros para achar o caso que
        importa — e o que se quer aqui e o mesmo que o
        `[Pagamento] pedido pago foi cancelled sem estorno` da armadilha 25:
        um grep que devolve DINHEIRO PARADO, nao todo evento do tipo.
        """
        if balance <= 0:
            return
        logger.warning(
            "[LGPD] conta anonimizada com saldo de cashback perdido "
            "customer_id=%s saldo=%s",
            customer_id,
            balance,
        )

    def _ensure_password_matches(self, customer: Customer, password: str) -> None:
        """A mesma exigencia de qualquer operacao irreversivel de conta.

        Sem ela, um token vazado — ou um celular esquecido destravado — apaga
        a conta de alguem.
        """
        if verify_password(password, customer.password_hash):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Senha incorreta",
        )

    def _ensure_no_order_in_flight(self, customer: Customer) -> None:
        """Recusa enquanto houver comida a caminho.

        Pedido fora de `completed`, `cancelled` e `rejected` esta na mao do
        restaurante: anonimizar no meio tira o nome e o telefone de quem o
        entregador precisa achar, e o pedido vira orfao no meio da entrega.

        E recusa TEMPORARIA, e o 409 leva os numeros de pedido junto — sem
        eles a pessoa nao tem como saber o que esta segurando a exclusao.
        """
        em_curso = self.order_repository.list_orders_in_flight(
            customer.id, TERMINAL_ORDER_STATUSES
        )
        if not em_curso:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "Você tem pedidos em andamento. "
                    "Tente novamente quando eles forem concluídos."
                ),
                "orders_in_flight": [pedido.order_number for pedido in em_curso],
            },
        )

    def _anonymize_orders(self, customer: Customer) -> None:
        for pedido in self.order_repository.list_all_by_customer(customer.id):
            self._strip_person_from_order(pedido, customer.id)
        self.db.flush()

    @staticmethod
    def _strip_person_from_order(order: Order, customer_id: uuid.UUID) -> None:
        """O que sai do pedido, campo a campo.

        Fica de fora desta lista, e de proposito: todo valor de dinheiro, os
        campos de comissao, o cupom, o pagamento, os itens, e
        `delivery_distance_km` — que e o que justifica a taxa cobrada naquele
        pedido, e e um raio, nao um ponto: nao localiza a casa de ninguem.

        `address_neighborhood`, `address_city` e `address_state` tambem ficam:
        bairro nao identifica ninguem sozinho, e e o recorte que sustenta "de
        onde vem meus pedidos", que e analise legitima do lojista.
        """
        # Os dois snapshots sao NOT NULL: viram sentinela, nao NULL.
        order.customer_name_snapshot = ANONYMIZED_NAME
        order.customer_phone_snapshot = anonymized_phone(customer_id)

        order.address_street = None
        order.address_number = None
        order.address_complement = None
        order.address_reference = None
        order.address_zipcode = None
        order.delivery_latitude = None
        order.delivery_longitude = None

        # Campo livre, e na pratica mistura o que e pedido ("sem cebola") com
        # o que e pessoa ("apartamento 302, falar com a Maria"). Nao ha como
        # separar os dois automaticamente, e o segundo e justamente o que a
        # exclusao existe para tirar.
        order.notes = None

        # Explicito, e nao pelo `ON DELETE SET NULL` do passo seguinte: o
        # vinculo tem que sumir mesmo que a FK mude um dia.
        order.customer_address_id = None

    def _clear_review_comments(self, customer: Customer) -> None:
        """O texto que a pessoa escreveu avaliando os pedidos dela. A NOTA FICA.

        Mesma linha que decide o resto deste service — fica o que e da
        VENDA, sai o que e da PESSOA. A nota e numero, nao identifica
        ninguem e e o historico de qualidade do restaurante: apaga-la
        reescreveria a media do lojista a cada exclusao de conta. O
        comentario e campo livre, e mistura "demorou" com "moro no 302,
        falar com a Maria" — exatamente como `order.notes`, logo acima.

        Alcanca por `orders.customer_id`, que e o que o `ai_feedback` nao
        tinha. Mas alcanca SO quem tem conta: o comentario do pedido de
        convidado tem `customer_id` nulo e sai pela retencao
        (`order_review_service.review_retention_cutoff`).
        """
        self.order_review_repository.clear_comments_of_customer(customer.id)

    def _forget_cards_at_gateway(self, customer: Customer) -> int:
        """Apaga os cartoes salvos na conta do Mercado Pago de cada lojista.

        MELHOR ESFORCO, de proposito. O direito de apagar a conta nao pode
        ficar refem de o gateway estar de pe: uma instabilidade deles
        transformaria a exclusao num 502 que a pessoa nao tem como resolver,
        e a LGPD nao admite esse tipo de dependencia. Por isso a falha e
        contada e logada, nunca propagada.

        O que sobra quando falha e um cartao pendurado na conta do lojista
        sem referencia nossa. Nao ha PAN nem CVV la — o Mercado Pago guarda
        o cartao, nao nos — mas e um resto que alguem precisa saber que
        existe, e por isso ele sai no log com o customer_id.

        ANTES da transacao e nao depois do commit: depois do commit as linhas
        ja teriam sido apagadas e nao haveria mais como saber quais ids
        remover.
        """
        falhas = 0
        for card in self.saved_card_repository.list_all_cards_of_customer(customer.id):
            if not self._delete_one_card_at_gateway(card):
                falhas += 1
        return falhas

    def _delete_one_card_at_gateway(self, card) -> bool:
        profile = card.profile
        credential = self.payment_credential_service.get_active_credential(
            profile.restaurant_id
        )
        if credential is None:
            # Restaurante sem credencial no ambiente ativo: nao ha conta a
            # que pedir a remocao. Conta como resto, nao como sucesso.
            return False
        try:
            delete_saved_card(
                access_token=credential.access_token,
                provider_customer_id=profile.provider_customer_id,
                provider_card_id=card.provider_card_id,
            )
            return True
        except PaymentGatewayError as exc:
            logger.warning(
                "[LGPD] falha ao remover cartao no gateway customer_id=%s: %s",
                profile.customer_id,
                exc,
            )
            return False

    @staticmethod
    def _log_orphan_cards(customer_id: uuid.UUID, falhas: int) -> None:
        """Linha propria e SO quando sobrou cartao, pelo mesmo motivo do
        `_log_forfeited_cashback`: o que se quer de um grep e o caso que
        precisa de acao, nao todo evento do tipo."""
        if falhas <= 0:
            return
        logger.warning(
            "[LGPD] conta anonimizada com cartao remanescente no gateway "
            "customer_id=%s cartoes=%s",
            customer_id,
            falhas,
        )

    def _delete_saved_cards(self, customer: Customer) -> None:
        """Apaga cartoes E perfis daqui dentro da transacao unica.

        O perfil vai junto: ele guarda o id do "customer" que o Mercado Pago
        criou a partir do e-mail da pessoa, e deixar a linha seria manter um
        ponteiro para um cadastro dela num sistema de terceiro, que e
        exatamente o que a anonimizacao existe para nao fazer.
        """
        for card in self.saved_card_repository.list_all_cards_of_customer(customer.id):
            self.saved_card_repository.delete_card(card)
        for profile in self.saved_card_repository.list_profiles_of_customer(customer.id):
            self.saved_card_repository.delete_profile(profile)
        self.db.flush()

    def _delete_addresses(self, customer: Customer) -> None:
        self.customer_repository.delete_addresses_of(customer.id)

    def _delete_delivery_estimates(self, customer: Customer) -> None:
        self.delivery_estimate_repository.delete_by_customer(customer.id)

    def _delete_verification_codes(self, customer: Customer) -> None:
        self.customer_repository.delete_codes_of(customer.id)

    def _delete_social_identities(self, customer: Customer) -> None:
        """Desliga as contas de provedor (o "entrar com Google").

        Mesmo motivo de `_delete_saved_cards` apagar o PERFIL junto do cartao:
        a linha guarda o `sub`, que e o identificador da pessoa dentro do
        Google, e manter um ponteiro para o cadastro dela num sistema de
        terceiro e exatamente o que a exclusao existe para nao fazer.

        E ha um segundo estrago, que nao e de privacidade e e o que se ve
        primeiro: sem este passo, quem excluisse a conta e entrasse com o
        Google de novo cairia no caso "sub conhecido" — logado numa conta
        `is_active=False`, 403 para sempre e sem como se recadastrar pelo
        Google. Com ele, a pessoa volta pelo caminho de cliente novo, que e o
        certo: a conta anterior nao existe mais.

        Nao ha chamada ao Google aqui, e nao ha o que chamar: o vinculo e so
        deste lado. O que a pessoa possa ter autorizado na conta dela do
        Google se revoga la, na tela de permissoes deles.
        """
        self.social_identity_repository.delete_of_customer(customer.id)

    def _anonymize_customer(self, customer: Customer) -> None:
        """POR ULTIMO, mesmo sendo o principal.

        Os passos anteriores acham as linhas PELO `customer_id`. Numa ordem em
        que este viesse antes, uma falha no meio deixaria o cliente ja
        anonimizado com enderecos intactos apontando para ele. Nesta ordem, o
        unico estado possivel depois de uma falha e "nada aconteceu".
        """
        agora = utcnow()
        self.customer_repository.update(
            customer,
            name=ANONYMIZED_NAME,
            # A saida dos valores antigos DA COLUNA e o que libera o recadastro:
            # `_registration_conflicts` procura por e-mail e telefone, e eles
            # deixaram de existir na tabela. A pessoa volta com id novo.
            email=anonymized_email(customer.id),
            phone=anonymized_phone(customer.id),
            birth_date=ANONYMIZED_BIRTH_DATE,
            # Hash de um segredo sorteado e jogado fora. Um valor invalido
            # qualquer tambem recusaria o login, mas este e o unico que nao
            # depende de como `verify_password` trata lixo.
            password_hash=hash_password(secrets.token_urlsafe(32)),
            # Mata todo JWT em circulacao — inclusive o que acabou de fazer
            # esta chamada. E o mecanismo que ja existe para expulsar sessao,
            # e nao ha lista de sessoes ativas para revogar uma a uma.
            password_changed_at=agora,
            email_verified_at=None,
            phone_verified_at=None,
            marketing_opt_in=False,
            is_active=False,
            anonymized_at=agora,
        )
