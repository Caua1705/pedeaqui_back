"""Nada que vai para o Redis carrega dado pessoal em claro.

Chave de Redis nao e lugar escondido: ela aparece em `KEYS`, em `SCAN`, em
`MONITOR`, em qualquer dump e no painel de qualquer Redis gerenciado. E o
VALOR aparece no `MONITOR` junto — `SETEX chave valor` mostra os dois.

`ChatCache.embedding_key` ja sabia disso e escrevia por extenso. Mesmo assim o
cache de estimativa punha a **coordenada da casa do cliente** na chave, com
quatro casas decimais (~11 m), e no valor. Ninguem desobedeceu de proposito:
quem escreveu aquela chave nao estava lendo aquele docstring.

**Regra que depende de alguem lembrar volta a ser quebrada.** Este arquivo e a
versao que nao depende de lembrar, e ele cobra as duas metades — a chave e o
valor —, porque consertar so uma deixaria o dado exatamente onde ele estava.

O varredor tem o metodo escrito por extenso em
`scripts/dados_pessoais_em_chave.py`.
"""

import json
import unittest
import uuid
from dataclasses import asdict
from decimal import Decimal

from scripts.dados_pessoais_em_chave import varrer
from src.ai.services.chat_cache import ChatCache
from src.services.delivery_estimate_service import (
    Coordinates,
    DeliveryEstimateCache,
    DeliveryEstimateResult,
)
from tests import fabricas


COORDENADA = Coordinates(latitude=-3.7512345, longitude=-38.5567891)


def _resultado(**sobrescritas) -> DeliveryEstimateResult:
    campos = {
        "serviceable": True,
        "reason": None,
        "message": None,
        "distance_km": 4.2,
        "travel_time_min": 18,
        "prep_time_min": 20,
        "prep_time_max": 30,
        "eta_min": 38,
        "eta_max": 48,
        "delivery_fee": 8.0,
        "provider": "google_routes",
        "fallback": False,
        "latitude": COORDENADA.latitude,
        "longitude": COORDENADA.longitude,
    }
    campos.update(sobrescritas)
    return DeliveryEstimateResult(**campos)


class OVarredorTests(unittest.TestCase):
    def test_nenhuma_chave_com_dado_pessoal_em_claro(self):
        achados = [
            achado
            for achado in varrer()
            if any(classe == "PESSOAL" for _, classe in achado[3])
        ]

        assert not achados, (
            "Chave de Redis com dado pessoal em claro. Ela aparece em KEYS, em "
            "MONITOR e em qualquer dump — use um digest, como "
            "`ChatCache.embedding_key` e `_cache_key` da estimativa.\n\n"
            + "\n".join(
                f"    {arquivo}:{linha}\n        {fonte}\n"
                + "\n".join(
                    f"        [{classe}] {expressao}" for expressao, classe in partes
                )
                for arquivo, linha, fonte, partes in achados
            )
        )

    def test_o_varredor_acusa_uma_chave_plantada(self):
        """Sem isto, o teste de cima poderia estar verde por nao achar NADA.

        E o modo de falha real deste varredor: ele so olha modulos que falam
        com Redis e so montagens com `:` no literal. Um refinamento a mais e
        ele para de enxergar, em silencio.
        """
        from scripts.dados_pessoais_em_chave import _classificar, _tem_namespace
        import ast

        plantada = ast.parse('f"cache:v1:{customer.phone}:{x}"').body[0].value

        self.assertTrue(_tem_namespace(plantada))
        self.assertEqual(_classificar("customer.phone"), "PESSOAL")

    def test_o_varredor_nao_acusa_o_que_ja_esta_certo(self):
        """O outro lado: digest e identificador NAO podem virar vermelho.

        Se virarem, a regra deixa de ser seguivel — nao ha como escrever uma
        chave util sem `restaurant_id` nem sem digest.
        """
        from scripts.dados_pessoais_em_chave import _classificar

        self.assertEqual(_classificar("self.message_digest(message)"), "hasheado")
        self.assertEqual(_classificar("payload.address_id"), "pseudonimo")
        self.assertEqual(_classificar("restaurant_id"), "pseudonimo")


