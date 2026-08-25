"""Cartao salvo por restaurante: o que vai para o Mercado Pago e o que fica aqui.

O gateway e SUBSTITUIDO por um FakeHttpxClient no lugar de httpx.Client —
nenhuma chamada de rede acontece nestes testes.

O que estes testes PROVAM:

- **o numero do cartao nao existe no contrato.** Nem em `SaveCardRequest`,
  nem em `CardPaymentPayload`, nem em `SavedCardResponse`, nem nas colunas
  das duas tabelas novas. Este e o teste que deve falhar alto se alguem um
  dia acrescentar um campo de PAN "so para validar do lado de ca";
- o customer do Mercado Pago e BUSCADO antes de criado, e a corrida (dois
  cadastros ao mesmo tempo) cai na busca de recuperacao em vez de estourar;
- salvar le do gateway SO os cinco campos que a tabela guarda — os
  `first_six_digits` e o nome do portador que eles mandam ficam de fora;
- remover cartao apaga NOS DOIS LADOS, na ordem gateway->banco, e um 404
  deles conta como sucesso;
- a cobranca com cartao salvo manda `payer.type=customer` e `payer.id`, e a
  BANDEIRA sai do banco, nunca do corpo que o cliente enviou.

O que ISSO NAO PROVA — so uma chamada real contra a credencial de teste do
Mercado Pago prova:

- que o corpo que montamos para `/v1/customers` e `/v1/customers/{id}/cards`
  e aceito pela API deles hoje;
- que um token gerado a partir de um `card_id` + CVV no navegador de fato
  cobra o cartao salvo;
- que a analise antifraude devolve `in_process` nos casos em que esperamos.
"""

import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx

from src.integrations.payment_gateway import (
    CardPaymentInput,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
    create_payment,
    delete_saved_card,
    find_or_create_gateway_customer,
    save_card,
)


ACCESS_TOKEN = "TEST-token-do-junior-da-picanha"
HTTPX_CLIENT_PATH = "src.integrations.payment_gateway.httpx.Client"
CUSTOMER_ID = "1234567890-abcDEF"
CARD_ID = "9876543210"


class FakeResponse:
    def __init__(self, status_code, json_body=None, content=b"{}"):
        self.status_code = status_code
        self._json_body = json_body
        self.content = content

    def json(self):
        return self._json_body


class SequenceHttpxClient:
    """Devolve uma resposta por chamada, na ordem.

    O dublê do test_mercadopago_gateway.py responde sempre a MESMA coisa, e
    aqui isso nao serve: `find_or_create_gateway_customer` faz busca e
    criacao em sequencia, e o que se quer provar e justamente a ordem delas.
    """

    def __init__(self, responses=None, exception=None):
        self._responses = list(responses or [])
        self._exception = exception
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def request(self, method, url, json=None, headers=None):
        self.requests.append({"method": method, "url": url, "json": json, "headers": headers})
        if self._exception is not None:
            raise self._exception
        return self._responses.pop(0)


def patched(client):
    return patch(HTTPX_CLIENT_PATH, return_value=client)


