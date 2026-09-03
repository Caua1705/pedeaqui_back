"""LGPD Fase 2 — exclusão de conta por anonimização.

Contra o Postgres de verdade, porque o que estes testes protegem é
justamente o que o schema faz: o `NOT NULL` de `coupon_redemptions.customer_id`
que impede o DELETE, o `UNIQUE` de `customers.email` que a sentinela tem que
respeitar, e o `ON DELETE SET NULL` que solta o pedido do endereço apagado.
Um dublê de banco não teria nenhum dos três.

O formato é sempre o mesmo: **medir antes, anonimizar, medir depois, exigir
igualdade** — do lado do lojista — e **exigir diferença** do lado da pessoa.

Desenho em `docs/lgpd-fase2-exclusao-de-conta.md`.
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.endpoints import customers as rota_de_clientes
from src.api.middleware.rate_limit_state import RateLimitStateMiddleware
from src.api.rate_limit import limiter
from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from src.models.cashback_transaction_model import CashbackTransaction
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.coupon_redemption_model import CouponRedemption
from src.models.customer_model import (
    AccountDeletionCode,
    Customer,
    EmailVerificationCode,
)
from src.models.customer_social_identity_model import CustomerSocialIdentity
from src.models.delivery_estimate_model import DeliveryEstimate
from src.models.order_model import Order
from src.models.order_review_model import OrderReview
from src.repositories.customer_repository import CustomerRepository
from src.schemas.customer_schema import DeleteCustomerAccountRequest
from src.repositories.customer_social_identity_repository import (
    CustomerSocialIdentityRepository,
)
from src.schemas.auth_schema import VerifyEmailCodeRequest
from src.services.auth_service import AuthService, unusable_password_hash
from src.services.customer_anonymization_service import (
    ANONYMIZED_NAME,
    CustomerAnonymizationService,
    anonymized_email,
)
from src.utils.security import hash_password, hash_verification_code
from tests.fabricas_db import (
    criar_cliente,
    criar_endereco,
    criar_filial,
    criar_pedido,
    criar_restaurante,
)


pytestmark = pytest.mark.db

SENHA = "senha-do-cliente-123"

# O bcrypt custa ~0,3s de propósito. Um hash por cenário cobraria isso em cada
# teste do módulo, e são muitos.
SENHA_HASH = hash_password(SENHA)


# ---------------------------------------------------------------------------
# O cenário base, compartilhado por quase todos os testes
# ---------------------------------------------------------------------------


class Cenario:
    """Um restaurante, uma filial, um cliente com DOIS pedidos `completed`.

    Um dos pedidos tem cupom resgatado, o outro não. Junto vêm endereço
    salvo, cashback lançado, estimativa de entrega e um código de verificação
    de e-mail — ou seja, uma linha em cada tabela que a exclusão toca.
    """

    def __init__(self, db):
        self.db = db
        self.restaurante = criar_restaurante(db)
        self.filial = criar_filial(db, self.restaurante)
        self.cliente = criar_cliente(db)
        self.cliente.password_hash = SENHA_HASH
        self.email_antigo = self.cliente.email
        self.telefone_antigo = self.cliente.phone
        self.id_antigo = self.cliente.id

        self.endereco = criar_endereco(db, self.cliente, is_default=True)

        self.token_do_pedido = f"token-{uuid.uuid4().hex}"
        self.pedido_com_cupom = self._pedido(self.token_do_pedido)
        self.pedido_sem_cupom = self._pedido(f"token-{uuid.uuid4().hex}")

        self.cupom = self._cupom()
        self.resgate = CouponRedemption(
            coupon_id=self.cupom.id,
            customer_id=self.cliente.id,
            order_id=self.pedido_com_cupom.id,
            discount_amount=Decimal("5.00"),
            status="applied",
            idempotency_key=f"resgate-{uuid.uuid4().hex}",
        )
        db.add(self.resgate)

        self.cashback = CashbackTransaction(
            customer_id=self.cliente.id,
            restaurant_id=self.restaurante.id,
            type="earned",
            amount=Decimal("7.50"),
            status="available",
        )
        db.add(self.cashback)

        self.estimativa = DeliveryEstimate(
            restaurant_id=self.restaurante.id,
            branch_id=self.filial.id,
            customer_id=self.cliente.id,
            token=f"est-{uuid.uuid4().hex}",
            address_fingerprint=uuid.uuid4().hex,
            distance_km=Decimal("3.20"),
            delivery_fee=Decimal("8.00"),
            provider="google",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        db.add(self.estimativa)

        self.codigo = EmailVerificationCode(
            customer_id=self.cliente.id,
            email=self.email_antigo,
            code_hash="hash-do-codigo",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        db.add(self.codigo)
        # `commit`, e não `flush`, e o motivo é o
        # `test_falha_no_meio_nao_deixa_estado_pela_metade`. A fixture `db`
        # roda com `join_transaction_mode="create_savepoint"`: o `rollback`
        # do service volta ao savepoint ABERTO NA PRIMEIRA ESCRITA da sessão.
        # Sem um commit aqui, esse savepoint é o de antes deste cenário, e o
        # rollback levaria o cenário inteiro junto — o teste passaria por não
        # encontrar linha nenhuma, que é o oposto do que ele afirma.
        # O commit solta um savepoint novo; a transação externa da fixture
        # continua e desfaz tudo no fim, como nos outros testes.
        db.commit()

    def _pedido(self, tracking_token: str) -> Order:
        pedido = criar_pedido(
            self.db,
            self.restaurante,
            self.filial,
            cliente=self.cliente,
            status="completed",
            total=Decimal("52.90"),
            tracking_token=tracking_token,
        )
        pedido.customer_address_id = self.endereco.id
        pedido.subtotal = Decimal("44.90")
        pedido.delivery_fee = Decimal("8.00")
        pedido.commission_percent = Decimal("10.00")
        pedido.commission_base_amount = Decimal("44.90")
        pedido.commission_amount = Decimal("4.49")
        pedido.address_street = "Rua das Flores"
        pedido.address_number = "302"
        pedido.address_complement = "Apto 12"
        pedido.address_reference = "Ao lado da praça"
        pedido.address_neighborhood = "Aldeota"
        pedido.address_city = "Fortaleza"
        pedido.address_state = "CE"
        pedido.address_zipcode = "60150000"
        pedido.delivery_latitude = Decimal("-3.7327000")
        pedido.delivery_longitude = Decimal("-38.5270000")
        pedido.delivery_distance_km = Decimal("3.20")
        pedido.notes = "apartamento 302, falar com a Maria"
        self.db.flush()
        return pedido

    def _cupom(self) -> RestaurantCoupon:
        modelo = CouponTemplate(
            name="Modelo",
            image_path="coupons/modelo.png",
            discount_type="fixed",
            discount_value=Decimal("5.00"),
        )
        self.db.add(modelo)
        self.db.flush()
        agora = datetime.now(timezone.utc)
        cupom = RestaurantCoupon(
            restaurant_id=self.restaurante.id,
            coupon_template_id=modelo.id,
            code=f"CUPOM{uuid.uuid4().hex[:6].upper()}",
            title="Cupom de teste",
            discount_type="fixed",
            discount_value=Decimal("5.00"),
            valid_from=agora - timedelta(days=1),
            valid_until=agora + timedelta(days=30),
            # Uso ÚNICO por cliente: é o que faz o resgate ter que sobreviver.
            usage_limit_per_customer=1,
        )
        self.db.add(cupom)
        self.db.flush()
        return cupom


@pytest.fixture
def cenario(db) -> Cenario:
    return Cenario(db)


def anonimizar(db, cliente, senha: str = SENHA) -> None:
    CustomerAnonymizationService(db).anonymize(
        cliente, DeleteCustomerAccountRequest(password=senha)
    )


# ---------------------------------------------------------------------------
# O que NÃO pode mudar: o histórico do lojista
# ---------------------------------------------------------------------------


def test_os_valores_do_pedido_ficam_intactos(db, cenario):
    antes = {
        pedido.id: (
            pedido.subtotal,
            pedido.delivery_fee,
            pedido.service_fee,
            pedido.total,
            pedido.commission_percent,
            pedido.commission_base_amount,
            pedido.commission_amount,
        )
        for pedido in (cenario.pedido_com_cupom, cenario.pedido_sem_cupom)
    }

    anonimizar(db, cenario.cliente)

    for pedido_id, valores in antes.items():
        pedido = db.get(Order, pedido_id)
        db.refresh(pedido)
        assert (
            pedido.subtotal,
            pedido.delivery_fee,
            pedido.service_fee,
            pedido.total,
            pedido.commission_percent,
            pedido.commission_base_amount,
            pedido.commission_amount,
        ) == valores


def test_o_pedido_continua_ligado_ao_cliente_e_ao_restaurante(db, cenario):
    """O pedido não vira órfão: ele continua apontando para o fantasma.

    Soltar `customer_id` pareceria "mais anônimo" e quebraria o histórico do
    cliente, o relatório por cliente e o próprio resgate de cupom, que exige
    a linha existir.
    """
    anonimizar(db, cenario.cliente)

    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    db.refresh(pedido)
    assert pedido.customer_id == cenario.id_antigo
    assert pedido.restaurant_id == cenario.restaurante.id
    assert pedido.branch_id == cenario.filial.id


def test_a_redencao_do_cupom_sobrevive(db, cenario):
    """Apagar devolveria o cupom de uso único — exclusão viraria reciclagem
    de desconto."""
    anonimizar(db, cenario.cliente)

    resgate = db.get(CouponRedemption, cenario.resgate.id)
    assert resgate is not None
    assert resgate.customer_id == cenario.id_antigo
    assert resgate.status == "applied"


def test_o_extrato_de_cashback_sobrevive(db, cenario):
    """`customer_id` é `ON DELETE CASCADE`: um DELETE levaria o extrato
    inteiro junto, crédito não usado incluído. A anonimização não o toca."""
    anonimizar(db, cenario.cliente)

    lancamento = db.get(CashbackTransaction, cenario.cashback.id)
    assert lancamento is not None
    assert lancamento.amount == Decimal("7.50")
    assert lancamento.status == "available"


# ---------------------------------------------------------------------------
# O que TEM que mudar: a pessoa
# ---------------------------------------------------------------------------


def test_o_nome_e_o_telefone_somem_do_pedido(db, cenario):
    telefone_antigo = cenario.telefone_antigo

    anonimizar(db, cenario.cliente)

    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    db.refresh(pedido)
    assert pedido.customer_name_snapshot == ANONYMIZED_NAME
    assert telefone_antigo not in pedido.customer_phone_snapshot


def test_o_endereco_de_entrega_some_do_pedido_menos_bairro_e_cidade(db, cenario):
    anonimizar(db, cenario.cliente)

    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    db.refresh(pedido)
    assert pedido.address_street is None
    assert pedido.address_number is None
    assert pedido.address_complement is None
    assert pedido.address_reference is None
    assert pedido.address_zipcode is None
    assert pedido.delivery_latitude is None
    assert pedido.delivery_longitude is None

    # A decisão que este teste trava: bairro e cidade FICAM. Eles sustentam
    # "de onde vêm meus pedidos", e bairro não identifica ninguém sozinho.
    assert pedido.address_neighborhood == "Aldeota"
    assert pedido.address_city == "Fortaleza"
    assert pedido.address_state == "CE"
    # A distância também fica: é o que justifica a taxa cobrada, e é um raio,
    # não um ponto.
    assert pedido.delivery_distance_km == Decimal("3.20")


def test_o_recado_do_pedido_some(db, cenario):
    anonimizar(db, cenario.cliente)

    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    db.refresh(pedido)
    assert pedido.notes is None


def test_os_enderecos_salvos_somem_e_o_pedido_solta_o_vinculo(db, cenario):
    anonimizar(db, cenario.cliente)

    assert CustomerRepository(db).list_addresses(cenario.id_antigo) == []
    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    db.refresh(pedido)
    assert pedido.customer_address_id is None


def test_a_estimativa_de_entrega_some(db, cenario):
    """Cache de rota com a coordenada de casa. Não é histórico de nada."""
    id_estimativa = cenario.estimativa.id
    anonimizar(db, cenario.cliente)

    # O DELETE em massa nao avisa a identity map: sem isto o `get` devolve a
    # copia velha da sessao (e estoura no refresh) em vez de ir ao banco.
    db.expunge_all()
    assert db.get(DeliveryEstimate, id_estimativa) is None


def test_os_codigos_de_verificacao_somem(db, cenario):
    """Eles guardam o e-mail em TEXTO PURO numa segunda tabela — anonimizar
    `customers` sem apagá-los deixaria o endereço legível ao lado."""
    id_codigo = cenario.codigo.id
    anonimizar(db, cenario.cliente)

    db.expunge_all()
    assert db.get(EmailVerificationCode, id_codigo) is None


def test_a_conta_fica_marcada_como_anonimizada(db, cenario):
    """`is_active=false` sozinho não serve: ele já significa conta SUSPENSA,
    que é reversível e mantém os dados."""
    anonimizar(db, cenario.cliente)

    cliente = db.get(Customer, cenario.id_antigo)
    db.refresh(cliente)
    assert cliente.anonymized_at is not None
    assert cliente.is_active is False
    assert cliente.name == ANONYMIZED_NAME
    assert cliente.birth_date == date(1900, 1, 1)


def test_o_token_em_circulacao_morre(db, cenario):
    """`password_changed_at` é o marco de revogação — inclusive para o token
    que fez esta própria chamada. Não há lista de sessões para revogar uma a
    uma."""
    anonimizar(db, cenario.cliente)

    cliente = db.get(Customer, cenario.id_antigo)
    db.refresh(cliente)
    assert cliente.password_changed_at is not None


def test_o_login_antigo_para_de_funcionar(db, cenario):
    from src.schemas.auth_schema import LoginRequest

    email_antigo = cenario.email_antigo
    anonimizar(db, cenario.cliente)

    with pytest.raises(HTTPException) as erro:
        AuthService(db).login(LoginRequest(login=email_antigo, password=SENHA))
    # 401 e não 403 "conta inativa": o e-mail saiu da coluna, então para o
    # login essa conta simplesmente não existe.
    assert erro.value.status_code == 401


# ---------------------------------------------------------------------------
# O que fecha a decisão: recadastro liberado, link de acompanhamento vivo
# ---------------------------------------------------------------------------


def test_o_email_e_o_telefone_voltam_a_poder_ser_cadastrados(db, cenario):
    """O teste negativo que a decisão inteira existe para permitir."""
    email_antigo = cenario.email_antigo
    telefone_antigo = cenario.telefone_antigo
    id_antigo = cenario.id_antigo

    anonimizar(db, cenario.cliente)

    repositorio = CustomerRepository(db)
    assert repositorio.get_by_email(email_antigo) is None
    assert repositorio.get_by_phone(telefone_antigo) is None

    # E o cadastro novo passa de verdade, com id NOVO: a pessoa volta como
    # desconhecida, sem histórico, sem cashback, sem endereços.
    nova = repositorio.create(
        name="Maria",
        email=email_antigo,
        phone=telefone_antigo,
        password_hash=SENHA_HASH,
        birth_date=date(1990, 1, 1),
        is_active=True,
    )
    db.flush()
    assert nova.id != id_antigo


def test_a_conta_nova_nasce_sem_historico(db, cenario):
    email_antigo = cenario.email_antigo
    telefone_antigo = cenario.telefone_antigo

    anonimizar(db, cenario.cliente)

    repositorio = CustomerRepository(db)
    nova = repositorio.create(
        name="Maria",
        email=email_antigo,
        phone=telefone_antigo,
        password_hash=SENHA_HASH,
        birth_date=date(1990, 1, 1),
        is_active=True,
    )
    db.flush()

    from src.repositories.order_repository import OrderRepository

    assert OrderRepository(db).list_all_by_customer(nova.id) == []
    # E os dois pedidos antigos continuam com o restaurante, no fantasma.
    assert len(OrderRepository(db).list_all_by_customer(cenario.id_antigo)) == 2


def test_o_link_de_acompanhamento_continua_funcionando(db, cenario):
    """O pedido não é apagado e `tracking_token_hash` continua lá.

    Quem tiver o link antigo vê a NOTA DA VENDA — sem nome, telefone nem
    endereço. Foi a decisão tomada: o link não morre junto.
    """
    from src.services.order_service import OrderService

    slug = cenario.restaurante.slug
    token = cenario.token_do_pedido

    anonimizar(db, cenario.cliente)

    resposta = OrderService(db).get_order_by_tracking_token(slug, token)
    assert resposta.order_number == cenario.pedido_com_cupom.order_number
    assert resposta.customer_name_snapshot == ANONYMIZED_NAME


def test_duas_exclusoes_nao_colidem(db):
    """Pega a sentinela constante: `email` e `phone` são UNIQUE, e um valor
    fixo faria a segunda exclusão morrer na constraint."""
    primeiro = criar_cliente(db)
    primeiro.password_hash = SENHA_HASH
    segundo = criar_cliente(db)
    segundo.password_hash = SENHA_HASH
    db.flush()

    anonimizar(db, primeiro)
    anonimizar(db, segundo)

    db.refresh(primeiro)
    db.refresh(segundo)
    assert primeiro.email != segundo.email
    assert primeiro.phone != segundo.phone


# ---------------------------------------------------------------------------
# As recusas
# ---------------------------------------------------------------------------


def test_senha_errada_nao_apaga_nada(db, cenario):
    with pytest.raises(HTTPException) as erro:
        anonimizar(db, cenario.cliente, senha="senha-errada")
    assert erro.value.status_code == 401

    db.refresh(cenario.cliente)
    assert cenario.cliente.email == cenario.email_antigo
    assert cenario.cliente.anonymized_at is None
    assert CustomerRepository(db).list_addresses(cenario.id_antigo) != []
    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    db.refresh(pedido)
    assert pedido.customer_name_snapshot != ANONYMIZED_NAME


def test_pedido_em_andamento_bloqueia(db, cenario):
    """O caso que mais importa: comida a caminho.

    Anonimizar no meio tiraria o nome e o telefone de quem o entregador
    precisa achar.
    """
    em_curso = criar_pedido(
        db,
        cenario.restaurante,
        cenario.filial,
        cliente=cenario.cliente,
        status="preparing",
    )
    db.flush()

    with pytest.raises(HTTPException) as erro:
        anonimizar(db, cenario.cliente)

    assert erro.value.status_code == 409
    # O número vai no corpo porque a recusa é TEMPORÁRIA: sem ele o app só
    # consegue dizer "tente mais tarde".
    assert em_curso.order_number in erro.value.detail["orders_in_flight"]

    db.refresh(cenario.cliente)
    assert cenario.cliente.anonymized_at is None
    assert CustomerRepository(db).list_addresses(cenario.id_antigo) != []


def test_pedido_terminal_nao_bloqueia(db, cenario):
    """`cancelled` e `rejected` são terminais tanto quanto `completed` — se
    entrassem no bloqueio, um pedido recusado trancaria a conta para sempre."""
    criar_pedido(
        db, cenario.restaurante, cenario.filial, cliente=cenario.cliente, status="cancelled"
    )
    criar_pedido(
        db, cenario.restaurante, cenario.filial, cliente=cenario.cliente, status="rejected"
    )
    db.flush()

    anonimizar(db, cenario.cliente)

    db.refresh(cenario.cliente)
    assert cenario.cliente.anonymized_at is not None


def test_falha_no_meio_nao_deixa_estado_pela_metade(db, cenario, monkeypatch):
    """Sem este teste, "uma transação" é uma afirmação do docstring.

    A falha é injetada DEPOIS de os pedidos já terem sido anonimizados e
    ANTES do cliente — exatamente o estado que não pode sobrar: o lojista
    perde o histórico e a pessoa continua logada.
    """
    def explodir(self, customer):
        raise RuntimeError("falha injetada")

    monkeypatch.setattr(
        CustomerAnonymizationService, "_delete_delivery_estimates", explodir
    )

    with pytest.raises(RuntimeError):
        anonimizar(db, cenario.cliente)

    # Sem `rollback` aqui: quem o fez foi o próprio service, e é justamente
    # isso que este teste está conferindo. O rollback expira os objetos da
    # sessão, então o `get` abaixo relê do banco.
    cliente = db.get(Customer, cenario.id_antigo)
    assert cliente.email == cenario.email_antigo
    assert cliente.is_active is True
    assert cliente.anonymized_at is None

    pedido = db.get(Order, cenario.pedido_com_cupom.id)
    assert pedido.customer_name_snapshot != ANONYMIZED_NAME
    assert pedido.notes == "apartamento 302, falar com a Maria"


# ---------------------------------------------------------------------------
# A rota
# ---------------------------------------------------------------------------


def cliente_http(db, cliente_logado) -> TestClient:
    """App mínimo com o router de clientes. Não passa pelo `main.py`.

    O `limiter` e o `RateLimitStateMiddleware` vêm junto porque a rota é
    limitada: sem os dois, o wrapper do `@limiter.limit` lê
    `request.state.view_rate_limit` e estoura `AttributeError` — o 500 que o
    middleware existe para evitar.
    """
    app = FastAPI()
    app.state.limiter = limiter
    app.add_middleware(RateLimitStateMiddleware)
    app.include_router(rota_de_clientes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_customer] = lambda: cliente_logado
    return TestClient(app)


def test_a_rota_responde_204_e_anonimiza(db, cenario):
    http = cliente_http(db, cenario.cliente)

    resposta = http.request("DELETE", "/customers/me", json={"password": SENHA})

    assert resposta.status_code == 204
    assert resposta.content == b""
    db.refresh(cenario.cliente)
    assert cenario.cliente.anonymized_at is not None


def test_a_rota_responde_401_com_a_senha_errada(db, cenario):
    http = cliente_http(db, cenario.cliente)

    resposta = http.request("DELETE", "/customers/me", json={"password": "errada"})

    assert resposta.status_code == 401
    db.refresh(cenario.cliente)
    assert cenario.cliente.anonymized_at is None


def test_a_rota_responde_409_com_pedido_em_andamento(db, cenario):
    em_curso = criar_pedido(
        db,
        cenario.restaurante,
        cenario.filial,
        cliente=cenario.cliente,
        status="out_for_delivery",
    )
    db.flush()
    http = cliente_http(db, cenario.cliente)

    resposta = http.request("DELETE", "/customers/me", json={"password": SENHA})

    assert resposta.status_code == 409
    # O envelope `detail` é o formato que a rota entrega de verdade, e é o que
    # `OrdersInFlightResponse` anuncia no OpenAPI (armadilha 16).
    corpo = resposta.json()["detail"]
    assert em_curso.order_number in corpo["orders_in_flight"]


# ---------------------------------------------------------------------------
# O cashback perdido: o aviso e o rastro
# ---------------------------------------------------------------------------
#
# O saldo é perdido de verdade, e o teste logo acima
# (`test_o_extrato_de_cashback_sobrevive`) é o que o prova pelo outro lado: o
# lançamento continua `available`, mas ligado ao id VELHO. O recadastro nasce
# com id novo, então ninguém alcança aquele saldo nunca mais.
#
# Não há como a rota avisar antes — quando ela responde, já aconteceu, e não
# há desfazer. Quem avisa é o app, com `GET /customers/me/cashback` na tela de
# confirmação; o contrato diz isso no docstring da rota, que é o que sai no
# OpenAPI. O que fica DO LADO DE CÁ é o rastro no log, no mesmo espírito do
# `[Pagamento] pedido pago foi cancelled sem estorno` (armadilha 25).


def test_saldo_perdido_vira_warning_no_log(db, cenario, caplog):
    """O cenário tem R$ 7,50 `available`, e eles somem do alcance da pessoa."""
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        anonimizar(db, cenario.cliente)

    perdas = [
        registro.getMessage()
        for registro in caplog.records
        if "cashback perdido" in registro.getMessage()
    ]
    assert len(perdas) == 1
    assert "7.50" in perdas[0]
    assert str(cenario.id_antigo) in perdas[0]


def test_conta_sem_saldo_nao_gera_warning(db, cenario, caplog):
    """Só dinheiro parado entra no log.

    É a propriedade que faz o grep servir: um campo em toda exclusão
    obrigaria a filtrar os zeros para achar o caso que importa, e a exclusão
    sem saldo é o caso comum.
    """
    db.delete(cenario.cashback)
    db.flush()

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        anonimizar(db, cenario.cliente)

    perdas = [
        registro
        for registro in caplog.records
        if "cashback perdido" in registro.getMessage()
    ]
    assert perdas == []


def test_o_log_nao_leva_dado_pessoal(db, cenario, caplog):
    """`customer_id` é pseudônimo; nome, e-mail e telefone não entram.

    Mesma regra do resto do projeto, e ela vale COM MAIS FORÇA aqui: esta é
    a linha escrita no exato momento em que a pessoa pediu para sumir.
    """
    email_antigo = cenario.email_antigo
    telefone_antigo = cenario.telefone_antigo

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        anonimizar(db, cenario.cliente)

    lgpd = " ".join(
        registro.getMessage()
        for registro in caplog.records
        if "[LGPD]" in registro.getMessage()
    )
    assert email_antigo not in lgpd
    assert telefone_antigo not in lgpd
    assert "Maria" not in lgpd


def test_a_falha_no_meio_nao_deixa_o_warning_no_log(db, cenario, caplog, monkeypatch):
    """O saldo é lido ANTES da transação, mas só é logado DEPOIS do commit.

    Sem essa ordem, uma exclusão que falhou no meio deixaria no log a linha
    dizendo que a pessoa perdeu o cashback — e ela não perdeu, porque o
    rollback desfez tudo.
    """
    monkeypatch.setattr(
        CustomerAnonymizationService,
        "_delete_addresses",
        lambda self, cliente: (_ for _ in ()).throw(RuntimeError("falha forçada")),
    )

    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        with pytest.raises(RuntimeError):
            anonimizar(db, cenario.cliente)

    perdas = [
        registro
        for registro in caplog.records
        if "cashback perdido" in registro.getMessage()
    ]
    assert perdas == []


# ---------------------------------------------------------------------------
# Avaliação de pedido: o texto sai, a nota fica
# ---------------------------------------------------------------------------
#
# É a mesma linha que decide o resto deste arquivo — fica o que é da VENDA,
# sai o que é da PESSOA. A nota é número, não identifica ninguém e é o
# histórico de qualidade do restaurante: apagá-la reescreveria a média do
# lojista a cada exclusão de conta. O comentário é campo livre, e mistura
# "demorou" com "moro no 302, falar com a Maria", exatamente como
# `order.notes`.
#
# E isto alcança porque a avaliação chega por `orders.customer_id` — o que o
# `ai_feedback` não tinha. Só que alcança SÓ quem tem conta: o comentário do
# pedido de convidado tem `customer_id` nulo e sai pela retenção, coberta em
# `tests/test_retencao_de_avaliacao_db.py`.


def _avaliar(db, pedido, rating, comment):
    avaliacao = OrderReview(order_id=pedido.id, rating=rating, comment=comment)
    db.add(avaliacao)
    db.flush()
    return avaliacao


def test_o_comentario_da_avaliacao_some(db, cenario):
    avaliacao = _avaliar(
        db,
        cenario.pedido_com_cupom,
        rating=2,
        comment="moro no apartamento 302, falar com a Maria",
    )

    anonimizar(db, cenario.cliente)

    db.refresh(avaliacao)
    assert avaliacao.comment is None


def test_a_nota_da_avaliacao_fica(db, cenario):
    """O que o lojista não pode perder quando um cliente sai."""
    avaliacao = _avaliar(db, cenario.pedido_com_cupom, rating=2, comment="demorou")

    anonimizar(db, cenario.cliente)

    db.refresh(avaliacao)
    assert avaliacao.rating == 2
    assert db.get(OrderReview, avaliacao.id) is not None


def test_a_avaliacao_de_outra_pessoa_nao_e_tocada(db, cenario):
    """O `UPDATE` é escopado por `orders.customer_id`.

    Sem o escopo, uma exclusão de conta apagaria o comentário da base
    inteira — e ninguém veria erro nenhum.
    """
    vizinho = criar_cliente(db)
    pedido_do_vizinho = criar_pedido(
        db, cenario.restaurante, cenario.filial, cliente=vizinho, status="completed"
    )
    db.flush()
    alheia = _avaliar(db, pedido_do_vizinho, rating=5, comment="tudo certo")

    anonimizar(db, cenario.cliente)

    db.refresh(alheia)
    assert alheia.comment == "tudo certo"


def test_avaliacao_sem_comentario_nao_atrapalha(db, cenario):
    """O caso comum: nota alta, sem texto. Não pode virar erro."""
    avaliacao = _avaliar(db, cenario.pedido_com_cupom, rating=5, comment=None)

    anonimizar(db, cenario.cliente)

    db.refresh(avaliacao)
    assert avaliacao.rating == 5


# --- A identidade social ("entrar com Google") -------------------------------
#
# Dois estragos diferentes num passo só, e o segundo é o que se vê primeiro:
# o ponteiro para o cadastro da pessoa dentro do Google fica de pé, e quem
# excluiu a conta e volta pelo Google cai no caso "sub conhecido" — logado
# numa conta `is_active=False`, 403 para sempre e sem como se recadastrar.


def _ligar_google(db, cliente, sub: str = "sub-do-google-123"):
    return CustomerSocialIdentityRepository(db).create(
        customer_id=cliente.id,
        provider=SOCIAL_PROVIDER_GOOGLE,
        provider_user_id=sub,
    )


def test_a_identidade_social_some(db, cenario):
    identidade = _ligar_google(db, cenario.cliente)
    db.flush()
    id_identidade = identidade.id

    anonimizar(db, cenario.cliente)

    db.expunge_all()
    assert db.get(CustomerSocialIdentity, id_identidade) is None


def test_o_sub_fica_livre_para_uma_conta_nova(db, cenario):
    """A consequência prática de apagar: quem excluiu a conta e volta pelo
    Google é tratado como cliente novo.

    Com a identidade sobrevivendo, o `sub` continuaria apontando para a conta
    anonimizada — e `get_by_provider_user` a devolveria, `is_active=False`.
    """
    _ligar_google(db, cenario.cliente, sub="sub-que-volta")
    db.flush()

    anonimizar(db, cenario.cliente)

    db.expunge_all()
    encontrada = CustomerSocialIdentityRepository(db).get_by_provider_user(
        SOCIAL_PROVIDER_GOOGLE, "sub-que-volta"
    )
    assert encontrada is None


def test_a_identidade_de_outra_pessoa_nao_e_tocada(db, cenario):
    """O escopo do DELETE. Sem ele, uma exclusão de conta desligaria o Google
    da base inteira — e ninguém veria erro nenhum."""
    vizinho = criar_cliente(db)
    alheia = _ligar_google(db, vizinho, sub="sub-do-vizinho")
    db.flush()
    id_alheia = alheia.id

    anonimizar(db, cenario.cliente)

    db.expunge_all()
    assert db.get(CustomerSocialIdentity, id_alheia) is not None


# --- Excluir sem senha: o codigo no e-mail no lugar dela ---------------------
#
# A conta que entrou so pelo Google nao tem senha para mandar. O codigo prova
# a mesma coisa que a senha provaria — acesso a caixa de entrada.
#
# O que so aparece contra o banco: as TRES tabelas de codigo existem de
# verdade, e a consulta da exclusao nao enxerga a da verificacao. Na suite
# rapida isso e um dublê com dois campos; aqui sao dois SELECTs em duas
# tabelas.


class ServicoDeEmailFalso:
    """Colaborador externo. Guarda o codigo que sairia no e-mail."""

    def __init__(self):
        self.codigos = []

    def send_account_deletion_code(self, to_email, code):
        self.codigos.append((to_email, code))

    def send_email_verification_code(self, to_email, code):
        self.codigos.append((to_email, code))

    def send_password_reset_code(self, to_email, code):
        self.codigos.append((to_email, code))


def _cliente_sem_senha(db, cenario):
    """A conta do Google: `password_hash` inutilizavel, com o prefixo `!`."""
    cenario.cliente.password_hash = unusable_password_hash()
    db.flush()
    return cenario.cliente


def _servico_de_exclusao(db):
    servico = CustomerAnonymizationService(db)
    servico.email_service = ServicoDeEmailFalso()
    return servico


def test_a_conta_sem_senha_pede_codigo_e_ele_e_gravado_em_hmac(db, cenario):
    cliente = _cliente_sem_senha(db, cenario)
    servico = _servico_de_exclusao(db)

    servico.request_deletion_code(cliente)

    _, codigo = servico.email_service.codigos[-1]
    linha = db.scalar(
        select(AccountDeletionCode).where(AccountDeletionCode.customer_id == cliente.id)
    )
    assert linha is not None
    assert linha.code_hash != codigo
    assert linha.code_hash == hash_verification_code(codigo)


def test_o_codigo_certo_apaga_a_conta_sem_senha(db, cenario):
    cliente = _cliente_sem_senha(db, cenario)
    servico = _servico_de_exclusao(db)
    servico.request_deletion_code(cliente)
    _, codigo = servico.email_service.codigos[-1]

    _servico_de_exclusao(db).anonymize(
        cliente, DeleteCustomerAccountRequest(email_code=codigo)
    )

    db.refresh(cliente)
    assert cliente.anonymized_at is not None
    assert cliente.email == anonymized_email(cliente.id)


def test_a_linha_do_codigo_some_junto_com_a_conta(db, cenario):
    """Ela guarda o e-mail em TEXTO PURO — e e a linha que autorizou a
    exclusao. Sobreviver a ela seria deixar o endereco de quem pediu para
    sumir legivel ao lado."""
    cliente = _cliente_sem_senha(db, cenario)
    servico = _servico_de_exclusao(db)
    servico.request_deletion_code(cliente)
    _, codigo = servico.email_service.codigos[-1]

    _servico_de_exclusao(db).anonymize(
        cliente, DeleteCustomerAccountRequest(email_code=codigo)
    )

    db.expunge_all()
    quantas = db.scalar(
        select(func.count())
        .select_from(AccountDeletionCode)
        .where(AccountDeletionCode.customer_id == cliente.id)
    )
    assert quantas == 0


def test_o_codigo_de_verificacao_nao_apaga_a_conta(db, cenario):
    """A REGRA QUE JA MORDEU, contra o banco de verdade.

    Ha uma linha valida em `email_verification_codes` e NENHUMA em
    `account_deletion_codes`. O codigo daquela nao serve aqui — e a separacao
    e o schema, nao um `if`.
    """
    cliente = _cliente_sem_senha(db, cenario)
    codigo = "123456"
    CustomerRepository(db).create_email_code(
        customer_id=cliente.id,
        email=cliente.email,
        code_hash=hash_verification_code(codigo),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        attempts_count=0,
        resend_count=0,
    )
    db.flush()

    with pytest.raises(HTTPException) as erro:
        _servico_de_exclusao(db).anonymize(
            cliente, DeleteCustomerAccountRequest(email_code=codigo)
        )

    assert erro.value.status_code == 401
    db.refresh(cliente)
    assert cliente.anonymized_at is None


def test_o_codigo_de_exclusao_nao_verifica_o_email(db, cenario):
    """A outra direcao da mesma regra. `verify_email_code` le a outra tabela e
    nao enxerga a linha de exclusao."""
    cliente = _cliente_sem_senha(db, cenario)
    cliente.email_verified_at = None
    db.flush()
    servico = _servico_de_exclusao(db)
    servico.request_deletion_code(cliente)
    _, codigo = servico.email_service.codigos[-1]

    auth = AuthService(db)
    auth.email_service = ServicoDeEmailFalso()
    with pytest.raises(HTTPException) as erro:
        auth.verify_email_code(
            VerifyEmailCodeRequest(email=cliente.email, code=codigo)
        )

    assert erro.value.status_code == 400
    db.refresh(cliente)
    assert cliente.email_verified_at is None


def test_a_conta_com_senha_nao_ganha_o_segundo_caminho(db, cenario):
    """Ela ja tem prova. Emitir codigo abriria uma exclusao a mais onde uma
    bastava — quem tivesse o token e a caixa de entrada apagaria a conta sem
    saber a senha."""
    servico = _servico_de_exclusao(db)

    with pytest.raises(HTTPException) as erro:
        servico.request_deletion_code(cenario.cliente)

    assert erro.value.status_code == 400
    assert servico.email_service.codigos == []


def test_a_conta_com_senha_recusa_o_codigo_na_exclusao(db, cenario):
    with pytest.raises(HTTPException) as erro:
        _servico_de_exclusao(db).anonymize(
            cenario.cliente, DeleteCustomerAccountRequest(email_code="123456")
        )

    assert erro.value.status_code == 400
    db.refresh(cenario.cliente)
    assert cenario.cliente.anonymized_at is None
