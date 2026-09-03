"""As contas de provedor de quem JA ESTA LOGADO: ligar, desvincular, listar.

`GoogleAuthService` responde "quem e voce?" para quem ainda nao entrou. Este
responde "quais contas abrem a minha?" para quem ja entrou — e as duas
perguntas tem donos diferentes o suficiente para nao dividirem arquivo.

## Ligar exige a senha, e o motivo e PERSISTENCIA

Ligar o Google **acrescenta uma forma de entrar**. Sem a senha, um JWT roubado
deixa de ser um acesso temporario e vira permanente: o ladrao liga o Google
dele, a vitima troca a senha — que mata todo token em circulacao, pelo marco
`password_changed_at` — e o ladrao volta pelo botao do Google, com a conta
inteira dentro.

A troca de senha e a UNICA ferramenta que o cliente tem para expulsar quem
entrou na conta dele (esta escrito assim em `CustomerService.change_password`).
Uma rota que crie acesso sem pedir a senha a desliga em silencio.

Por isso a conta SEM senha utilizavel nao liga um segundo provedor: ela nao
tem com o que provar, e inventar uma segunda prova aqui seria uma quarta
tabela de codigo. A saida e definir uma senha antes, e a mensagem diz isso.

## Desvincular NAO PODE DEIXAR A CONTA SEM PORTA

Conta sem senha e sem provedor nao entra mais. `POST /auth/forgot-password`
ainda resolveria — o e-mail continua la e o codigo chega —, mas **ninguem
descobre isso na hora**: a tela diz "desvinculado" e a pessoa so encontra o
problema na proxima vez que tentar entrar, sem nenhuma pista do que aconteceu.

Por isso a recusa e ANTES, com a frase dizendo que aquela e a unica forma de
entrar. Errar para o lado de recusar custa um clique; errar para o outro custa
a conta.

## O e-mail do provedor NAO precisa bater com o da conta

Aqui a sessao ja prova de quem e a conta e o `id_token` prova de quem e o
Google: sao os dois lados que o caso (b) do login precisa provar por codigo,
e eles ja estao provados. Exigir que os e-mails coincidam quebraria o caso
comum — o Gmail do trabalho ligado a conta pessoal — sem comprar nada.
"""

import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import SOCIAL_AUTH_PROVIDERS, SOCIAL_PROVIDER_GOOGLE
from src.models.customer_model import Customer
from src.models.customer_social_identity_model import CustomerSocialIdentity
from src.repositories.customer_social_identity_repository import (
    CustomerSocialIdentityRepository,
)
from src.schemas.customer_schema import (
    LinkedSocialAccountResponse,
    LinkGoogleAccountRequest,
    UnlinkSocialAccountRequest,
)
from src.services.auth_service import password_is_set
from src.services.google_auth_service import GoogleAuthService
from src.utils.security import utcnow, verify_password


logger = logging.getLogger("uvicorn.error")


SENHA_ANTES_DE_LIGAR = (
    "Defina uma senha antes de conectar outra conta. Use "
    "\"Esqueci minha senha\": o código vai para o e-mail desta conta."
)
ULTIMA_FORMA_DE_ENTRAR = (
    "Esta é a única forma de entrar nesta conta. Defina uma senha antes de "
    "desconectá-la, ou você ficará sem como entrar."
)
SENHA_ANTES_DE_DESVINCULAR = (
    "Defina uma senha antes de gerenciar as contas conectadas."
)