class NumeroDoCartaoNaoAtravessaTests(unittest.TestCase):
    """A garantia central do desenho, escrita como teste.

    Se um destes falhar, a integracao saiu do padrao de tokenizacao e o
    perimetro de PCI do projeto mudou — nao e um teste a ajustar, e uma
    decisao a reverter.
    """

    def test_o_contrato_de_salvar_cartao_nao_tem_campo_de_numero(self):
        from src.schemas.saved_card_schema import SaveCardRequest, SavedCardResponse

        proibidos = {
            "number", "card_number", "pan", "cvv", "security_code",
            "cvc", "first_six_digits", "cardholder_name",
        }
        for schema in (SaveCardRequest, SavedCardResponse):
            with self.subTest(schema=schema.__name__):
                self.assertEqual(set(schema.model_fields) & proibidos, set())

    def test_o_contrato_de_pagar_com_cartao_nao_tem_campo_de_numero(self):
        from src.schemas.payment_schema import CardPaymentPayload

        proibidos = {"number", "card_number", "pan", "cvv", "security_code", "cvc"}
        self.assertEqual(set(CardPaymentPayload.model_fields) & proibidos, set())

    def test_as_colunas_do_cartao_salvo_nao_guardam_numero(self):
        from src.models.customer_saved_card_model import CustomerSavedCard

        colunas = {coluna.name for coluna in CustomerSavedCard.__table__.columns}
        proibidas = {
            "number", "card_number", "pan", "cvv", "security_code",
            "first_six_digits", "cardholder_name",
        }
        self.assertEqual(colunas & proibidas, set())
        self.assertIn("last_four_digits", colunas)

    def test_salvar_manda_para_o_gateway_SO_o_token(self):
        """O corpo do POST /v1/customers/{id}/cards tem uma chave e uma so."""
        client = SequenceHttpxClient([
            FakeResponse(201, {
                "id": CARD_ID,
                "last_four_digits": "4321",
                "payment_method": {"id": "master"},
                "expiration_month": 11,
                "expiration_year": 2030,
            }),
        ])
        with patched(client):
            save_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                token="token-do-navegador",
            )
        self.assertEqual(client.requests[0]["json"], {"token": "token-do-navegador"})


class CustomerDoGatewayTests(unittest.TestCase):
    def test_busca_antes_de_criar_e_reaproveita_o_existente(self):
        client = SequenceHttpxClient([
            FakeResponse(200, {"results": [{"id": CUSTOMER_ID}]}),
        ])
        with patched(client):
            encontrado = find_or_create_gateway_customer(
                access_token=ACCESS_TOKEN, email="cliente@exemplo.com"
            )

        self.assertEqual(encontrado, CUSTOMER_ID)
        # Uma chamada so: nao houve POST.
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["method"], "GET")
        self.assertIn("/v1/customers/search", client.requests[0]["url"])

    def test_cria_quando_a_conta_ainda_nao_conhece_o_email(self):
        client = SequenceHttpxClient([
            FakeResponse(200, {"results": []}),
            FakeResponse(201, {"id": CUSTOMER_ID}),
        ])
        with patched(client):
            criado = find_or_create_gateway_customer(
                access_token=ACCESS_TOKEN, email="novo@exemplo.com"
            )

        self.assertEqual(criado, CUSTOMER_ID)
        self.assertEqual(client.requests[1]["method"], "POST")
        self.assertEqual(client.requests[1]["json"], {"email": "novo@exemplo.com"})

    def test_o_email_vai_url_encoded_na_busca(self):
        """`+` em e-mail e valido e, cru na query, viraria espaco."""
        client = SequenceHttpxClient([FakeResponse(200, {"results": []}),
                                      FakeResponse(201, {"id": CUSTOMER_ID})])
        with patched(client):
            find_or_create_gateway_customer(
                access_token=ACCESS_TOKEN, email="cliente+tag@exemplo.com"
            )
        self.assertIn("cliente%2Btag%40exemplo.com", client.requests[0]["url"])

    def test_corrida_de_dois_cadastros_cai_na_busca_de_recuperacao(self):
        """Duas abas salvando ao mesmo tempo: a segunda leva o 400 do POST e
        acha o customer que a primeira criou, em vez de estourar um erro que
        o cliente nao tem como resolver."""
        client = SequenceHttpxClient([
            FakeResponse(200, {"results": []}),
            FakeResponse(400, {"error": "bad_request", "message": "already exists"}),
            FakeResponse(200, {"results": [{"id": CUSTOMER_ID}]}),
        ])
        with patched(client):
            recuperado = find_or_create_gateway_customer(
                access_token=ACCESS_TOKEN, email="cliente@exemplo.com"
            )

        self.assertEqual(recuperado, CUSTOMER_ID)
        self.assertEqual(len(client.requests), 3)

    def test_erro_do_post_que_a_busca_nao_recupera_sobe(self):
        client = SequenceHttpxClient([
            FakeResponse(200, {"results": []}),
            FakeResponse(400, {"error": "bad_request", "message": "email invalido"}),
            FakeResponse(200, {"results": []}),
        ])
        with patched(client), self.assertRaises(PaymentGatewayError):
            find_or_create_gateway_customer(
                access_token=ACCESS_TOKEN, email="nao-e-email"
            )

    def test_customer_criado_sem_id_nao_passa_por_valido(self):
        client = SequenceHttpxClient([
            FakeResponse(200, {"results": []}),
            FakeResponse(201, {}),
        ])
        with patched(client), self.assertRaises(PaymentGatewayUnavailableError):
            find_or_create_gateway_customer(
                access_token=ACCESS_TOKEN, email="cliente@exemplo.com"
            )


