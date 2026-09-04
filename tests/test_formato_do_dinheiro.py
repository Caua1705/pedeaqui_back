"""Dinheiro em dois formatos: a dívida não pode crescer enquanto a decisão não vem.

A armadilha 34 registra o estado e registra **por que ele não foi consertado**:
as duas direções mudam o formato de fio, o app do cliente consome essas
respostas, e JSON não tem número com casa decimal fixa — duas casas só existem
como string. A decisão é uma só, sobre a API inteira, tomada junto com o app.

O que faltava não era a decisão. Era **alguém cobrando que ela não fosse tomada
pela metade**, e nos dois sentidos:

- **converter um schema isolado** já foi tentado e revertido (`bffca0e`): o
  `CustomerOrderHistoryItem` virou tudo `float` e deixou `discount_total` como
  número em `/customers/me/orders` e como string em `/orders/{token}` — o mesmo
  campo com dois tipos em duas rotas;
- **um schema NOVO nascer misturado**, que é o lado que ninguém vigiava.

Este teste congela a lista de hoje. Ele não manda converter nada: manda a lista
não crescer.

A medição vem do `/openapi.json` **gerado**, e não dos `.py` — é o documento que
o front consome, e é nele que `Decimal` já aparece como `string`.
"""

import unittest

from main import app
from scripts.formato_do_dinheiro import (
    ESQUEMAS_QUE_MISTURAM,
    esquemas_que_misturam,
    formatos_por_esquema,
)


class OsQueMisturamOsDoisFormatosTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = app.openapi()

    def test_a_lista_congelada_nao_cresceu(self):
        """Schema novo misturando os dois formatos nasce vermelho aqui.

        É o lado que ninguém vigiava: a armadilha 34 protegia contra converter
        um schema isolado, e nada protegia contra criar mais um misturado."""
        novos = sorted(esquemas_que_misturam(self.spec) - ESQUEMAS_QUE_MISTURAM)

        self.assertEqual(
            novos,
            [],
            f"schema(s) novo(s) entregando dinheiro nos DOIS formatos no mesmo "
            f"objeto: {novos}. Enquanto a decisão da armadilha 34 não é tomada, "
            f"a dívida é a que já existe — e não uma a mais. Escolha um formato "
            f"para os campos novos, o mesmo dos vizinhos dentro daquela resposta.",
        )

    def test_a_lista_congelada_nao_encolheu_por_acidente(self):
        """O outro lado, e é o que já aconteceu uma vez.

        Um schema que sai da lista foi convertido — e converter um isolado é
        exatamente o que a armadilha 34 proíbe, porque deixa o mesmo campo com
        tipos diferentes em rotas diferentes. Se a conversão foi deliberada e
        vale para a API inteira, a lista sai daqui junto."""
        sumidos = sorted(ESQUEMAS_QUE_MISTURAM - esquemas_que_misturam(self.spec))

        self.assertEqual(
            sumidos,
            [],
            f"schema(s) que deixaram de misturar: {sumidos}. Se foi conversão "
            f"isolada, ela deixa o mesmo campo com dois tipos em rotas "
            f"diferentes — foi revertida uma vez por isso (`bffca0e`). Se foi a "
            f"decisão da API inteira, apague `ESQUEMAS_QUE_MISTURAM` junto.",
        )

    def test_os_tres_misturam_exatamente_os_mesmos_campos(self):
        """As três respostas são a MESMA divisão, e isso é o que a torna
        consertável de uma vez: quatro campos do pedido como número, três
        descontos como string. Se um dia divergirem, a conversão deixa de ser
        uma decisão e vira três."""
        achados = formatos_por_esquema(self.spec)

        divisoes = {
            nome: (
                tuple(sorted(achados[nome]["numero"])),
                tuple(sorted(achados[nome]["string"])),
            )
            for nome in ESQUEMAS_QUE_MISTURAM
        }

        self.assertEqual(len(set(divisoes.values())), 1, divisoes)
        numero, string = next(iter(divisoes.values()))
        self.assertEqual(
            numero, ("delivery_fee", "service_fee", "subtotal", "total")
        )
        self.assertEqual(
            string,
            (
                "cashback_redeemed_amount",
                "coupon_discount_amount",
                "discount_total",
            ),
        )


class OVarredorEnxergaTests(unittest.TestCase):
    """Varredor visto só respondendo "nenhum" não provou nada.

    O modo de falha real deste arquivo é o critério de "campo de dinheiro"
    parar de casar — e aí os três testes acima ficam verdes por vacuidade.
    """

    def test_reconhece_a_mistura_num_schema_plantado(self):
        plantado = {
            "components": {
                "schemas": {
                    "Plantado": {
                        "properties": {
                            "total": {"type": "number"},
                            "discount_total": {"type": "string"},
                        }
                    }
                }
            }
        }

        self.assertEqual(esquemas_que_misturam(plantado), {"Plantado"})

    def test_contagem_inteira_NAO_e_dinheiro(self):
        """`AdminProductListResponse.total` casa pelo nome e é contador.

        Sem este corte, metade do resultado seria contagem — e o teste da
        mistura acusaria schemas que não têm dinheiro nenhum."""
        plantado = {
            "components": {
                "schemas": {
                    "Listagem": {
                        "properties": {
                            "total": {"type": "integer"},
                            "revenue_total": {"type": "string"},
                        }
                    }
                }
            }
        }

        self.assertEqual(esquemas_que_misturam(plantado), set())

    def test_o_nulavel_e_achatado(self):
        """`float | None` chega como `anyOf`, e sem achatar ele não seria
        reconhecido como número nenhum."""
        plantado = {
            "components": {
                "schemas": {
                    "Nulavel": {
                        "properties": {
                            "courier_fee": {
                                "anyOf": [{"type": "number"}, {"type": "null"}]
                            },
                            "discount_total": {"type": "string"},
                        }
                    }
                }
            }
        }

        self.assertEqual(esquemas_que_misturam(plantado), {"Nulavel"})


if __name__ == "__main__":
    unittest.main()
