"""Caracterizacao de `services/customer_service.py` — o lado do PERFIL.

TESTE DE CARACTERIZACAO: descreve o que o codigo faz HOJE. Comportamento
esquisito fica registrado e verde, com comentario apontando o problema.

O lado dos ENDERECOS ja tem rede em `test_customer_addresses.py`, e ela nao e
repetida aqui. O que estava sem teste nenhum e o perfil: `update_me`,
`change_password` e `list_orders` — as tres coisas que o cliente mexe na conta
dele.

`change_password` e a mais delicada das tres: ela mexe em `password_changed_at`,
que INVALIDA todos os tokens ja emitidos. Um retorno cedo colocado no lugar
errado ali deixa de expulsar quem invadiu a conta — que e a unica ferramenta
que o cliente tem contra isso.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from src.schemas.cashback_schema import CashbackTransactionsResponse
from src.schemas.customer_schema import (
    ChangeCustomerPasswordRequest,
    CurrentCustomerResponse,
    CustomerDataExportResponse,
    UpdateCurrentCustomerRequest,
)
from src.services.customer_service import CustomerService
from src.utils.security import hash_password, verify_password


SENHA_ATUAL = "senha-atual-123"

# Hasheado uma vez por modulo: o bcrypt leva ~0,3s de proposito, e um hash
# por `make_customer` cobraria isso em cada teste.
SENHA_ATUAL_HASH = hash_password(SENHA_ATUAL)


class FakeDb:
    def __init__(self, falha=None):
        self.events = []
        self.falha = falha

    def add(self, value):
        self.events.append("add")

    def commit(self):
        if self.falha is not None:
            self.events.append("commit-falhou")
            raise self.falha
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def refresh(self, value):
        self.events.append("refresh")


class FakeCustomerRepository:
    """Dublê do repositorio de cliente.

    Os parametros `*_apos_a_corrida` existem para o teste do `except
    IntegrityError`: a conferencia de cima le o banco ANTES do commit e a
    releitura de dentro do except le DEPOIS. Um dublê que devolvesse o mesmo
    nas duas pararia na conferencia de cima, e o teste do except passaria sem
    nunca entrar nele.
    """

    def __init__(
        self,
        by_email=None,
        by_phone=None,
        by_email_apos_a_corrida=None,
        by_phone_apos_a_corrida=None,
    ):
        self.by_email = by_email
        self.by_phone = by_phone
        self.by_email_apos_a_corrida = by_email_apos_a_corrida
        self.by_phone_apos_a_corrida = by_phone_apos_a_corrida
        self.updated_with = None
        self.leituras_de_email = 0
        self.leituras_de_telefone = 0

    def get_by_email(self, email):
        self.leituras_de_email += 1
        if self.leituras_de_email > 1 and self.by_email_apos_a_corrida is not None:
            return self.by_email_apos_a_corrida
        return self.by_email

    def get_by_phone(self, phone):
        self.leituras_de_telefone += 1
        if self.leituras_de_telefone > 1 and self.by_phone_apos_a_corrida is not None:
            return self.by_phone_apos_a_corrida
        return self.by_phone

    def update(self, customer, **values):
        self.updated_with = values
        for key, value in values.items():
            setattr(customer, key, value)
        return customer


class FakeOrderRepository:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def list_orders_by_customer(self, customer_id):
        return self.rows


class FakeOrderReviewRepository:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.pedido_para = None

    def list_by_customer(self, customer_id):
        self.pedido_para = customer_id
        return self.rows


def make_customer(password=SENHA_ATUAL, **overrides):
    valores = {
        "id": uuid.uuid4(),
        "name": "Joana Souza",
        "email": "joana@exemplo.com",
        "phone": "85999998888",
        "birth_date": date(1990, 5, 20),
        "email_verified_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "marketing_opt_in": True,
        "password_hash": (SENHA_ATUAL_HASH if password == SENHA_ATUAL else hash_password(password)) if password else None,
        "password_changed_at": None,
    }
    valores.update(overrides)
    return SimpleNamespace(**valores)


def make_service(
    db=None,
    customer_repository=None,
    order_repository=None,
    order_review_repository=None,
):
    service = CustomerService.__new__(CustomerService)
    service.db = db or FakeDb()
    service.customer_repository = customer_repository or FakeCustomerRepository()
    service.order_repository = order_repository or FakeOrderRepository()
    service.order_review_repository = (
        order_review_repository or FakeOrderReviewRepository()
    )
    return service


def integrity_error():
    return IntegrityError("INSERT", {}, Exception("duplicate key"))


# ---------------------------------------------------------------------------
# get_me
# ---------------------------------------------------------------------------


class TestGetMe:
    def test_it_copies_the_fields_the_client_may_see(self):
        response = make_service().get_me(make_customer())

        assert response.name == "Joana Souza"
        assert response.email == "joana@exemplo.com"

    def test_email_verified_is_derived_from_the_timestamp(self):
        """A resposta publica nao expoe QUANDO o e-mail foi verificado, so se
        foi. O timestamp fica no banco."""
        assert make_service().get_me(make_customer()).email_verified is True
        assert make_service().get_me(make_customer(email_verified_at=None)).email_verified is False


# ---------------------------------------------------------------------------
# export_me
# ---------------------------------------------------------------------------


class FakeCashbackService:
    """Dublê do CashbackService, guardando com que limite foi chamado."""

    def __init__(self, db=None):
        FakeCashbackService.chamado_com = None

    def list_transactions(self, customer, limit, offset):
        FakeCashbackService.chamado_com = (customer, limit, offset)
        return CashbackTransactionsResponse(balance=0.0, transactions=[])


class TestExportMe:
    def _service(self, monkeypatch, **kwargs):
        import src.services.customer_service as modulo

        monkeypatch.setattr(modulo, "CashbackService", FakeCashbackService)
        return make_service(**kwargs)

    def test_it_gathers_the_five_blocks(self, monkeypatch):
        """Direito de acesso e portabilidade (Art. 18, II e V). O pacote e
        montagem do que ja saia em rotas separadas.

        As avaliacoes entraram como quinto bloco: sao dado DELA, e sem elas
        o direito de acesso ficaria incompleto justamente no campo de texto
        livre — que e o que a exclusao de conta depois apaga."""
        service = self._service(monkeypatch)
        service.list_addresses = lambda customer: []
        service.list_orders = lambda customer: []

        export = service.export_me(make_customer())

        assert export.profile.email == "joana@exemplo.com"
        assert export.addresses == []
        assert export.orders == []
        assert export.cashback.transactions == []
        assert export.reviews == []
        assert export.exported_at is not None

    def test_it_exports_only_the_holder_of_the_token(self, monkeypatch):
        """O escopo e a unica coisa que separa "meus dados" de "a base". Todo
        bloco tem que ser buscado com o cliente que veio do token — e nao ha
        parametro de cliente na rota, de proposito."""
        customer = make_customer()
        pedidos_para = []

        service = self._service(monkeypatch)
        service.list_addresses = lambda c: pedidos_para.append(("addresses", c.id)) or []
        service.list_orders = lambda c: pedidos_para.append(("orders", c.id)) or []

        service.export_me(customer)

        assert pedidos_para == [("addresses", customer.id), ("orders", customer.id)]
        assert FakeCashbackService.chamado_com[0] is customer
        assert service.order_review_repository.pedido_para == customer.id

    def test_the_password_hash_never_leaves(self):
        """Credencial nao e dado do titular, e exportar hash de senha e
        entregar o material de um ataque offline."""
        assert "password_hash" not in CustomerDataExportResponse.model_fields
        assert "password_hash" not in CurrentCustomerResponse.model_fields


# ---------------------------------------------------------------------------
# update_me
# ---------------------------------------------------------------------------


def make_update_payload(**overrides):
    valores = {
        "name": "Joana Souza",
        "email": "joana@exemplo.com",
        "phone": "85999998888",
        "birth_date": date(1990, 5, 20),
    }
    valores.update(overrides)
    return UpdateCurrentCustomerRequest(**valores)


class TestUpdateMe:
    def test_it_saves_and_returns_the_new_profile(self):
        db = FakeDb()
        service = make_service(db=db)
        customer = make_customer()

        response = service.update_me(customer, make_update_payload(name="Joana S. Souza"))

        assert response.name == "Joana S. Souza"
        assert "commit" in db.events

    def test_the_marketing_consent_can_be_revoked(self):
        """Era o unico consentimento sem volta: coletado no cadastro, e o
        `extra="forbid"` do schema impedia qualquer alteracao depois. O
        cliente marcava a caixa uma vez e nao tinha, em lugar nenhum do
        produto, como desmarcar."""
        customer = make_customer()
        customer.marketing_opt_in = True
        service = make_service()

        response = service.update_me(
            customer, make_update_payload(marketing_opt_in=False)
        )

        assert response.marketing_opt_in is False

    def test_the_consent_can_be_given_again(self):
        customer = make_customer()
        customer.marketing_opt_in = False

        response = make_service().update_me(
            customer, make_update_payload(marketing_opt_in=True)
        )

        assert response.marketing_opt_in is True

    def test_a_payload_without_the_field_does_not_touch_the_consent(self):
        """Ausente e "nao mexi", `false` e "revoguei". Sem a distincao, quem
        editasse so o telefone revogaria o proprio opt-in sem ter pedido — e
        e exatamente o que o painel e o app instalados mandam hoje, que nao
        conhecem o campo."""
        repository = FakeCustomerRepository()
        customer = make_customer()
        customer.marketing_opt_in = True

        service = make_service(customer_repository=repository)
        service.update_me(customer, make_update_payload(phone="85988887777"))

        assert "marketing_opt_in" not in repository.updated_with
        assert customer.marketing_opt_in is True

    def test_an_email_owned_by_someone_else_is_409(self):
        outro = make_customer()
        service = make_service(customer_repository=FakeCustomerRepository(by_email=outro))

        with pytest.raises(HTTPException) as exc:
            service.update_me(make_customer(), make_update_payload())

        assert exc.value.status_code == 409
        assert "E-mail" in exc.value.detail

    def test_a_phone_owned_by_someone_else_is_409(self):
        outro = make_customer()
        service = make_service(customer_repository=FakeCustomerRepository(by_phone=outro))

        with pytest.raises(HTTPException) as exc:
            service.update_me(make_customer(), make_update_payload())

        assert exc.value.status_code == 409
        assert "Telefone" in exc.value.detail

    def test_my_own_email_is_not_a_conflict(self):
        """A conferencia e `owner.id != customer.id`. Sem isso, salvar o
        perfil sem trocar o e-mail responderia "e-mail ja esta em uso" — o
        proprio dono conflitando consigo mesmo."""
        customer = make_customer()
        service = make_service(customer_repository=FakeCustomerRepository(by_email=customer, by_phone=customer))

        assert service.update_me(customer, make_update_payload()).email == "joana@exemplo.com"

    def test_a_race_on_commit_becomes_409_with_the_guilty_field(self):
        """A corrida que as duas conferencias acima nao pegam: alguem gravou o
        mesmo e-mail entre a leitura e o commit. O `except IntegrityError`
        RELE o banco para dizer qual campo colidiu.

        O dublê tem que responder DIFERENTE nas duas leituras — livre na
        conferencia de cima, ocupado na releitura de dentro do except. Um fake
        que devolvesse o dono desde o inicio pararia na conferencia de cima e
        o teste passaria sem nunca entrar no `except`.
        """
        repository = FakeCustomerRepository(by_email_apos_a_corrida=make_customer())
        service = make_service(db=FakeDb(falha=integrity_error()), customer_repository=repository)

        with pytest.raises(HTTPException) as exc:
            service.update_me(make_customer(), make_update_payload())

        assert exc.value.status_code == 409
        assert exc.value.detail == "E-mail já está em uso"

    def test_an_integrity_error_from_neither_field_is_not_blamed_on_the_phone(self):
        """O `else` do tratamento era "entao foi o telefone" — por eliminacao,
        sem conferir. Qualquer IntegrityError que nao fosse de e-mail (uma FK,
        um CHECK, uma constraint nova) respondia "Telefone já está em uso".

        O cliente ia corrigir um campo que estava certo, e o erro de verdade
        nunca aparecia como erro em lugar nenhum. Agora o IntegrityError sobe:
        vira 500 com a causa no log, que e o que faz o defeito ser visto.
        """
        # Nenhum dos dois campos tem dono: o conflito veio de outra coisa.
        service = make_service(
            db=FakeDb(falha=integrity_error()),
            customer_repository=FakeCustomerRepository(),
        )

        with pytest.raises(IntegrityError):
            service.update_me(make_customer(), make_update_payload())

    def test_a_race_on_the_phone_is_named_after_checking_it(self):
        """O caminho legitimo continua, e agora por CONFERENCIA e nao por
        eliminacao: o telefone so leva a culpa quando ele realmente tem outro
        dono na releitura."""
        repository = FakeCustomerRepository(by_phone_apos_a_corrida=make_customer())
        service = make_service(
            db=FakeDb(falha=integrity_error()), customer_repository=repository
        )

        with pytest.raises(HTTPException) as exc:
            service.update_me(make_customer(), make_update_payload())

        assert exc.value.status_code == 409
        assert exc.value.detail == "Telefone já está em uso"
        # A prova de que passou pelo except: a segunda leitura aconteceu.
        assert repository.leituras_de_telefone == 2

    def test_any_other_failure_rolls_back_and_propagates(self):
        db = FakeDb(falha=RuntimeError("banco caiu"))
        service = make_service(db=db)

        with pytest.raises(RuntimeError):
            service.update_me(make_customer(), make_update_payload())

        assert "rollback" in db.events


# ---------------------------------------------------------------------------
# change_password
# ---------------------------------------------------------------------------


def make_password_payload(current=SENHA_ATUAL, new="senha-nova-123", confirm=None):
    return ChangeCustomerPasswordRequest(
        current_password=current,
        new_password=new,
        confirm_password=new if confirm is None else confirm,
    )


class TestChangePassword:
    def test_it_replaces_the_hash_and_stamps_the_change(self):
        """O `password_changed_at` e o que invalida os tokens ja emitidos —
        inclusive o do proprio aparelho que trocou a senha. E o preco de nao
        manter lista de sessoes, e e a unica ferramenta do cliente para
        expulsar quem entrou na conta dele."""
        db = FakeDb()
        customer = make_customer()
        service = make_service(db=db)

        response = service.change_password(customer, make_password_payload())

        assert verify_password("senha-nova-123", customer.password_hash) is True
        assert customer.password_changed_at is not None
        assert "commit" in db.events
        assert "sucesso" in response.message

    def test_the_wrong_current_password_is_400(self):
        with pytest.raises(HTTPException) as exc:
            make_service().change_password(make_customer(), make_password_payload(current="errada"))

        assert exc.value.status_code == 400
        assert exc.value.detail == "Senha atual incorreta"

    def test_a_confirmation_that_does_not_match_is_400(self):
        with pytest.raises(HTTPException) as exc:
            make_service().change_password(
                make_customer(), make_password_payload(new="senha-nova-123", confirm="outra-coisa")
            )

        assert exc.value.status_code == 400

    def test_a_new_password_under_eight_characters_is_400(self):
        with pytest.raises(HTTPException) as exc:
            make_service().change_password(make_customer(), make_password_payload(new="curta12"))

        assert exc.value.status_code == 400

    def test_exactly_eight_characters_is_accepted(self):
        """A fronteira do `< 8`. Registrada porque e o que uma reescrita troca
        por `<= 8` sem ninguem notar."""
        customer = make_customer()

        make_service().change_password(customer, make_password_payload(new="12345678"))

        assert verify_password("12345678", customer.password_hash) is True

    def test_a_password_over_seventy_two_bytes_is_400_not_500(self):
        """`PasswordTooLongError` do bcrypt vira 400 com mensagem propria. Sem
        esse tratamento, uma senha longa derrubaria a rota."""
        with pytest.raises(HTTPException) as exc:
            make_service().change_password(make_customer(), make_password_payload(new="x" * 73))

        assert exc.value.status_code == 400
        assert "longa" in exc.value.detail

    def test_the_checks_run_before_anything_is_written(self):
        """Nenhuma das quatro recusas chega a commitar."""
        db = FakeDb()
        service = make_service(db=db)

        with pytest.raises(HTTPException):
            service.change_password(make_customer(), make_password_payload(current="errada"))

        assert db.events == []

    def test_a_customer_without_a_password_cannot_change_it(self):
        """Conta sem senha gravada: `verify_password` devolve False e a troca
        para em "senha atual incorreta". O caminho para essa conta e o de
        recuperacao, nao este."""
        with pytest.raises(HTTPException) as exc:
            make_service().change_password(make_customer(password=None), make_password_payload())

        assert exc.value.detail == "Senha atual incorreta"

    def test_a_failure_on_commit_rolls_back(self):
        db = FakeDb(falha=RuntimeError("banco caiu"))
        service = make_service(db=db)

        with pytest.raises(RuntimeError):
            service.change_password(make_customer(), make_password_payload())

        assert "rollback" in db.events


# ---------------------------------------------------------------------------
# Enderecos: so os caminhos de ERRO
#
# O comportamento normal (primeiro endereco vira padrao, importacao
# idempotente, dono de outro cliente) esta em `test_customer_addresses.py`. O
# que falta rede sao os `except` — e sao eles que uma refatoracao de retorno
# cedo mexe de lugar.
# ---------------------------------------------------------------------------


class FakeAddressRepository:
    def __init__(self, addresses=()):
        self.addresses = list(addresses)
        self.unset_calls = 0

    def lock_customer(self, customer_id):
        return SimpleNamespace(id=customer_id)

    def list_addresses(self, customer_id):
        return [a for a in self.addresses if a.customer_id == customer_id]

    def get_address(self, customer_id, address_id):
        return next(
            (a for a in self.addresses if a.id == address_id and a.customer_id == customer_id),
            None,
        )

    def unset_default_addresses(self, customer_id):
        self.unset_calls += 1
        for address in self.list_addresses(customer_id):
            address.is_default = False


def make_address(customer_id, is_default=False, **overrides):
    valores = {
        "id": uuid.uuid4(),
        "customer_id": customer_id,
        "street": "Rua das Flores",
        "number": "123",
        "neighborhood": "Aldeota",
        "city": "Fortaleza",
        "state": "CE",
        "zipcode": "60150000",
        "client_reference": None,
        "is_default": is_default,
    }
    valores.update(overrides)
    return SimpleNamespace(**valores)


class TestAddressErrorPaths:
    def test_an_address_that_is_not_mine_is_404(self):
        customer = make_customer()
        service = make_service(customer_repository=FakeAddressRepository())

        with pytest.raises(HTTPException) as exc:
            service.set_default_address(customer, uuid.uuid4())

        assert exc.value.status_code == 404

    def test_setting_the_default_unsets_the_previous_one(self):
        customer = make_customer()
        antigo = make_address(customer.id, is_default=True)
        novo = make_address(customer.id)
        repository = FakeAddressRepository([antigo, novo])
        service = make_service(customer_repository=repository)

        resultado = service.set_default_address(customer, novo.id)

        assert repository.unset_calls == 1
        assert resultado.is_default is True

    def test_a_conflict_setting_the_default_is_409(self):
        customer = make_customer()
        endereco = make_address(customer.id)
        service = make_service(
            db=FakeDb(falha=integrity_error()),
            customer_repository=FakeAddressRepository([endereco]),
        )

        with pytest.raises(HTTPException) as exc:
            service.set_default_address(customer, endereco.id)

        assert exc.value.status_code == 409

    def test_blanking_a_required_field_on_update_is_400(self):
        """`street`, `number` e `neighborhood` nao podem ser apagados por um
        PATCH. Sem isso, um endereco salvo viraria incompleto e o calculo de
        frete perderia o ponto de partida."""
        from src.schemas.customer_schema import UpdateCustomerAddressRequest

        customer = make_customer()
        endereco = make_address(customer.id)
        service = make_service(customer_repository=FakeAddressRepository([endereco]))

        with pytest.raises(HTTPException) as exc:
            service.update_address(customer, endereco.id, UpdateCustomerAddressRequest(street=""))

        assert exc.value.status_code == 400
        assert exc.value.detail == "Endereço incompleto"

    def test_an_update_that_does_not_touch_the_required_fields_goes_through(self):
        from src.schemas.customer_schema import UpdateCustomerAddressRequest

        customer = make_customer()
        endereco = make_address(customer.id)
        service = make_service(customer_repository=FakeAddressRepository([endereco]))

        resultado = service.update_address(
            customer, endereco.id, UpdateCustomerAddressRequest(complement="Apto 402")
        )

        assert resultado.complement == "Apto 402"
        # Campo ausente do corpo nao e tocado (`exclude_unset`).
        assert resultado.street == "Rua das Flores"

    def test_a_conflict_on_update_is_409(self):
        from src.schemas.customer_schema import UpdateCustomerAddressRequest

        customer = make_customer()
        endereco = make_address(customer.id)
        service = make_service(
            db=FakeDb(falha=integrity_error()),
            customer_repository=FakeAddressRepository([endereco]),
        )

        with pytest.raises(HTTPException) as exc:
            service.update_address(customer, endereco.id, UpdateCustomerAddressRequest(complement="Apto 402"))

        assert exc.value.status_code == 409

    def test_list_addresses_just_forwards_to_the_repository(self):
        customer = make_customer()
        endereco = make_address(customer.id)
        service = make_service(customer_repository=FakeAddressRepository([endereco]))

        assert service.list_addresses(customer) == [endereco]


class TestAddressFingerprint:
    def test_accents_case_and_spacing_do_not_change_the_fingerprint(self):
        """E o que faz a importacao ser idempotente para quem digitou o mesmo
        endereco de dois jeitos. Mesma dualidade NFC/NFD da armadilha 31, aqui
        resolvida por NFKD + descarte dos combinantes."""
        a = make_address(uuid.uuid4(), street="Avenida  Santos Dumont", neighborhood="Aldeota")
        b = make_address(uuid.uuid4(), street="AVENIDA SANTOS DUMONT", neighborhood="Aldeóta")

        assert CustomerService._address_fingerprint(a) == CustomerService._address_fingerprint(b)

    def test_a_different_number_is_a_different_address(self):
        a = make_address(uuid.uuid4(), number="123")
        b = make_address(uuid.uuid4(), number="124")

        assert CustomerService._address_fingerprint(a) != CustomerService._address_fingerprint(b)

    def test_a_missing_field_becomes_an_empty_slot_not_a_crash(self):
        assert CustomerService._normalize_address_value(None) == ""


# ---------------------------------------------------------------------------
# list_orders
# ---------------------------------------------------------------------------


def make_order_row(order_number=1042, itens=(), **overrides):
    valores = {
        "id": uuid.uuid4(),
        "order_number": order_number,
        "status": "completed",
        "order_type": "delivery",
        "subtotal": Decimal("124.00"),
        "delivery_fee": Decimal("7.00"),
        "service_fee": Decimal("0.00"),
        "coupon_code_snapshot": None,
        "coupon_discount_amount": Decimal("0.00"),
        "cashback_redeemed_amount": Decimal("0.00"),
        "discount_total": Decimal("0.00"),
        "total": Decimal("131.00"),
        "created_at": datetime(2026, 8, 11, 20, 41, tzinfo=timezone.utc),
        "items": list(itens),
    }
    valores.update(overrides)
    return (SimpleNamespace(**valores), "Pizzaria do Ze", "Aldeota")


def make_order_item(name="Pizza Calabresa", unit_price="45.00", quantity=1):
    return SimpleNamespace(
        id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_code_snapshot="P1",
        product_name_snapshot=name,
        product_description_snapshot=None,
        unit_price_snapshot=Decimal(unit_price),
        quantity=quantity,
        observation=None,
        total=Decimal(unit_price) * quantity,
        created_at=datetime(2026, 8, 11, 20, 41, tzinfo=timezone.utc),
    )


class TestListOrders:
    def test_no_orders_is_an_empty_list(self):
        assert make_service().list_orders(make_customer()) == []

    def test_the_restaurant_and_branch_names_come_from_the_row_not_the_order(self):
        """A consulta devolve uma tupla `(pedido, nome_do_restaurante,
        nome_da_filial)` — os dois nomes sao da juncao, e nao campos do
        pedido."""
        service = make_service(order_repository=FakeOrderRepository([make_order_row()]))

        pedido = service.list_orders(make_customer())[0]

        assert pedido.restaurant_name == "Pizzaria do Ze"
        assert pedido.branch_name == "Aldeota"

    def test_money_comes_out_as_float_and_discounts_stay_decimal(self):
        """ESQUISITO, e registrado como esta.

        O mesmo item de historico mistura os dois tipos: `subtotal`,
        `delivery_fee`, `service_fee` e `total` passam por `money_to_float`,
        enquanto `coupon_discount_amount`, `cashback_redeemed_amount` e
        `discount_total` saem como `Decimal` via `quantize_money`.

        Nao e bug de valor — o schema declara os tipos assim e a serializacao
        resolve. Mas e uma inconsistencia dentro da MESMA resposta, e quem
        somar os campos em Python vai encontrar float com Decimal, que o
        Python recusa.

        NAO corrigir aqui foi decisao tomada, e nao esquecimento. Este mesmo
        par existe em `CreateOrderResponse` e `OrderDetailResponse`, e as duas
        saidas mudam o formato de fio: tudo float tira as casas fixas dos tres
        descontos, tudo Decimal transforma quatro campos que hoje sao NUMERO
        em string. Consertar uma resposta so deixaria o mesmo campo com tipos
        diferentes em rotas diferentes — pior que a inconsistencia atual.

        Vira uma decisao unica sobre a API inteira, junto com o app do
        cliente. Ver a armadilha 34 da skill.
        """
        service = make_service(order_repository=FakeOrderRepository([make_order_row()]))

        pedido = service.list_orders(make_customer())[0]

        assert isinstance(pedido.total, float)
        assert isinstance(pedido.discount_total, Decimal)
        with pytest.raises(TypeError):
            pedido.total + pedido.discount_total

    def test_the_items_of_the_order_come_along(self):
        row = make_order_row(itens=[make_order_item("Pizza"), make_order_item("Coca")])
        service = make_service(order_repository=FakeOrderRepository([row]))

        pedido = service.list_orders(make_customer())[0]

        assert [item.product_name_snapshot for item in pedido.items] == ["Pizza", "Coca"]

    def test_the_unit_price_is_not_recomputed_from_the_options(self):
        """`unit_price_snapshot` ja vem do backend COM os adicionais somados
        (armadilha 2). O historico so o converte para float — se alguem somar
        adicional de novo aqui, o cliente ve um valor maior do que pagou."""
        item = make_order_item(unit_price="45.00", quantity=2)
        service = make_service(order_repository=FakeOrderRepository([make_order_row(itens=[item])]))

        resposta = service.list_orders(make_customer())[0].items[0]

        assert resposta.unit_price_snapshot == 45.00
        assert resposta.total == 90.00

    def test_a_coupon_code_survives_as_a_snapshot(self):
        row = make_order_row(coupon_code_snapshot="BEMVINDO", coupon_discount_amount=Decimal("10.00"))
        service = make_service(order_repository=FakeOrderRepository([row]))

        pedido = service.list_orders(make_customer())[0]

        assert pedido.coupon_code == "BEMVINDO"
        assert pedido.coupon_discount_amount == Decimal("10.00")
