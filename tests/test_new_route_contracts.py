import json
import unittest
import uuid

from pydantic import ValidationError

from main import app
from src.schemas.admin_printing_schema import PrintAgentCommandType
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

    def test_the_command_types_are_published_as_an_enum(self):
        """A LISTA de comandos tem que sair no documento.

        `PrintAgentCommandType` existe como `str, Enum` exatamente para isto
        (armadilha 16). Enquanto `command_type` foi publicado como `str`
        solto, o enum nao era referenciado por schema nenhum e nao chegava a
        gerador de cliente nenhum — o agente teria que descobrir os valores
        possiveis lendo o backend.
        """
        published = self.schema["components"]["schemas"]["PrintAgentCommandType"]["enum"]

        self.assertEqual(
            sorted(published), sorted(item.value for item in PrintAgentCommandType)
        )
        self.assertIn("print_test", published)

    def test_the_enum_is_reachable_from_the_stream_event(self):
        # Enum solto em components que nenhum schema referencia nao chega a
        # gerador de cliente nenhum.
        event = self.schema["components"]["schemas"]["PrintAgentCommandEvent"]

        self.assertIn("PrintAgentCommandType", json.dumps(event["properties"]["command_type"]))


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


class CardapioPorFilialContractTests(unittest.TestCase):
    """O `branch_id` tem que estar PUBLICADO em cada lugar em que ele decide.

    O painel e o app geram o cliente deles a partir do /openapi.json
    (armadilha 16). Um `branch_id` que exista no service e nao no documento e
    um parametro que ninguem consegue mandar — e nesse caso as rotas voltam a
    responder pela filial padrao, que e exatamente o defeito silencioso que a
    revisao 20260820_0026 fecha.
    """

    @classmethod
    def setUpClass(cls):
        cls.documento = app.openapi()
        cls.components = cls.documento["components"]["schemas"]

    def _corpo(self, caminho: str, metodo: str = "post") -> dict:
        referencia = self.documento["paths"][caminho][metodo]["requestBody"]
        nome = referencia["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
        return self.components[nome]

    def _parametros(self, caminho: str, metodo: str = "get") -> set[str]:
        return {
            parametro["name"]
            for parametro in self.documento["paths"][caminho][metodo].get("parameters", [])
        }

    def test_o_menu_publica_o_parametro_de_filial(self):
        self.assertIn("branch_id", self._parametros("/restaurants/{restaurant_slug}/menu"))

    def test_as_duas_rotas_de_produto_publicam_o_parametro(self):
        """Sem ele, o link de produto volta a ser ambiguo entre as lojas."""
        for caminho in (
            "/restaurants/{restaurant_slug}/products/{product_slug}",
            "/restaurants/{restaurant_slug}/categories/{category_slug}/products",
        ):
            self.assertIn("branch_id", self._parametros(caminho), caminho)

    def test_a_resposta_do_menu_diz_de_qual_filial_ela_e(self):
        menu = self.components["RestaurantMenuResponse"]

        self.assertIn("branch_id", menu["properties"])
        # `settings_branch_id` continua publicado: ele e o campo que o painel
        # ja consome, e some so quando os dois lados tiverem trocado.
        self.assertIn("settings_branch_id", menu["properties"])

    def test_o_payment_methods_morto_saiu_do_bloco_de_operacao(self):
        """Ele foi ecoado ali enquanto a coluna existiu, e podia discordar do
        que a filial de fato aceita (armadilha 15)."""
        settings = self.components["RestaurantSettingsResponse"]

        self.assertNotIn("payment_methods", settings["properties"])

    def test_o_chat_exige_a_filial(self):
        """OBRIGATORIO, e nao opcional com queda para a filial padrao.

        Opcional daria a MESMA resposta errada de antes — o Rapi oferecendo
        com preco um produto que a loja nao vende —, so que escondida atras de
        um caminho que parece configurado. Obrigatorio quebra o cliente que
        nao mandou, na primeira chamada, com 422 e o nome do campo.
        """
        corpo = self._corpo("/chat")

        self.assertIn("branch_id", corpo["properties"])
        self.assertIn("branch_id", corpo["required"])

    def test_as_duas_rotas_de_voz_exigem_a_filial(self):
        """A sessao E a busca. A busca nao recebe o id da sessao, entao nao ha
        de onde herdar a loja.

        Medido nos SCHEMAS e nao no /openapi.json: o router da voz so e
        montado com `VOICE_ENABLED`, e a suite roda com ele desligado — que e
        o padrao em todo lugar. Sem esta excecao, as duas rotas ficariam sem
        rede nenhuma.
        """
        from src.api.voice import BuscaRequest, SessaoRequest

        for schema in (SessaoRequest, BuscaRequest):
            campo = schema.model_fields["branch_id"]
            self.assertTrue(campo.is_required(), schema.__name__)

    def test_a_criacao_de_categoria_exige_a_filial_e_a_de_produto_nao(self):
        """A assimetria e proposital, e esta na decisao 3 do service.

        `AdminProductCreate.category_id` ja determina a loja. Pedir os dois
        abriria a possibilidade de um corpo com `branch_id` e `category_id`
        em desacordo, e o unico desfecho possivel seria um 400 que nao
        precisa existir.
        """
        self.assertIn("branch_id", self.components["AdminCategoryCreate"]["required"])
        self.assertNotIn("branch_id", self.components["AdminProductCreate"]["properties"])

    def test_a_chave_de_catalogo_e_publicada_para_leitura_e_escrita(self):
        """Sem ela no documento, o painel nao tem como ligar o produto de uma
        loja ao da outra — e a pergunta que ela existe para responder some."""
        self.assertIn("catalog_key", self.components["AdminProductResponse"]["properties"])
        for nome in ("AdminProductCreate", "AdminProductUpdate"):
            self.assertIn("catalog_key", self.components[nome]["properties"], nome)
            self.assertNotIn("catalog_key", self.components[nome].get("required", []), nome)

    def test_os_seis_relatorios_publicam_o_recorte_de_filial(self):
        for rota in (
            "commission", "summary", "sales-by-day",
            "payment-methods", "products", "cancellations",
        ):
            caminho = f"/admin/reports/{rota}"
            self.assertIn("branch_id", self._parametros(caminho), caminho)

    def test_o_relatorio_diz_de_que_recorte_ele_fala(self):
        """Nulo e "o restaurante inteiro", nunca "filial nenhuma"."""
        # `PaymentMethodsResponse` existe em dois modulos e o FastAPI
        # qualifica os dois pelo caminho do modulo. Nomear o do relatorio pelo
        # nome curto acharia o de `restaurant_schema` — ou nenhum dos dois.
        for nome in (
            "CommissionReportResponse",
            "SalesSummaryResponse",
            "SalesByDayResponse",
            "src__schemas__admin_report_schema__PaymentMethodsResponse",
            "ProductSalesResponse",
            "CancellationsResponse",
        ):
            self.assertIn("branch_id", self.components[nome]["properties"], nome)
            self.assertNotIn("branch_id", self.components[nome].get("required", []), nome)

    def test_o_ranking_de_produtos_publica_a_chave_de_catalogo(self):
        """E ela que explica por que duas lojas aparecem numa linha so."""
        self.assertIn("catalog_key", self.components["ProductSalesItem"]["properties"])

