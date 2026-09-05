"""Soma os avisos de WhatsApp por restaurante. So leitura.

Irmao de `ai_usage_service`, e a simetria para no nome: aquele GRAVA e le, este
so le. A diferenca nao e falta de simetria — e que `whatsapp_messages` ja e o
livro-razao. Ela nasceu para responder "o cliente foi avisado?", tem uma linha
por (pedido, tipo de aviso) desde a revisao `20260905_0053`, e responde
"quantos templates sairam" sem nenhuma coluna a mais.

Por isso este arquivo nao tem gancho de gravacao em lugar nenhum, e por isso o
item nao precisou de migracao.
"""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from src.repositories.whatsapp_usage_repository import WhatsAppUsageRepository
from src.schemas.whatsapp_usage_schema import (
    WhatsAppUsageByKind,
    WhatsAppUsageByRestaurant,
    WhatsAppUsageReportResponse,
)


class WhatsAppUsageService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = WhatsAppUsageRepository(db)

    def templates_por_restaurante(
        self,
        desde: datetime,
        ate: datetime,
        restaurant_id: uuid.UUID | None = None,
    ) -> WhatsAppUsageReportResponse:
        """Um bloco por restaurante, com os quatro tipos de aviso dentro.

        O repositorio devolve uma linha por (restaurante, tipo); o que esta
        funcao faz e agrupar. A soma acontece aqui e nao numa segunda consulta
        de propósito: duas consultas sobre a mesma janela podem discordar se
        uma linha for gravada entre elas, e o total que nao bate com a soma das
        partes e o tipo de defeito que ninguem confere.
        """
        linhas = self.repository.templates_por_restaurante_e_tipo(
            desde=desde, ate=ate, restaurant_id=restaurant_id
        )

        blocos: dict[uuid.UUID, WhatsAppUsageByRestaurant] = {}
        for linha in linhas:
            bloco = blocos.get(linha.restaurant_id)
            if bloco is None:
                bloco = WhatsAppUsageByRestaurant(
                    restaurant_id=linha.restaurant_id,
                    restaurant_name=linha.restaurante,
                    templates=0,
                    sent=0,
                    refused_here=0,
                    refused_by_meta=0,
                    by_kind=[],
                )
                blocos[linha.restaurant_id] = bloco

            bloco.templates += linha.templates
            bloco.sent += linha.enviados
            bloco.refused_here += linha.recusados_aqui
            # DERIVADO, e nao um quarto contador na consulta: "a Meta recusou" e
            # exatamente "nao saiu e nao fomos nos". Uma quarta condicao no SQL
            # poderia divergir desta subtracao sem que nada acusasse.
            bloco.refused_by_meta += linha.templates - linha.enviados - linha.recusados_aqui
            bloco.by_kind.append(
                WhatsAppUsageByKind(
                    kind=linha.tipo,
                    templates=linha.templates,
                    sent=linha.enviados,
                )
            )

        restaurantes = list(blocos.values())
        return WhatsAppUsageReportResponse(
            start=desde,
            end=ate,
            restaurants=restaurantes,
            total_templates=sum(bloco.templates for bloco in restaurantes),
            total_sent=sum(bloco.sent for bloco in restaurantes),
        )