class AChaveDaEstimativaTests(unittest.TestCase):
    def test_a_coordenada_nao_aparece_na_chave(self):
        """O achado que abriu este arquivo, afirmado.

        Quatro casas decimais sao ~11 metros: identifica uma casa, nao um
        bairro.
        """
        from src.services.delivery_estimate_service import DeliveryEstimateService

        chave = DeliveryEstimateService._cache_key(
            "junior-da-picanha",
            uuid.uuid4(),
            COORDENADA,
            20,
            30,
            fabricas.filial(),
            "abc123",
        )

        self.assertNotIn("-3.7512", chave)
        self.assertNotIn("-38.5567", chave)
        self.assertNotIn("3.75", chave)

    def test_a_chave_continua_separando_enderecos_diferentes(self):
        """O digest nao pode ter custado a funcao do cache.

        Duas coordenadas distantes tem que dar chaves diferentes — senao o
        cliente da outra ponta da cidade receberia a taxa do primeiro.
        """
        from src.services.delivery_estimate_service import DeliveryEstimateService

        filial = fabricas.filial()
        branch_id = uuid.uuid4()
        argumentos = ("junior", branch_id, None, 20, 30, filial, "abc123")

        perto = DeliveryEstimateService._cache_key(
            *argumentos[:2], COORDENADA, *argumentos[3:]
        )
        longe = DeliveryEstimateService._cache_key(
            *argumentos[:2],
            Coordinates(latitude=-3.9, longitude=-38.9),
            *argumentos[3:],
        )

        self.assertNotEqual(perto, longe)

    def test_a_mesma_coordenada_da_a_mesma_chave(self):
        """Deterministico, senao o cache nunca acerta e o Google e chamado sempre."""
        from src.services.delivery_estimate_service import DeliveryEstimateService

        filial = fabricas.filial()
        branch_id = uuid.uuid4()
        argumentos = ("junior", branch_id, COORDENADA, 20, 30, filial, "abc123")

        self.assertEqual(
            DeliveryEstimateService._cache_key(*argumentos),
            DeliveryEstimateService._cache_key(*argumentos),
        )

    def test_o_branch_id_fica_legivel_na_chave(self):
        """De proposito, e vale dizer por que.

        Identificador interno nao diz nada sobre a pessoa, e sem ele nao ha
        como varrer as chaves de uma filial para depurar cache. Hashear TUDO
        seria trocar privacidade que ja se tem por operacao que se perde.
        """
        from src.services.delivery_estimate_service import DeliveryEstimateService

        branch_id = uuid.uuid4()
        chave = DeliveryEstimateService._cache_key(
            "junior", branch_id, COORDENADA, 20, 30, fabricas.filial(), "abc123"
        )

        self.assertIn(str(branch_id), chave)


class OValorDaEstimativaTests(unittest.TestCase):
    """A outra metade. `SETEX chave VALOR` expoe o valor no MONITOR tambem."""

    def _cache_de_memoria(self) -> DeliveryEstimateCache:
        cache = DeliveryEstimateCache.__new__(DeliveryEstimateCache)
        cache.redis = None
        return cache

    def test_a_coordenada_nao_e_gravada_no_valor(self):
        cache = self._cache_de_memoria()

        cache.set("chave-de-teste", _resultado())

        gravado = DeliveryEstimateCache._memory["chave-de-teste"][1]
        self.assertNotIn("latitude", gravado)
        self.assertNotIn("longitude", gravado)
        self.assertNotIn("-3.7512345", gravado)

    def test_o_resto_do_resultado_continua_inteiro(self):
        """O corte nao pode ter levado junto o que o cache existe para guardar.

        Taxa, distancia e prazo sao o motivo de o cache existir — sem eles o
        Google seria chamado de novo em toda consulta.
        """
        cache = self._cache_de_memoria()

        cache.set("chave-de-teste", _resultado())
        de_volta = cache.get("chave-de-teste")

        self.assertEqual(de_volta.delivery_fee, 8.0)
        self.assertEqual(de_volta.distance_km, 4.2)
        self.assertEqual(de_volta.eta_min, 38)
        self.assertEqual(de_volta.provider, "google_routes")

    def test_a_coordenada_volta_NULA_do_cache(self):
        """E quem a recoloca e `estimate`, do destino que ele ja resolveu.

        Recolocar e ate mais correto que devolver a guardada: a chave agrupa
        por quatro casas decimais, entao um acerto pode vir de uma coordenada
        VIZINHA — e o pedido tem que gravar a do endereco que ele esta usando.
        """
        cache = self._cache_de_memoria()

        cache.set("chave-de-teste", _resultado())
        de_volta = cache.get("chave-de-teste")

        self.assertIsNone(de_volta.latitude)
        self.assertIsNone(de_volta.longitude)

    def test_um_valor_antigo_com_coordenada_nao_derruba_a_leitura(self):
        """Compatibilidade com o que ja esta no Redis agora.

        O `v2` da chave torna as entradas antigas inalcancaveis, mas o cache de
        MEMORIA nao tem versao — e um deploy no meio de uma requisicao pode
        deixar as duas formas convivendo. Ler o formato velho tem que devolver
        `None` (cache frio), nunca levantar: o preco de errar aqui e 500 no
        checkout por causa de cache.
        """
        cache = self._cache_de_memoria()
        formato_antigo = json.dumps(asdict(_resultado()))
        DeliveryEstimateCache._memory["chave-velha"] = (float("inf"), formato_antigo)

        self.assertIsNone(cache.get("chave-velha"))


class AChaveDaBuscaTests(unittest.TestCase):
    def test_a_mensagem_do_cliente_nao_aparece_na_chave_de_busca(self):
        """Este cache vive SO EM MEMORIA — e mesmo assim vai hasheado.

        O cache de embedding tambem vivia, e subir para o Redis foi uma
        mudanca de poucas linhas. Teria levado a pergunta do cliente junto.
        """
        cache = ChatCache()
        chave = cache.retrieval_key(
            uuid.uuid4(),
            uuid.uuid4(),
            "moro na rua das Flores 200, meu telefone e 85999990000",
            max_price=Decimal("20"),
        )

        self.assertNotIn("Flores", chave)
        self.assertNotIn("85999990000", chave)
        self.assertNotIn("moro", chave)

    def test_a_chave_de_busca_continua_separando_perguntas_diferentes(self):
        cache = ChatCache()
        restaurant_id = uuid.uuid4()
        branch_id = uuid.uuid4()

        uma = cache.retrieval_key(restaurant_id, branch_id, "picanha")
        outra = cache.retrieval_key(restaurant_id, branch_id, "pudim")

        self.assertNotEqual(uma, outra)


if __name__ == "__main__":
    unittest.main()
