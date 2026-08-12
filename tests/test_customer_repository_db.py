"""`CustomerRepository` contra o Postgres.

Testes de caracterização: descrevem o que a consulta faz **hoje**. Uma falha
aqui é bug encontrado, não teste errado.

O que os fakes em memória não alcançavam e por isso está aqui: a ordenação de
endereço (duas colunas, uma delas booleana), o `>=` da janela de reenvio, o
`ORDER BY ... LIMIT 1` do último código e o `with_for_update`, que só existe
quando há banco do outro lado.
"""

from datetime import timedelta

import pytest

from src.models.customer_model import EmailVerificationCode, PasswordResetCode
from src.repositories.customer_repository import CustomerRepository
from tests.fabricas_db import criar_cliente, criar_endereco, instante


pytestmark = pytest.mark.db


class TestAsBuscasPorIdentificador:
    def test_encontra_por_telefone_email_e_cpf(self, db):
        cliente = criar_cliente(
            db,
            email="joana@exemplo.com",
            phone="85988887777",
            cpf="12345678901",
        )

        repositorio = CustomerRepository(db)

        assert repositorio.get_by_email("joana@exemplo.com").id == cliente.id
        assert repositorio.get_by_phone("85988887777").id == cliente.id
        assert repositorio.get_by_cpf("12345678901").id == cliente.id

    def test_a_comparacao_e_exata_e_nao_normaliza_nada(self, db):
        """Documenta a divisão de trabalho: normalizar é do service
        (`normalize_email`, `normalize_digits`), e o repositório compara o que
        recebeu. Quem chamar daqui com o valor cru não encontra — e é assim que
        o bug do telefone com máscara (armadilha 27) aparecia.
        """
        criar_cliente(db, email="joana@exemplo.com", phone="85988887777")

        repositorio = CustomerRepository(db)

        assert repositorio.get_by_email("JOANA@exemplo.com") is None
        assert repositorio.get_by_phone("(85) 98888-7777") is None

    def test_get_by_email_or_phone_prefere_o_email(self, db):
        """Com os dois preenchidos, o telefone nem é consultado. Importa
        porque os dois podem apontar para clientes diferentes."""
        dona_do_email = criar_cliente(db, email="dona@exemplo.com", phone="85911111111")
        criar_cliente(db, email="outro@exemplo.com", phone="85922222222")

        encontrado = CustomerRepository(db).get_by_email_or_phone(
            email="dona@exemplo.com",
            phone="85922222222",
        )

        assert encontrado.id == dona_do_email.id

    def test_sem_email_e_sem_telefone_devolve_none_sem_consultar(self, db):
        criar_cliente(db)
        assert CustomerRepository(db).get_by_email_or_phone(email=None, phone=None) is None

    def test_lock_customer_traz_a_mesma_linha_de_get_by_id(self, db):
        """`with_for_update` é o que o resgate de cashback vai usar (armadilha
        26). O que se garante aqui é que a cláusula é aceita pelo Postgres na
        forma em que está escrita — travar de verdade exigiria duas conexões, e
        o valor disso não paga a complexidade num teste de repositório."""
        cliente = criar_cliente(db)

        repositorio = CustomerRepository(db)

        assert repositorio.lock_customer(cliente.id).id == cliente.id
        assert repositorio.get_by_id(cliente.id).id == cliente.id


class TestOsCodigosDeVerificacaoDeEmail:
    def test_o_ultimo_nao_usado_e_o_mais_recente(self, db):
        cliente = criar_cliente(db, email="joana@exemplo.com")
        self._criar_codigo(db, cliente, "antigo", criado_em=instante(2026, 8, 1, 10))
        recente = self._criar_codigo(db, cliente, "recente", criado_em=instante(2026, 8, 1, 11))

        encontrado = CustomerRepository(db).latest_unused_email_code("joana@exemplo.com")

        assert encontrado.id == recente.id

    def test_codigo_ja_usado_nao_conta_como_ultimo(self, db):
        """É o que faz o cooldown de reenvio olhar só para código pendente:
        um código usado com sucesso não pode segurar o próximo envio."""
        cliente = criar_cliente(db, email="joana@exemplo.com")
        pendente = self._criar_codigo(db, cliente, "pendente", criado_em=instante(2026, 8, 1, 10))
        usado = self._criar_codigo(db, cliente, "usado", criado_em=instante(2026, 8, 1, 11))
        usado.used_at = instante(2026, 8, 1, 11, 30)
        db.flush()

        encontrado = CustomerRepository(db).latest_unused_email_code("joana@exemplo.com")

        assert encontrado.id == pendente.id

    def test_o_codigo_de_outro_email_nao_aparece(self, db):
        outro = criar_cliente(db, email="outro@exemplo.com")
        self._criar_codigo(db, outro, "do-outro", criado_em=instante(2026, 8, 1, 10))

        assert CustomerRepository(db).latest_unused_email_code("joana@exemplo.com") is None

    def test_a_janela_de_contagem_inclui_o_instante_exato(self, db):
        """`created_at >= since`, e o `=` é o que está sendo travado aqui: com
        `>` estrito, o código criado no exato limite da janela escaparia da
        contagem e o cliente ganharia um reenvio a mais."""
        cliente = criar_cliente(db, email="joana@exemplo.com")
        limite = instante(2026, 8, 1, 10)
        self._criar_codigo(db, cliente, "no-limite", criado_em=limite)
        self._criar_codigo(db, cliente, "antes", criado_em=limite - timedelta(seconds=1))
        self._criar_codigo(db, cliente, "depois", criado_em=limite + timedelta(seconds=1))

        quantos = CustomerRepository(db).count_email_codes_since(
            email="joana@exemplo.com",
            since=limite,
        )

        assert quantos == 2

    def test_a_contagem_ignora_se_o_codigo_foi_usado(self, db):
        """Ao contrário de `latest_unused_email_code`. É proposital: o limite
        de reenvios conta ENVIOS, e um código usado foi enviado do mesmo
        jeito."""
        cliente = criar_cliente(db, email="joana@exemplo.com")
        limite = instante(2026, 8, 1, 10)
        usado = self._criar_codigo(db, cliente, "usado", criado_em=limite)
        usado.used_at = limite + timedelta(minutes=1)
        db.flush()

        quantos = CustomerRepository(db).count_email_codes_since(
            email="joana@exemplo.com",
            since=limite,
        )

        assert quantos == 1

    @staticmethod
    def _criar_codigo(db, cliente, code_hash, criado_em):
        codigo = EmailVerificationCode(
            customer_id=cliente.id,
            email=cliente.email,
            code_hash=code_hash,
            expires_at=criado_em + timedelta(minutes=15),
        )
        codigo.created_at = criado_em
        db.add(codigo)
        db.flush()
        return codigo


