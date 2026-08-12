import json
import unittest
import uuid

from pydantic import ValidationError

from main import app
from src.schemas.customer_schema import ImportCustomerAddressesRequest
from src.schemas.delivery_schema import DeliveryEstimateRequest
from src.schemas.payment_schema import PaymentErrorCode


PAYMENT_ROUTE = "/restaurants/{restaurant_slug}/orders/{tracking_token}/payment"


class PaymentErrorContractTests(unittest.TestCase):
    """O erro da cobranca tem que estar PUBLICADO, nao so implementado.

    O frontend escreve o parser a partir do /openapi.json. Um formato que
    existe no codigo mas nao no documento e um formato que ninguem consegue
    consumir sem ler o backend.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = app.openapi()
        cls.operation = cls.schema["paths"][PAYMENT_ROUTE]["post"]

    def test_the_error_statuses_are_declared(self):
        self.assertIn("502", self.operation["responses"])
        self.assertIn("503", self.operation["responses"])

    def test_the_declared_body_carries_the_detail_envelope(self):
        # HTTPException entrega {"detail": {...}}. Anunciar o detail na raiz
        # faria o frontend escrever o parser contra um formato que a rota
        # nunca devolve.
        for status_code in ("502", "503"):
            ref = self.operation["responses"][status_code]["content"]["application/json"]["schema"]["$ref"]
            self.assertEqual(ref, "#/components/schemas/PaymentErrorResponse")

        envelope = self.schema["components"]["schemas"]["PaymentErrorResponse"]
        self.assertEqual(
            envelope["properties"]["detail"]["$ref"],
            "#/components/schemas/PaymentErrorDetail",
        )

    def test_the_detail_publishes_every_field_the_frontend_reads(self):
        detail = self.schema["components"]["schemas"]["PaymentErrorDetail"]

        for field in ("code", "message", "retryable", "provider_error_code"):
            self.assertIn(field, detail["properties"])
        # provider_error_code e o unico opcional: nem todo erro tem
        # referencia do provedor.
        self.assertEqual(sorted(detail["required"]), ["code", "message", "retryable"])

    def test_the_possible_codes_are_published_as_an_enum(self):
        # Sem a lista, o frontend so consegue tratar `retryable` e cai no
        # texto generico para os dois casos definitivos, que pedem coisas
        # diferentes do cliente.
        published = self.schema["components"]["schemas"]["PaymentErrorCode"]["enum"]

        self.assertEqual(sorted(published), sorted(code.value for code in PaymentErrorCode))
        self.assertIn("gateway_unavailable", published)
        self.assertIn("payment_unavailable", published)
        self.assertIn("payment_rejected", published)

    def test_the_enum_is_reachable_from_the_route(self):
        # Um enum solto em components que nenhuma rota referencia nao chega
        # a gerador de cliente nenhum.
        detail = self.schema["components"]["schemas"]["PaymentErrorDetail"]

        self.assertIn("PaymentErrorCode", json.dumps(detail["properties"]["code"]))


class DeliveryContractTests(unittest.TestCase):
    def test_accepts_address_id_and_empty_branch_id(self):
        address_id = uuid.uuid4()
        payload = DeliveryEstimateRequest.model_validate(
            {"branch_id": "", "address_id": str(address_id)}
        )
        self.assertIsNone(payload.branch_id)
        self.assertEqual(payload.address_id, address_id)

    def test_normalizes_inline_address_legacy_values(self):
        payload = DeliveryEstimateRequest.model_validate(
            {
                "address": {
                    "street": " Travessa Joao Felipe ",
                    "number": " 111 ",
                    "neighborhood": " Mousa Brasil ",
                    "city": "",
                    "state": None,
                    "zip_code": " 60123-456 ",
                    "latitude": "",
                    "longitude": "",
                }
            }
        )
        self.assertEqual(payload.address.city, "Fortaleza")
        self.assertEqual(payload.address.state, "CE")
        self.assertEqual(payload.address.zipcode, "60123-456")
        self.assertIsNone(payload.address.latitude)
        self.assertIsNone(payload.address.longitude)

    def test_requires_exactly_one_address_source_with_clear_message(self):
        for raw in ({}, {"address_id": str(uuid.uuid4()), "address": {
            "street": "Rua A", "number": "1", "neighborhood": "Centro"
        }}):
            with self.assertRaises(ValidationError) as raised:
                DeliveryEstimateRequest.model_validate(raw)
            self.assertIn(
                "Informe exatamente um entre address_id e address",
                str(raised.exception),
            )

    def test_rejects_customer_id_and_untrusted_calculation_fields(self):
        for field in ("customer_id", "delivery_fee", "distance_km", "eta_min", "prep_time_max"):
            with self.assertRaises(ValidationError):
                DeliveryEstimateRequest.model_validate(
                    {field: "1", "address_id": str(uuid.uuid4())}
                )


class AddressImportContractTests(unittest.TestCase):
    def test_accepts_empty_list(self):
        payload = ImportCustomerAddressesRequest.model_validate({"addresses": []})
        self.assertEqual(payload.addresses, [])

    def test_requires_addresses_to_be_a_list(self):
        with self.assertRaises(ValidationError) as raised:
            ImportCustomerAddressesRequest.model_validate({"addresses": {}})
        self.assertIn("addresses", str(raised.exception))

    def test_accepts_zip_alias_and_normalizes_empty_optional_fields(self):
        payload = ImportCustomerAddressesRequest.model_validate(
            {
                "addresses": [{
                    "client_reference": " local-uuid ",
                    "label": "",
                    "street": " Travessa Joao Felipe ",
                    "number": " 111 ",
                    "neighborhood": " Mousa Brasil ",
                    "city": "Fortaleza",
                    "state": "CE",
                    "zip_code": "60123-456",
                    "complement": "",
                    "reference": None,
                    "latitude": "",
                    "longitude": None,
                    "is_default": True,
                }]
            }
        )
        address = payload.addresses[0]
        self.assertEqual(address.zipcode, "60123456")
        self.assertIsNone(address.label)
        self.assertIsNone(address.complement)
        self.assertIsNone(address.latitude)

    def test_rejects_customer_id_and_more_than_twenty_addresses(self):
        address = {"street": "Rua A", "number": "1", "neighborhood": "Centro"}
        with self.assertRaises(ValidationError):
            ImportCustomerAddressesRequest.model_validate(
                {"addresses": [{**address, "customer_id": str(uuid.uuid4())}]}
            )
        with self.assertRaises(ValidationError):
            ImportCustomerAddressesRequest.model_validate(
                {"addresses": [address] * 21}
            )

    def test_openapi_documents_both_request_bodies(self):
        paths = app.openapi()["paths"]
        self.assertIn(
            "requestBody",
            paths["/restaurants/{restaurant_slug}/delivery/estimate"]["post"],
        )
        operation = paths["/customers/me/addresses/import"]["post"]
        self.assertIn("requestBody", operation)
        self.assertEqual(operation["security"], [{"HTTPBearer": []}])

    def test_openapi_documents_cashback_transactions_pagination_and_auth(self):
        operation = app.openapi()["paths"]["/customers/me/cashback/transactions"]["get"]
        parameters = {item["name"]: item for item in operation["parameters"]}

        self.assertEqual(operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(parameters["limit"]["schema"]["default"], 20)
        self.assertEqual(parameters["limit"]["schema"]["maximum"], 50)
        self.assertEqual(parameters["offset"]["schema"]["default"], 0)


# As rotas do agente de impressao, do jeito que o /openapi.json as publica.
#
# Duas sao chamadas PELO agente e tres sao lidas pelo PAINEL. Os prefixos
# diferentes (`/admin/print-agent/...` contra `/admin/branches/{id}/...`) nao
# sao estetica: as do agente nao levam filial no path porque a filial dele sai
# do token.
AGENT_ROUTES = {
    "/admin/print-agent/heartbeat": "post",
    "/admin/print-agent/printers": "post",
}
PANEL_ROUTES = {
    "/admin/branches/{branch_id}/print-agent": "get",
    "/admin/branches/{branch_id}/printers": "get",
    "/admin/branches/{branch_id}/print-test": "post",
}


class PrintAgentRouteContractTests(unittest.TestCase):
    """As cinco rotas novas tem que estar PUBLICADAS, nao so implementadas.

    O painel consome o /openapi.json (armadilha 16): rota que existe no
    codigo mas nao no documento e rota que ninguem consegue chamar sem ler o
    backend. E como o agente de impressao e um segundo cliente desta API,
    instalado a mao em maquina de balcao, um contrato que muda sem aparecer
    aqui so e descoberto com a cozinha parada.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = app.openapi()
        cls.paths = cls.schema["paths"]

    def test_the_five_routes_are_published_with_their_methods(self):
        for path, method in {**AGENT_ROUTES, **PANEL_ROUTES}.items():
            self.assertIn(path, self.paths, path)
            self.assertIn(method, self.paths[path], f"{method} {path}")

    def test_every_route_demands_the_bearer_token(self):
        # Mesma exigencia do resto de /admin. Uma rota do agente publicada
        # sem `security` convidaria o painel a chama-la sem token e, pior,
        # sugeriria que ela e aberta.
        for path, method in {**AGENT_ROUTES, **PANEL_ROUTES}.items():
            operation = self.paths[path][method]
            self.assertEqual(operation["security"], [{"HTTPBearer": []}], path)

    def test_the_agent_routes_do_not_take_a_branch_anywhere(self):
        """A filial do agente sai do TOKEN, nunca do cliente.

        Se ela aparecesse no path ou no corpo, um agente poderia se anunciar
        como outra loja e receber os comandos dela — a via de teste da
        Aldeota sairia no Centro. E o mesmo defeito que a revisao 0015
        corrigiu no usuario do Junior, e o documento e onde ele reapareceria
        primeiro.
        """
        for path, method in AGENT_ROUTES.items():
            operation = self.paths[path][method]
            self.assertEqual(operation.get("parameters", []), [], path)

            body = operation["requestBody"]["content"]["application/json"]
            name = body["schema"]["$ref"].rsplit("/", 1)[-1]
            published = self.schema["components"]["schemas"][name]["properties"]
            self.assertNotIn("branch_id", published, path)

    def test_the_panel_routes_take_the_branch_in_the_path(self):
        for path, method in PANEL_ROUTES.items():
            names = [item["name"] for item in self.paths[path][method]["parameters"]]
            self.assertIn("branch_id", names, path)

    def test_the_heartbeat_publishes_only_the_version(self):
        # O agente conta a versao instalada e mais nada. E assim que o painel
        # percebe que a maquina foi atualizada, sem ninguem avisar.
        request = self.schema["components"]["schemas"]["PrintAgentHeartbeatRequest"]
        self.assertEqual(list(request["properties"]), ["agent_version"])

    def test_the_status_publishes_is_online_as_required(self):
        """`is_online` e a pergunta que a tela faz, e ela nao pode vir vazia.

        Os outros campos sao opcionais de proposito: filial que nunca
        instalou o agente responde 200 com `is_online=false` e o resto nulo,
        e nao 404 — "ninguem instalou aqui" e uma resposta que a tela precisa
        poder mostrar.
        """
        status = self.schema["components"]["schemas"]["PrintAgentStatusResponse"]

        self.assertIn("is_online", status["properties"])
        self.assertEqual(sorted(status["required"]), ["branch_id", "is_online"])

    def test_the_print_test_answers_202_and_says_if_the_agent_is_online(self):
        """202 e nao 200: o comando foi enfileirado, nao "a bobina saiu".

        Quem imprime e o agente, quando o stream entregar. Sem
        `agent_is_online` o lojista aperta o botao, ve sucesso e fica olhando
        uma impressora que esta desligada desde ontem.
        """
        operation = self.paths["/admin/branches/{branch_id}/print-test"]["post"]

        self.assertIn("202", operation["responses"])
        self.assertNotIn("200", operation["responses"])

        ref = operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(ref, "#/components/schemas/PrintTestResponse")

        response = self.schema["components"]["schemas"]["PrintTestResponse"]
        self.assertIn("agent_is_online", response["required"])