class SalvarCartaoTests(unittest.TestCase):
    def test_le_so_os_cinco_campos_que_a_tabela_guarda(self):
        """A resposta deles traz mais coisa. O que nao e extraido aqui nao
        tem como ser gravado por engano depois."""
        client = SequenceHttpxClient([
            FakeResponse(201, {
                "id": CARD_ID,
                "last_four_digits": "4321",
                "first_six_digits": "503175",
                "cardholder": {"name": "FULANO DE TAL", "identification": {"number": "123"}},
                "payment_method": {"id": "master", "name": "Mastercard"},
                "issuer": {"id": 24, "name": "Banco"},
                "expiration_month": 11,
                "expiration_year": 2030,
            }),
        ])
        with patched(client):
            dados = save_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                token="token-do-navegador",
            )

        self.assertEqual(dados.provider_card_id, CARD_ID)
        self.assertEqual(dados.brand, "master")
        self.assertEqual(dados.last_four_digits, "4321")
        self.assertEqual(dados.expiration_month, 11)
        self.assertEqual(dados.expiration_year, 2030)
        # Nem os seis primeiros digitos nem o nome do portador viraram atributo.
        self.assertFalse(hasattr(dados, "first_six_digits"))
        self.assertFalse(hasattr(dados, "cardholder_name"))

    def test_resposta_sem_bandeira_ou_digitos_nao_vira_linha_no_banco(self):
        client = SequenceHttpxClient([
            FakeResponse(201, {"id": CARD_ID, "last_four_digits": "4321"}),
        ])
        with patched(client), self.assertRaises(PaymentGatewayUnavailableError):
            save_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                token="token-do-navegador",
            )

    def test_a_url_leva_o_customer_e_o_token_do_restaurante(self):
        client = SequenceHttpxClient([
            FakeResponse(201, {
                "id": CARD_ID,
                "last_four_digits": "4321",
                "payment_method": {"id": "visa"},
            }),
        ])
        with patched(client):
            save_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                token="token-do-navegador",
            )

        pedido = client.requests[0]
        self.assertTrue(pedido["url"].endswith(f"/v1/customers/{CUSTOMER_ID}/cards"))
        self.assertEqual(pedido["headers"]["Authorization"], f"Bearer {ACCESS_TOKEN}")


class RemoverCartaoTests(unittest.TestCase):
    def test_manda_delete_na_url_do_cartao(self):
        client = SequenceHttpxClient([FakeResponse(200, {"id": CARD_ID})])
        with patched(client):
            delete_saved_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                provider_card_id=CARD_ID,
            )

        pedido = client.requests[0]
        self.assertEqual(pedido["method"], "DELETE")
        self.assertTrue(
            pedido["url"].endswith(f"/v1/customers/{CUSTOMER_ID}/cards/{CARD_ID}")
        )

    def test_corpo_vazio_e_sucesso(self):
        """Eles ora devolvem o recurso apagado, ora um 204 pelado. Tratar
        corpo vazio como erro faria uma remocao bem-sucedida virar 502 na
        cara do cliente, com a linha continuando no nosso banco."""
        client = SequenceHttpxClient([FakeResponse(204, None, content=b"")])
        with patched(client):
            delete_saved_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                provider_card_id=CARD_ID,
            )

    def test_404_do_gateway_conta_como_sucesso(self):
        """O cartao ja nao esta la, que e o estado que se queria. Levantar
        erro travaria para sempre uma linha que o cliente quer ver sumir."""
        client = SequenceHttpxClient([
            FakeResponse(404, {"error": "not_found", "message": "card not found"}),
        ])
        with patched(client):
            delete_saved_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                provider_card_id=CARD_ID,
            )

    def test_gateway_fora_do_ar_nao_e_engolido(self):
        """A linha tem que continuar no banco para o cliente tentar de novo."""
        client = SequenceHttpxClient(exception=httpx.ConnectError("sem rede"))
        with patched(client), self.assertRaises(PaymentGatewayUnavailableError):
            delete_saved_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                provider_card_id=CARD_ID,
            )

    def test_401_do_gateway_nao_vira_sucesso_silencioso(self):
        client = SequenceHttpxClient([
            FakeResponse(401, {"error": "unauthorized", "message": "invalid token"}),
        ])
        with patched(client), self.assertRaises(PaymentGatewayError):
            delete_saved_card(
                access_token=ACCESS_TOKEN,
                provider_customer_id=CUSTOMER_ID,
                provider_card_id=CARD_ID,
            )