class TestOsCodigosDeRecuperacaoDeSenha:
    def test_invalidar_marca_so_os_nao_usados_do_cliente(self, db):
        """Três coisas de uma vez porque é o conjunto que importa: o já usado
        conserva o `used_at` original (perdê-lo apagaria quando ele foi usado)
        e o código de outro cliente não é tocado."""
        cliente = criar_cliente(db, email="joana@exemplo.com")
        outro = criar_cliente(db, email="outro@exemplo.com")

        pendente = self._criar_codigo(db, cliente, "pendente")
        usado_antes = self._criar_codigo(db, cliente, "usado")
        usado_antes.used_at = instante(2026, 8, 1, 9)
        do_outro = self._criar_codigo(db, outro, "do-outro")
        db.flush()

        CustomerRepository(db).invalidate_unused_password_reset_codes(cliente.id)

        assert pendente.used_at is not None
        assert usado_antes.used_at == instante(2026, 8, 1, 9)
        assert do_outro.used_at is None

    def test_encontra_pelo_hash_do_token(self, db):
        cliente = criar_cliente(db)
        codigo = self._criar_codigo(db, cliente, "codigo")
        codigo.reset_token_hash = "hash-do-token"
        db.flush()

        encontrado = CustomerRepository(db).get_password_reset_by_token_hash("hash-do-token")

        assert encontrado.id == codigo.id

    def test_hash_desconhecido_devolve_none(self, db):
        criar_cliente(db)
        assert CustomerRepository(db).get_password_reset_by_token_hash("nao-existe") is None

    @staticmethod
    def _criar_codigo(db, cliente, code_hash):
        codigo = PasswordResetCode(
            customer_id=cliente.id,
            email=cliente.email,
            code_hash=code_hash,
            expires_at=instante(2026, 8, 1, 12),
        )
        db.add(codigo)
        db.flush()
        return codigo


class TestOsEnderecos:
    def test_o_padrao_vem_primeiro_e_o_resto_do_mais_novo_para_o_mais_velho(self, db):
        """A ordenação é `is_default DESC, created_at DESC` — duas colunas, e
        o teste precisa das duas para valer: com só uma delas, trocar a ordem
        das cláusulas passaria despercebido."""
        cliente = criar_cliente(db)
        velho = criar_endereco(db, cliente, street="Velho", created_at=instante(2026, 8, 1, 8))
        novo = criar_endereco(db, cliente, street="Novo", created_at=instante(2026, 8, 1, 10))
        padrao = criar_endereco(
            db,
            cliente,
            street="Padrao",
            is_default=True,
            created_at=instante(2026, 8, 1, 9),
        )

        encontrados = CustomerRepository(db).list_addresses(cliente.id)

        assert [endereco.id for endereco in encontrados] == [padrao.id, novo.id, velho.id]

    def test_endereco_de_outro_cliente_nao_aparece_nem_e_encontrado(self, db):
        """O `customer_id` no WHERE de `get_address` é o que impede alguém com
        um UUID de endereço em mãos de ler a rua de outra pessoa."""
        cliente = criar_cliente(db)
        outro = criar_cliente(db)
        do_outro = criar_endereco(db, outro, street="Rua do Outro")

        repositorio = CustomerRepository(db)

        assert repositorio.list_addresses(cliente.id) == []
        assert repositorio.get_address(cliente.id, do_outro.id) is None
        assert repositorio.get_address(outro.id, do_outro.id).id == do_outro.id

    def test_desmarcar_o_padrao_nao_alcanca_outro_cliente(self, db):
        cliente = criar_cliente(db)
        marcado = criar_endereco(db, cliente, street="Casa", is_default=True)
        outro = criar_cliente(db)
        marcado_do_outro = criar_endereco(db, outro, street="Casa do Outro", is_default=True)

        CustomerRepository(db).unset_default_addresses(cliente.id)

        assert marcado.is_default is False
        assert marcado_do_outro.is_default is True