class PrinterNameContractTests(unittest.TestCase):
    """`printer_name` publicado nos tres lugares em que ele e lido.

    E o campo que conserta o rename silencioso: antes dele, o vinculo setor
    -> impressora existia so no config.ini da maquina do balcao e era casado
    pelo NOME do setor. Renomear "Cozinha" no painel fazia a via cair na
    impressora padrao e a comanda da cozinha comecar a sair no balcao, sem
    erro em lugar nenhum.

    Se ele nao estiver no documento, o painel nao tem como grava-lo e o
    agente nao tem como saber que deve le-lo — e o defeito volta inteiro.
    """

    @classmethod
    def setUpClass(cls):
        cls.components = app.openapi()["components"]["schemas"]

    def test_the_sector_response_publishes_the_printer(self):
        sector = self.components["PrintingSectorResponse"]

        self.assertIn("printer_name", sector["properties"])

    def test_the_printer_is_optional_and_nullable(self):
        """Nulo NAO e ausencia de dado: e "resolva pelo config.ini", que e o
        comportamento anterior a coluna.

        Publicar o campo como obrigatorio faria o painel exigir uma escolha
        para salvar qualquer setor, e toda loja ja instalada — que imprime
        pelo config.ini ha meses — teria que ser reconfigurada para conseguir
        editar o nome de um setor.
        """
        sector = self.components["PrintingSectorResponse"]

        self.assertNotIn("printer_name", sector.get("required", []))
        types = {option["type"] for option in sector["properties"]["printer_name"]["anyOf"]}
        self.assertEqual(types, {"string", "null"})

    def test_the_panel_can_write_the_printer_on_create_and_on_edit(self):
        # Sem isto o campo seria so de leitura: a tela mostraria a impressora
        # escolhida e nao teria como escolher nenhuma.
        for name in ("PrintingSectorCreate", "PrintingSectorUpdate"):
            self.assertIn("printer_name", self.components[name]["properties"], name)
            self.assertNotIn("printer_name", self.components[name].get("required", []))

    def test_the_print_job_carries_the_printer_to_the_agent(self):
        """O agente le esta via do documento como qualquer outro cliente.

        `printer_name` fora daqui e o agente continuando a resolver a
        impressora pelo NOME do setor no config.ini — que e exatamente o que
        quebra no rename.
        """
        job = self.components["PrintJobResponse"]

        self.assertIn("printer_name", job["properties"])
        # Opcional pelo mesmo motivo do setor, e mais um: a via do cliente e
        # a de resgate ("SEM SETOR") nao pertencem a setor nenhum e caem na
        # impressora padrao do agente, como sempre foi.
        self.assertNotIn("printer_name", job["required"])


if __name__ == "__main__":
    unittest.main()