class CobrancaComCartaoSalvoTests(unittest.TestCase):
    def _cobra(self, card):
        client = SequenceHttpxClient([
            FakeResponse(201, {
                "id": 987654321,
                "status": "approved",
                "status_detail": "accredited",
            }),
        ])
        with patched(client):
            intent = create_payment(
                provider="mercadopago",
                order_id=uuid.uuid4(),
                amount=Decimal("93.00"),
                payment_method="credit_card",
                description="Pedido #1234",
                access_token=ACCESS_TOKEN,
                payer_email="cliente@exemplo.com",
                card=card,
            )
        return client.requests[0]["json"], intent

    def test_cartao_salvo_manda_payer_type_customer_e_id(self):
        """Sem estes dois campos o Mercado Pago recusa um token que nasceu de
        um `card_id`: para ele o cartao pertence ao customer."""
        body, intent = self._cobra(
            CardPaymentInput(
                token="token-gerado-do-card-id-mais-cvv",
                payment_method_id="master",
                provider_customer_id=CUSTOMER_ID,
            )
        )

        self.assertEqual(body["payer"]["type"], "customer")
        self.assertEqual(body["payer"]["id"], CUSTOMER_ID)
        self.assertEqual(body["payer"]["email"], "cliente@exemplo.com")
        self.assertEqual(body["token"], "token-gerado-do-card-id-mais-cvv")
        self.assertEqual(intent.payment_status, "paid")

    def test_cartao_avulso_continua_sem_payer_type(self):
        """A cobranca de cartao digitado na hora nao pode ganhar `payer.id`
        de brinde: o pagador dela e o e-mail avulso, e so."""
        body, _ = self._cobra(
            CardPaymentInput(token="token-avulso", payment_method_id="visa")
        )

        self.assertNotIn("type", body["payer"])
        self.assertNotIn("id", body["payer"])
        self.assertEqual(body["payer"]["email"], "cliente@exemplo.com")

    def test_o_token_continua_obrigatorio_no_cartao_salvo(self):
        """Cartao salvo poupa redigitar o NUMERO, nao o CVV: o SDK gera um
        token novo a partir do `card_id` + codigo de seguranca. Nao existe
        cobranca de cartao salvo sem token."""
        body, _ = self._cobra(
            CardPaymentInput(
                token="token-obrigatorio",
                payment_method_id="elo",
                provider_customer_id=CUSTOMER_ID,
            )
        )
        self.assertIn("token", body)
        self.assertTrue(body["token"])


class ContratoDoPayloadDeCartaoTests(unittest.TestCase):
    def test_bandeira_pode_faltar_quando_ha_cartao_salvo(self):
        from src.schemas.payment_schema import CardPaymentPayload

        payload = CardPaymentPayload(token="t", saved_card_id=uuid.uuid4())
        self.assertIsNone(payload.payment_method_id)

    def test_sem_bandeira_e_sem_cartao_salvo_o_corpo_e_recusado(self):
        """422 do proprio contrato, e nao um 400 do gateway la na frente
        dizendo `payment_method_id` ausente com o nome deles."""
        from pydantic import ValidationError
        from src.schemas.payment_schema import CardPaymentPayload

        with self.assertRaises(ValidationError):
            CardPaymentPayload(token="t")


