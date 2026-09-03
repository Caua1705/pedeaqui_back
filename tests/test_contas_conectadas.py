"""Contas conectadas: ligar o Google já logado, desvincular, e listar.

Três rotas que pressupõem uma tela só — "contas conectadas" —, e cada uma tem
uma regra que não se vê lendo o caminho feliz.

## Ligar exige a senha, e o motivo é persistência

Ligar o Google **acrescenta uma forma de entrar**. Sem exigir a senha, um JWT
roubado vira acesso permanente: o ladrão liga o Google dele, a vítima troca a
senha (que mata todo token em circulação, `password_changed_at`), e o ladrão
volta pelo botão do Google. A troca de senha deixaria de ser a ferramenta que
expulsa quem entrou — que é exatamente o que ela existe para ser.

## Desvincular NÃO PODE DEIXAR A CONTA SEM PORTA

Conta sem senha e sem Google não tem como entrar. O `forgot-password` ainda
resolveria — o e-mail continua lá —, mas ninguém descobre isso na hora: a
pessoa vê "desvinculado com sucesso" e na tentativa seguinte não entra mais.

## O e-mail do Google NÃO precisa bater com o da conta

E isso é teste, não observação: aqui a sessão já prova de quem é a conta, e o
`id_token` prova de quem é o Google. Exigir que os dois e-mails coincidam
quebraria o caso mais comum — o Gmail do trabalho ligado à conta pessoal — sem
comprar segurança nenhuma.
"""

import unittest
import uuid
from dataclasses import replace

from fastapi import HTTPException

from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from src.integrations.google_identity_client import (
    GoogleIdentity,
    GoogleIdentityInvalidTokenError,
)
from src.schemas.customer_schema import (
    LinkGoogleAccountRequest,
    UnlinkSocialAccountRequest,
)
from src.services import google_signin_tickets as tickets
from src.services.auth_service import unusable_password_hash
from src.services.customer_social_service import CustomerSocialAccountService
from tests import fabricas
from tests.test_auth_service import SENHA, SENHA_HASH, FakeDb, make_customer
from tests.test_google_signin import (
    EMAIL,
    SUB,
    FakeIdentityClient,
    FakeSocialIdentityRepository,
    identidade_do_google,
)


class FakeSocialIdentityRepositoryComLista(FakeSocialIdentityRepository):
    """O de `test_google_signin` mais a listagem e a remocao."""

    def __init__(self, identity=None, do_cliente=None):
        super().__init__(identity)
        self.do_cliente = list(do_cliente or [])
        self.removidas = []

    def list_of_customer(self, customer_id):
        return [i for i in self.do_cliente if i.customer_id == customer_id]

    def create(self, **values):
        criada = super().create(**values)
        self.do_cliente.append(criada)
        return criada

    def delete(self, identity):
        self.removidas.append(identity)
        self.do_cliente = [i for i in self.do_cliente if i is not identity]
        return 1


def montar_servico(customer=None, social=None, identity=None, erro=None):
    servico = CustomerSocialAccountService.__new__(CustomerSocialAccountService)
    servico.db = FakeDb()
    servico.social_identity_repository = social or FakeSocialIdentityRepositoryComLista()

    nonce, nonce_token = tickets.create_nonce()
    base = identity or identidade_do_google()
    servico.identity_client = FakeIdentityClient(replace(base, nonce=nonce), erro=erro)
    servico.nonce_token = nonce_token
    return servico


def pedido_de_ligacao(servico, **sobrescritas) -> LinkGoogleAccountRequest:
    campos = {
        "id_token": "qualquer",
        "nonce_token": servico.nonce_token,
        "password": SENHA,
    }
    campos.update(sobrescritas)
    return LinkGoogleAccountRequest(**campos)


def cliente_com_senha(**sobrescritas):
    campos = {"password_hash": SENHA_HASH}
    campos.update(sobrescritas)
    return make_customer(**campos)


def cliente_sem_senha(**sobrescritas):
    campos = {"password_hash": unusable_password_hash()}
    campos.update(sobrescritas)
    return make_customer(**campos)


