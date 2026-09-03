"""Excluir a conta sem senha: o código no e-mail no lugar dela.

Quem entrou só pelo Google não tem senha para mandar em `DELETE
/customers/me`, e a rota exige uma. O código de seis dígitos prova a mesma
coisa que a senha provaria — que a pessoa tem acesso ao e-mail da conta.

## A REGRA QUE JÁ MORDEU, e é o que a metade de baixo deste arquivo cobra

O código de exclusão **não pode cair no caminho de criar nem de vincular**. É a
mesma família do caso (b) do "entrar com Google": um código de seis dígitos,
numa tabela de códigos, e três fluxos que o consomem.

Aqui isso não é um `if` — é o SCHEMA. `account_deletion_codes` é uma tabela
própria, e `verify_email_code` lê `email_verification_codes`. Um código de
exclusão não tem como verificar e-mail nem ligar conta do Google, e um código
de verificação não tem como apagar conta, porque eles não estão na mesma
tabela. `TestOsCodigosNaoSeMisturam` prova as duas direções.

A alternativa era uma coluna `purpose` na tabela de verificação — e ela
obrigaria a mexer em `latest_unused_email_code`, que é o fluxo de e-mail
existente.
"""

import unittest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.models.customer_model import AccountDeletionCode, EmailVerificationCode
from src.schemas.auth_schema import VerifyEmailCodeRequest
from src.schemas.customer_schema import DeleteCustomerAccountRequest
from src.services.auth_service import unusable_password_hash
from src.services.customer_anonymization_service import CustomerAnonymizationService
from src.utils.security import hash_verification_code
from tests.test_auth_service import (
    CODIGO,
    SENHA,
    SENHA_HASH,
    FakeCustomerRepository,
    FakeDb,
    FakeEmailService,
    make_code_row,
    make_customer,
)


def make_deletion_code(**sobrescritas):
    return make_code_row(modelo=AccountDeletionCode, **sobrescritas)


class FakeDbComFlush(FakeDb):
    """O `FakeDb` de `test_auth_service` mais o `flush`.

    A anonimizacao passa por varios `flush()` entre os passos — e ela precisa:
    e o que garante que a ordem dos passos (enderecos antes do cliente) valha
    de verdade. Ver `_anonymize_customer`.
    """

    def flush(self):
        self.events.append("flush")


class FakeAnonymizationCustomerRepository(FakeCustomerRepository):
    """O de `test_auth_service` mais o que a exclusao chama.

    Herda em vez de copiar porque a metade que importa aqui — as tres
    consultas de codigo, com a linha de EXCLUSAO num campo proprio — ja esta
    la, e duas copias dela divergiriam.
    """

    def update(self, customer, **values):
        for campo, valor in values.items():
            setattr(customer, campo, valor)
        return customer

    def delete_addresses_of(self, customer_id):
        return 0

    def delete_codes_of(self, customer_id):
        return 0


class FakeOrderRepository:
    def list_orders_in_flight(self, customer_id, terminais):
        return []

    def list_all_by_customer(self, customer_id):
        return []


class FakeCashbackRepository:
    def get_available_balance(self, customer_id):
        from decimal import Decimal

        return Decimal("0")


class FakeVazio:
    """Repositorio que nao tem nada e nao guarda nada. Colaborador."""

    def __init__(self):
        self.apagados_de = []

    def delete_by_customer(self, customer_id):
        self.apagados_de.append(customer_id)
        return 0

    def clear_comments_of_customer(self, customer_id):
        return 0

    def list_all_cards_of_customer(self, customer_id):
        return []

    def list_profiles_of_customer(self, customer_id):
        return []

    def delete_of_customer(self, customer_id):
        self.apagados_de.append(customer_id)
        return 0


def montar_servico(customer=None, deletion_code=None) -> CustomerAnonymizationService:
    servico = CustomerAnonymizationService.__new__(CustomerAnonymizationService)
    servico.db = FakeDbComFlush()
    servico.customer_repository = FakeAnonymizationCustomerRepository(customer=customer)
    servico.customer_repository.deletion_code = deletion_code
    servico.order_repository = FakeOrderRepository()
    servico.delivery_estimate_repository = FakeVazio()
    servico.cashback_repository = FakeCashbackRepository()
    servico.order_review_repository = FakeVazio()
    servico.saved_card_repository = FakeVazio()
    servico.social_identity_repository = FakeVazio()
    servico.email_service = FakeEmailService()
    return servico