class CustomerSocialAccountService:
    def __init__(self, db: Session):
        self.db = db
        self.social_identity_repository = CustomerSocialIdentityRepository(db)
        # So o cliente de identidade do `GoogleAuthService`, e nao o service
        # inteiro: o que se reaproveita aqui e a conferencia do `id_token`
        # (assinatura, `aud`, `iss` e `nonce`), que nao pode ter uma segunda
        # versao — duas divergiriam no dia em que uma ganhasse uma checagem.
        self.identity_client = GoogleAuthService(db).identity_client

    def list_for(self, customer: Customer) -> list[LinkedSocialAccountResponse]:
        return [
            _to_response(identidade)
            for identidade in self.social_identity_repository.list_of_customer(
                customer.id
            )
        ]

    def link_google(
        self, customer: Customer, payload: LinkGoogleAccountRequest
    ) -> list[LinkedSocialAccountResponse]:
        """Liga a conta do Google a esta conta, que ja esta autenticada."""
        self._ensure_password_matches(customer, payload.password, SENHA_ANTES_DE_LIGAR)
        identity = GoogleAuthService(
            self.db, identity_client=self.identity_client
        ).verified_identity(payload.id_token, payload.nonce_token)

        existente = self.social_identity_repository.get_by_provider_user(
            SOCIAL_PROVIDER_GOOGLE, identity.subject
        )
        if existente is not None:
            return self._already_linked(existente, customer)

        self.social_identity_repository.create(
            customer_id=customer.id,
            provider=SOCIAL_PROVIDER_GOOGLE,
            provider_user_id=identity.subject,
            last_login_at=utcnow(),
        )
        self.db.commit()
        return self.list_for(customer)

    def _already_linked(
        self, existente: CustomerSocialIdentity, customer: Customer
    ) -> list[LinkedSocialAccountResponse]:
        """Ligar de novo o MESMO Google e a mesma coisa que ligar uma vez.

        Apontando para outra conta, e o `sub` de alguem tentando servir a
        duas — e ai recusar e a unica resposta certa. O UNIQUE do banco
        recusaria de qualquer jeito, mas como `IntegrityError`, que vira 500.
        """
        if existente.customer_id == customer.id:
            return self.list_for(customer)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta conta do Google já está conectada a outra conta.",
        )

    def unlink(
        self,
        customer: Customer,
        provider: str,
        payload: UnlinkSocialAccountRequest,
    ) -> list[LinkedSocialAccountResponse]:
        """Desconecta o provedor. Nunca deixa a conta sem forma de entrar."""
        ligadas = self.social_identity_repository.list_of_customer(customer.id)
        alvo = _find(ligadas, provider)
        if alvo is None:
            # 404 tambem para provedor que nao existe, e nao 422: das duas
            # perguntas que o app pode estar fazendo — "existe esse provedor?"
            # e "esta conectado?" — a que importa e a segunda, e a resposta e
            # a mesma.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Esta conta não está conectada a esse provedor.",
            )

        self._ensure_would_keep_a_way_in(customer, ligadas)
        self._ensure_password_matches(
            customer, payload.password, SENHA_ANTES_DE_DESVINCULAR
        )

        self.social_identity_repository.delete(alvo)
        self.db.commit()
        logger.info(
            "[Auth Google] provedor desconectado customer_id=%s provider=%s",
            customer.id,
            provider,
        )
        return self.list_for(customer)

    @staticmethod
    def _ensure_would_keep_a_way_in(
        customer: Customer, ligadas: list[CustomerSocialIdentity]
    ) -> None:
        """A trava. Recusa ANTES de desconectar, e diz por que.

        Duas frases diferentes de propósito: "seria a unica forma de entrar" e
        o que esta em jogo, e um "defina uma senha" generico no lugar dela
        faria a pessoa achar que e burocracia.
        """
        if password_is_set(customer):
            return
        if len(ligadas) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=ULTIMA_FORMA_DE_ENTRAR
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SENHA_ANTES_DE_DESVINCULAR,
        )

    @staticmethod
    def _ensure_password_matches(
        customer: Customer, password: str | None, sem_senha: str
    ) -> None:
        """A senha atual, sempre — as duas rotas mexem em forma de entrar.

        Conta sem senha utilizavel recebe **400** com o caminho, e nao 401: um
        "senha incorreta" para quem nunca teve senha manda a pessoa procurar
        um erro que ela nao cometeu.
        """
        if not password_is_set(customer):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=sem_senha
            )
        if password is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Informe a senha atual.",
            )
        if verify_password(password, customer.password_hash):
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Senha incorreta"
        )


def _find(
    ligadas: list[CustomerSocialIdentity], provider: str
) -> CustomerSocialIdentity | None:
    if provider not in SOCIAL_AUTH_PROVIDERS:
        return None
    for identidade in ligadas:
        if identidade.provider == provider:
            return identidade
    return None


def _to_response(identidade: CustomerSocialIdentity) -> LinkedSocialAccountResponse:
    return LinkedSocialAccountResponse(
        provider=identidade.provider,
        linked_at=identidade.created_at,
        last_login_at=identidade.last_login_at,
    )