class TestLigarOGoogleJaLogado(unittest.TestCase):
    def test_liga_e_devolve_a_lista(self) -> None:
        cliente = cliente_com_senha()
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(social=social)

        ligadas = servico.link_google(cliente, pedido_de_ligacao(servico))

        self.assertEqual(len(social.created), 1)
        self.assertEqual(social.created[0]["customer_id"], cliente.id)
        self.assertEqual(social.created[0]["provider_user_id"], SUB)
        self.assertEqual([conta.provider for conta in ligadas], [SOCIAL_PROVIDER_GOOGLE])

    def test_o_email_do_google_nao_precisa_bater_com_o_da_conta(self) -> None:
        """O Gmail do trabalho ligado a conta pessoal. A sessao ja prova de
        quem e a conta; o `id_token`, de quem e o Google."""
        cliente = cliente_com_senha(email="pessoal@exemplo.com")
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(
            social=social, identity=identidade_do_google(email="trabalho@gmail.com")
        )

        servico.link_google(cliente, pedido_de_ligacao(servico))

        self.assertEqual(len(social.created), 1)

    def test_sem_a_senha_nao_liga(self) -> None:
        """Ligar acrescenta forma de entrar. Sem a senha, um JWT roubado vira
        acesso permanente: a troca de senha da vitima nao expulsa mais."""
        cliente = cliente_com_senha()
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(social=social)

        with self.assertRaises(HTTPException) as erro:
            servico.link_google(cliente, pedido_de_ligacao(servico, password=None))

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(social.created, [])

    def test_senha_errada_nao_liga(self) -> None:
        cliente = cliente_com_senha()
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(social=social)

        with self.assertRaises(HTTPException) as erro:
            servico.link_google(cliente, pedido_de_ligacao(servico, password="outra"))

        self.assertEqual(erro.exception.status_code, 401)
        self.assertEqual(social.created, [])

    def test_conta_sem_senha_nao_liga_um_segundo_provedor(self) -> None:
        """Ela nao tem senha para provar, e nao ha segunda prova aqui. A saida
        e definir uma senha antes — e a mensagem diz isso."""
        cliente = cliente_sem_senha()
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(social=social)

        with self.assertRaises(HTTPException) as erro:
            servico.link_google(cliente, pedido_de_ligacao(servico))

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(social.created, [])

    def test_ligar_o_mesmo_google_de_novo_e_idempotente(self) -> None:
        cliente = cliente_com_senha()
        ja_ligada = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepositoryComLista(
            identity=ja_ligada, do_cliente=[ja_ligada]
        )
        servico = montar_servico(social=social)

        ligadas = servico.link_google(cliente, pedido_de_ligacao(servico))

        self.assertEqual(social.created, [])
        self.assertEqual(len(ligadas), 1)

    def test_google_de_outra_conta_e_409(self) -> None:
        cliente = cliente_com_senha()
        alheia = fabricas.identidade_social(
            customer_id=uuid.uuid4(), provider_user_id=SUB
        )
        social = FakeSocialIdentityRepositoryComLista(identity=alheia)
        servico = montar_servico(social=social)

        with self.assertRaises(HTTPException) as erro:
            servico.link_google(cliente, pedido_de_ligacao(servico))

        self.assertEqual(erro.exception.status_code, 409)
        self.assertEqual(social.created, [])

    def test_id_token_invalido_e_401(self) -> None:
        cliente = cliente_com_senha()
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(
            social=social, erro=GoogleIdentityInvalidTokenError("x")
        )

        with self.assertRaises(HTTPException) as erro:
            servico.link_google(cliente, pedido_de_ligacao(servico))

        self.assertEqual(erro.exception.status_code, 401)
        self.assertEqual(social.created, [])

    def test_nonce_de_outra_sessao_e_401(self) -> None:
        cliente = cliente_com_senha()
        social = FakeSocialIdentityRepositoryComLista()
        servico = montar_servico(social=social)
        _, de_outra_sessao = tickets.create_nonce()

        with self.assertRaises(HTTPException) as erro:
            servico.link_google(
                cliente, pedido_de_ligacao(servico, nonce_token=de_outra_sessao)
            )

        self.assertEqual(erro.exception.status_code, 401)
        self.assertEqual(social.created, [])