def cliente_do_google(**sobrescritas):
    campos = {"password_hash": unusable_password_hash()}
    campos.update(sobrescritas)
    return make_customer(**campos)


class TestQuemTemSenhaContinuaComSenha(unittest.TestCase):
    """O caminho de hoje não muda uma linha."""

    def test_a_senha_certa_apaga(self) -> None:
        cliente = make_customer(password_hash=SENHA_HASH)
        servico = montar_servico(customer=cliente)

        servico.anonymize(cliente, DeleteCustomerAccountRequest(password=SENHA))

        self.assertIsNotNone(cliente.anonymized_at)

    def test_a_senha_errada_e_401(self) -> None:
        cliente = make_customer(password_hash=SENHA_HASH)
        servico = montar_servico(customer=cliente)

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(password="outra"))

        self.assertEqual(erro.exception.status_code, 401)
        self.assertIsNone(cliente.anonymized_at)

    def test_codigo_no_lugar_da_senha_nao_serve_para_quem_tem_senha(self) -> None:
        """A conta com senha exige a senha. Aceitar o código nas duas
        rebaixaria a exigência de toda conta que tem uma."""
        cliente = make_customer(password_hash=SENHA_HASH)
        servico = montar_servico(customer=cliente, deletion_code=make_deletion_code())

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code=CODIGO))

        self.assertEqual(erro.exception.status_code, 400)
        self.assertIsNone(cliente.anonymized_at)


class TestQuemNaoTemSenhaUsaOCodigo(unittest.TestCase):
    def test_o_codigo_certo_apaga(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente, deletion_code=make_deletion_code())

        servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code=CODIGO))

        self.assertIsNotNone(cliente.anonymized_at)

    def test_o_codigo_errado_e_401_e_nao_apaga(self) -> None:
        cliente = cliente_do_google()
        linha = make_deletion_code()
        servico = montar_servico(customer=cliente, deletion_code=linha)

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code="000000"))

        self.assertEqual(erro.exception.status_code, 401)
        self.assertIsNone(cliente.anonymized_at)

    def test_o_codigo_errado_conta_tentativa(self) -> None:
        """Sem o contador, seis dígitos caem por força bruta em minutos — e o
        que está do outro lado é irreversível."""
        cliente = cliente_do_google()
        linha = make_deletion_code()
        servico = montar_servico(customer=cliente, deletion_code=linha)

        with self.assertRaises(HTTPException):
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code="000000"))

        self.assertEqual(linha.attempts_count, 1)

    def test_muitas_tentativas_e_429(self) -> None:
        cliente = cliente_do_google()
        linha = make_deletion_code(attempts=5)
        servico = montar_servico(customer=cliente, deletion_code=linha)

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code=CODIGO))

        self.assertEqual(erro.exception.status_code, 429)
        self.assertIsNone(cliente.anonymized_at)

    def test_codigo_vencido_e_401(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(
            customer=cliente, deletion_code=make_deletion_code(expires_in_minutes=-1)
        )

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code=CODIGO))

        self.assertEqual(erro.exception.status_code, 401)
        self.assertIsNone(cliente.anonymized_at)

    def test_sem_codigo_pedido_e_401(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente, deletion_code=None)

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code=CODIGO))

        self.assertEqual(erro.exception.status_code, 401)

    def test_senha_no_lugar_do_codigo_nao_serve(self) -> None:
        """A conta sem senha não tem senha para conferir. Aceitar o campo
        faria `verify_password` decidir contra um hash inutilizável — que
        recusa tudo, e a mensagem sairia "senha incorreta" para quem nunca
        teve senha."""
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente, deletion_code=make_deletion_code())

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(password=SENHA))

        self.assertEqual(erro.exception.status_code, 400)
        self.assertIsNone(cliente.anonymized_at)

    def test_corpo_vazio_e_400(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente, deletion_code=make_deletion_code())

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest())

        self.assertEqual(erro.exception.status_code, 400)


