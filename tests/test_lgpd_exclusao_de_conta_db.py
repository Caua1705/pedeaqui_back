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

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.dependencies.customer_auth import get_current_customer
from src.api.dependencies.database import get_db
from src.api.endpoints import customers as rota_de_clientes
from src.api.middleware.rate_limit_state import RateLimitStateMiddleware
from src.api.rate_limit import limiter
from src.models.cashback_transaction_model import CashbackTransaction
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.coupon_redemption_model import CouponRedemption
from src.models.customer_model import Customer, EmailVerificationCode
from src.models.delivery_estimate_model import DeliveryEstimate
from src.models.order_model import Order
from src.repositories.customer_repository import CustomerRepository
from src.services.auth_service import AuthService
from src.services.customer_anonymization_service import (
    ANONYMIZED_NAME,
    CustomerAnonymizationService,
)
from src.utils.security import hash_password
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
    CustomerAnonymizationService(db).anonymize(cliente, senha)


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