class TestDesvincular(unittest.TestCase):
    def test_desvincula_e_devolve_a_lista(self) -> None:
        cliente = cliente_com_senha()
        ligada = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepositoryComLista(do_cliente=[ligada])
        servico = montar_servico(social=social)

        restantes = servico.unlink(
            cliente, SOCIAL_PROVIDER_GOOGLE, UnlinkSocialAccountRequest(password=SENHA)
        )

        self.assertEqual(social.removidas, [ligada])
        self.assertEqual(restantes, [])

    def test_a_CONTA_SEM_SENHA_nao_desvincula_a_unica_porta(self) -> None:
        """A TRAVA. Sem senha e sem Google a pessoa nao entra mais, e ela
        descobre isso na tentativa seguinte — nao no clique."""
        cliente = cliente_sem_senha()
        ligada = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepositoryComLista(do_cliente=[ligada])
        servico = montar_servico(social=social)

        with self.assertRaises(HTTPException) as erro:
            servico.unlink(
                cliente,
                SOCIAL_PROVIDER_GOOGLE,
                UnlinkSocialAccountRequest(password=SENHA),
            )

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(social.removidas, [])
        self.assertIn("senha", erro.exception.detail.lower())

    def test_a_mensagem_da_trava_diz_que_seria_a_ULTIMA_porta(self) -> None:
        """Um "defina uma senha" generico nao explica o que esta em jogo."""
        cliente = cliente_sem_senha()
        ligada = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        servico = montar_servico(
            social=FakeSocialIdentityRepositoryComLista(do_cliente=[ligada])
        )

        with self.assertRaises(HTTPException) as erro:
            servico.unlink(
                cliente,
                SOCIAL_PROVIDER_GOOGLE,
                UnlinkSocialAccountRequest(password=SENHA),
            )

        self.assertIn("única", erro.exception.detail.lower())

    def test_senha_errada_nao_desvincula(self) -> None:
        cliente = cliente_com_senha()
        ligada = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepositoryComLista(do_cliente=[ligada])
        servico = montar_servico(social=social)

        with self.assertRaises(HTTPException) as erro:
            servico.unlink(
                cliente,
                SOCIAL_PROVIDER_GOOGLE,
                UnlinkSocialAccountRequest(password="outra"),
            )

        self.assertEqual(erro.exception.status_code, 401)
        self.assertEqual(social.removidas, [])

    def test_provedor_que_nao_esta_ligado_e_404(self) -> None:
        cliente = cliente_com_senha()
        servico = montar_servico(social=FakeSocialIdentityRepositoryComLista())

        with self.assertRaises(HTTPException) as erro:
            servico.unlink(
                cliente,
                SOCIAL_PROVIDER_GOOGLE,
                UnlinkSocialAccountRequest(password=SENHA),
            )

        self.assertEqual(erro.exception.status_code, 404)

    def test_provedor_desconhecido_e_404(self) -> None:
        cliente = cliente_com_senha()
        servico = montar_servico(social=FakeSocialIdentityRepositoryComLista())

        with self.assertRaises(HTTPException) as erro:
            servico.unlink(
                cliente, "orkut", UnlinkSocialAccountRequest(password=SENHA)
            )

        self.assertEqual(erro.exception.status_code, 404)

    def test_desvincular_nao_toca_na_identidade_de_outra_pessoa(self) -> None:
        cliente = cliente_com_senha()
        minha = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        alheia = fabricas.identidade_social(
            customer_id=uuid.uuid4(), provider_user_id="outro-sub"
        )
        social = FakeSocialIdentityRepositoryComLista(do_cliente=[minha, alheia])
        servico = montar_servico(social=social)

        servico.unlink(
            cliente, SOCIAL_PROVIDER_GOOGLE, UnlinkSocialAccountRequest(password=SENHA)
        )

        self.assertEqual(social.removidas, [minha])


class TestListar(unittest.TestCase):
    def test_lista_vazia_quando_nao_ha_nenhuma(self) -> None:
        servico = montar_servico(social=FakeSocialIdentityRepositoryComLista())

        self.assertEqual(servico.list_for(cliente_com_senha()), [])

    def test_lista_o_provedor_e_as_datas(self) -> None:
        cliente = cliente_com_senha()
        ligada = fabricas.identidade_social(customer_id=cliente.id)
        servico = montar_servico(
            social=FakeSocialIdentityRepositoryComLista(do_cliente=[ligada])
        )

        contas = servico.list_for(cliente)

        self.assertEqual(len(contas), 1)
        self.assertEqual(contas[0].provider, SOCIAL_PROVIDER_GOOGLE)
        self.assertEqual(contas[0].linked_at, ligada.created_at)

    def test_a_lista_da_tela_NAO_leva_o_sub(self) -> None:
        """O `sub` e identificador da pessoa dentro do Google. Ele pertence a
        exportacao da LGPD, que e um pedido explicito — nao a uma tela de
        configuracoes que abre sozinha."""
        cliente = cliente_com_senha()
        servico = montar_servico(
            social=FakeSocialIdentityRepositoryComLista(
                do_cliente=[fabricas.identidade_social(customer_id=cliente.id)]
            )
        )

        conta = servico.list_for(cliente)[0]

        self.assertNotIn("provider_user_id", conta.model_dump())


class TestOsDadosQueOTesteUsa(unittest.TestCase):
    def test_a_identidade_do_google_e_o_dataclass_de_verdade(self) -> None:
        self.assertIsInstance(identidade_do_google(), GoogleIdentity)

    def test_o_cliente_sem_senha_nao_tem_senha_utilizavel(self) -> None:
        from src.services.auth_service import password_is_set

        self.assertFalse(password_is_set(cliente_sem_senha()))
        self.assertTrue(password_is_set(cliente_com_senha()))

    def test_o_email_do_dublê_e_o_da_constante(self) -> None:
        self.assertEqual(identidade_do_google().email, EMAIL)


if __name__ == "__main__":
    unittest.main()