class TestOPedidoDoCodigo(unittest.TestCase):
    def test_manda_o_codigo_para_o_email_da_conta(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente)

        servico.request_deletion_code(cliente)

        enviados = servico.email_service.deletion_codes
        self.assertEqual(len(enviados), 1)
        self.assertEqual(enviados[0][0], cliente.email)

    def test_o_codigo_e_gravado_em_hmac_e_nunca_em_claro(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente)

        servico.request_deletion_code(cliente)

        _, codigo = servico.email_service.deletion_codes[0]
        gravado = servico.customer_repository.deletion_codes_created[0]
        self.assertNotEqual(gravado["code_hash"], codigo)
        self.assertEqual(gravado["code_hash"], hash_verification_code(codigo))

    def test_conta_com_senha_nao_pede_codigo(self) -> None:
        """Ela já tem prova. Emitir código aqui abriria um segundo caminho de
        exclusão para uma conta que não precisa dele."""
        cliente = make_customer(password_hash=SENHA_HASH)
        servico = montar_servico(customer=cliente)

        with self.assertRaises(HTTPException) as erro:
            servico.request_deletion_code(cliente)

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(servico.email_service.deletion_codes, [])


class TestOsCodigosNaoSeMisturam(unittest.TestCase):
    """A regra que já mordeu, cobrada nas DUAS direções.

    Não é um `if`: são duas tabelas. `verify_email_code` lê
    `email_verification_codes`, a exclusão lê `account_deletion_codes`, e
    nenhuma das duas enxerga a linha da outra.
    """

    def test_o_codigo_de_exclusao_nao_verifica_email_nem_liga_o_google(self) -> None:
        from src.services.auth_service import AuthService

        cliente = cliente_do_google(email_verified_at=None)
        auth = AuthService.__new__(AuthService)
        auth.db = FakeDb()
        # A tabela de verificacao esta VAZIA: so existe uma linha de exclusao.
        auth.customer_repository = FakeCustomerRepository(customer=cliente, email_code=None)
        auth.customer_repository.deletion_code = make_deletion_code()
        auth.social_identity_repository = FakeVazio()
        auth.email_service = FakeEmailService()

        with self.assertRaises(HTTPException) as erro:
            auth.verify_email_code(
                VerifyEmailCodeRequest(email=cliente.email, code=CODIGO)
            )

        self.assertEqual(erro.exception.status_code, 400)
        self.assertIsNone(cliente.email_verified_at)

    def test_o_codigo_de_verificacao_nao_apaga_a_conta(self) -> None:
        cliente = cliente_do_google()
        servico = montar_servico(customer=cliente, deletion_code=None)
        # Ha uma linha de VERIFICACAO valida, com o mesmo codigo.
        servico.customer_repository.email_code = make_code_row()

        with self.assertRaises(HTTPException) as erro:
            servico.anonymize(cliente, DeleteCustomerAccountRequest(email_code=CODIGO))

        self.assertEqual(erro.exception.status_code, 401)
        self.assertIsNone(cliente.anonymized_at)

    def test_as_duas_tabelas_sao_models_diferentes(self) -> None:
        """O que torna as duas de cima estruturais, e não um `if` que alguém
        pode remover."""
        self.assertIsNot(AccountDeletionCode, EmailVerificationCode)
        self.assertNotEqual(
            AccountDeletionCode.__tablename__, EmailVerificationCode.__tablename__
        )


class TestOsDadosQueOTesteUsa(unittest.TestCase):
    """O par do `pytest.raises`: os dublês descrevem linhas que existem."""

    def test_a_linha_de_exclusao_e_o_model_de_verdade(self) -> None:
        linha = make_deletion_code()
        self.assertIsInstance(linha, AccountDeletionCode)
        self.assertEqual(linha.code_hash, hash_verification_code(CODIGO))
        self.assertIsInstance(linha.expires_at, datetime)

    def test_o_cliente_do_google_nao_tem_senha_utilizavel(self) -> None:
        from src.services.auth_service import password_is_set

        self.assertFalse(password_is_set(cliente_do_google()))

    def test_a_linha_vencida_esta_mesmo_vencida(self) -> None:
        linha = make_deletion_code(expires_in_minutes=-1)
        self.assertLess(linha.expires_at, datetime.now(timezone.utc) + timedelta(0))


if __name__ == "__main__":
    unittest.main()