class AutorizacaoDoCartaoSalvoTests(unittest.TestCase):
    """`_resolve_card_input` com o repositorio dublado.

    O que se prova aqui e a AUTORIZACAO, que e a parte que nao pode depender
    de o banco estar de pe para ser conferida: cartao de outra pessoa e
    cartao de outra loja tem que dar 404 antes de qualquer cobranca existir.
    """

    def _service(self, saved):
        from src.services.payment_service import PaymentService

        service = PaymentService.__new__(PaymentService)
        service.saved_card_repository = _RepositorioDublado(saved)
        return service

    def _payload(self, saved_card_id):
        from src.schemas.payment_schema import CardPaymentPayload, StartPaymentRequest

        return StartPaymentRequest(
            card=CardPaymentPayload(token="token", saved_card_id=saved_card_id)
        )

    def test_cartao_da_pessoa_e_da_loja_certa_vira_input_com_o_customer(self):
        restaurante = uuid.uuid4()
        saved = _CartaoDublado(
            brand="master", restaurant_id=restaurante, provider_customer_id=CUSTOMER_ID
        )
        service = self._service(saved)

        card = service._resolve_card_input(
            "credit_card", self._payload(uuid.uuid4()), _ClienteDublado(), restaurante
        )

        self.assertEqual(card.provider_customer_id, CUSTOMER_ID)
        # A bandeira sai do BANCO, e nao do corpo que o cliente enviou.
        self.assertEqual(card.payment_method_id, "master")

    def test_cartao_de_outra_pessoa_da_404(self):
        from fastapi import HTTPException

        service = self._service(None)
        with self.assertRaises(HTTPException) as erro:
            service._resolve_card_input(
                "credit_card", self._payload(uuid.uuid4()), _ClienteDublado(), uuid.uuid4()
            )

        self.assertEqual(erro.exception.status_code, 404)
        self.assertEqual(erro.exception.detail["code"], "saved_card_not_found")

    def test_cartao_salvo_em_outra_loja_da_404(self):
        """Cobrar na loja B um cartao salvo na loja A daria 404 do gateway no
        meio do checkout, com a cobranca ja em andamento. Recusar aqui custa
        uma resposta."""
        from fastapi import HTTPException

        saved = _CartaoDublado(
            brand="visa", restaurant_id=uuid.uuid4(), provider_customer_id=CUSTOMER_ID
        )
        service = self._service(saved)

        with self.assertRaises(HTTPException) as erro:
            service._resolve_card_input(
                "credit_card", self._payload(uuid.uuid4()), _ClienteDublado(), uuid.uuid4()
            )

        self.assertEqual(erro.exception.status_code, 404)
        self.assertEqual(erro.exception.detail["code"], "saved_card_not_found")

    def test_cartao_avulso_nao_consulta_o_repositorio(self):
        from src.schemas.payment_schema import CardPaymentPayload, StartPaymentRequest

        service = self._service(None)
        payload = StartPaymentRequest(
            card=CardPaymentPayload(token="token", payment_method_id="visa")
        )

        card = service._resolve_card_input(
            "credit_card", payload, _ClienteDublado(), uuid.uuid4()
        )

        self.assertIsNone(card.provider_customer_id)
        self.assertEqual(card.payment_method_id, "visa")
        self.assertEqual(service.saved_card_repository.consultas, 0)


class _ClienteDublado:
    def __init__(self):
        self.id = uuid.uuid4()


class _PerfilDublado:
    def __init__(self, restaurant_id, provider_customer_id):
        self.restaurant_id = restaurant_id
        self.provider_customer_id = provider_customer_id


class _CartaoDublado:
    def __init__(self, brand, restaurant_id, provider_customer_id):
        self.brand = brand
        self.profile = _PerfilDublado(restaurant_id, provider_customer_id)


class _RepositorioDublado:
    def __init__(self, saved):
        self._saved = saved
        self.consultas = 0

    def get_card_of_customer(self, customer_id, card_id):
        self.consultas += 1
        return self._saved


if __name__ == "__main__":
    unittest.main()
