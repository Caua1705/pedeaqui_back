"""O livro-razao das sessoes de voz e o corte do lado do SERVIDOR.

===========================================================================
O QUE ESTE ARQUIVO ALCANCA, E O QUE NAO ALCANCA
===========================================================================

Fechar so no cliente nao impede ninguem de manter a conexao por fora: o
javascript da pagina e do navegador, e quem quiser edita. Este arquivo e a
metade do controle que mora no servidor.

**O que da.** Toda credencial emitida vira linha em `ai_voice_sessions`, com
quem pediu, para qual restaurante, quando, e ate quando pode durar. Quando o
navegador reporta o `call_id`, o servidor passa a conseguir DESLIGAR aquela
chamada na OpenAI, e desliga as que passaram do teto.

**O que nao da.** O `call_id` de uma chamada WebRTC so aparece no cabecalho
`Location` da resposta que a OpenAI da AO NAVEGADOR. O servidor nao participa
dessa requisicao. Se o cliente nao reportar — porque foi modificado, ou porque
o navegador nao expoe o cabecalho — a sessao existe no livro-razao e o
servidor NAO consegue desliga-la.

Ou seja: o desligamento server-side protege contra cliente que coopera e
contra aba esquecida, e nao contra cliente hostil. **Quem barra o hostil e o
controle na EMISSAO** — login, cota e rate limit. Nenhuma linha daqui
substitui isso.

**Quando a varredura roda.** Na emissao de cada credencial nova. E
oportunista de proposito: sem trafego, uma sessao abandonada fica no livro
ate a proxima emissao. Fechar isso com pontualidade pede um processo
periodico, que esta rodada nao pediu.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.ai.voice.realtime_client import hangup_call, issue_client_secret
from src.core.config import settings
from src.models.ai_voice_session_model import AIVoiceSession
from src.repositories.voice_session_repository import VoiceSessionRepository


logger = logging.getLogger("uvicorn.error")

SEM_CALL_ID = "vencida sem call_id — servidor nao consegue desligar"


class VoiceSessionService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = VoiceSessionRepository(db)

    def abrir(
        self,
        restaurant_id: uuid.UUID,
        customer_id: uuid.UUID | None,
        restaurant_context: str,
    ) -> tuple[AIVoiceSession, dict]:
        """Varre o que venceu, confere a cota, emite a credencial e registra.

        A ordem das quatro perguntas e crescente em custo. A voz estar ligada
        neste restaurante e uma coluna; a varredura e uma consulta; a cota sao
        duas; a emissao e uma chamada de rede paga.

        A varredura vem ANTES da cota, e nao e detalhe: sessao vencida que
        ninguem fechou continuaria contando contra o cliente depois de ja ter
        acabado. E a credencial e pedida antes de gravar a linha: emissao
        recusada nao pode deixar sessao fantasma consumindo cota.
        """
        self._ensure_voz_habilitada(restaurant_id)
        self.encerrar_vencidas()
        self._conferir_cotas(restaurant_id, customer_id)

        credencial = issue_client_secret(restaurant_id, restaurant_context)
        expira_em = _agora() + timedelta(seconds=settings.VOICE_MAX_SESSION_SECONDS)
        sessao = self.repository.registrar(
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            expires_at=expira_em,
        )
        self.db.commit()

        logger.info(
            "[Voz] sessao registrada | sessao_id=%s | restaurant_id=%s "
            "| customer_id=%s | expira_em=%s",
            sessao.id,
            restaurant_id,
            customer_id,
            expira_em.isoformat(),
        )
        return sessao, credencial

    def _ensure_voz_habilitada(self, restaurant_id: uuid.UUID) -> None:
        """A voz deste restaurante esta ligada?

        Primeira pergunta de todas, e a mais barata: uma coluna. Quem nao tem
        voz nao gasta consulta de cota nem chamada a OpenAI.

        DUAS chaves, e as duas precisam estar ligadas. `VOICE_ENABLED` no
        ambiente e a mestra — desligada, o router nem sobe e esta funcao nunca
        e alcancada (`main.py`). `restaurant_settings.voice_enabled` decide
        loja por loja, e nasce falso para todo mundo: e o que permite colocar
        um restaurante no ar sem acender a base inteira.

        403 e nao 404 de proposito: o restaurante existe e o cliente esta na
        pagina dele. Esconder isso nao protege nada e so deixaria o front sem
        saber o que dizer.
        """
        if self.repository.voz_habilitada(restaurant_id):
            return

        logger.info(
            "[Voz] emissao recusada: voz desligada neste restaurante | restaurant_id=%s",
            restaurant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="O atendimento por voz nao esta disponivel neste restaurante.",
        )

    def _conferir_cotas(self, restaurant_id: uuid.UUID, customer_id: uuid.UUID | None) -> None:
        """Duas cotas, e a segunda existe porque a primeira pode nao bastar.

        A do CLIENTE segura o uso individual. A do RESTAURANTE e rede de
        seguranca: se aparecerem muitos clientes de uma vez — ou se a primeira
        cota tiver um furo que ninguem viu — o gasto de uma loja continua com
        teto.

        Recusa com 429 e o motivo por extenso. O cliente nao tem o que fazer
        com "limite atingido" sem saber qual, e quem le o log tambem nao.
        """
        desde = _agora() - timedelta(hours=settings.VOICE_QUOTA_WINDOW_HOURS)

        if customer_id is not None:
            do_cliente = self.repository.contar_do_cliente_desde(customer_id, desde)
            if do_cliente >= settings.VOICE_SESSIONS_PER_CUSTOMER_PER_DAY:
                logger.warning(
                    "[Voz] cota do cliente estourada | customer_id=%s | usadas=%d",
                    customer_id,
                    do_cliente,
                )
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Voce ja usou {do_cliente} conversas por voz nas ultimas "
                        f"{settings.VOICE_QUOTA_WINDOW_HOURS} horas. Tente mais tarde."
                    ),
                )

        do_restaurante = self.repository.contar_do_restaurante_desde(restaurant_id, desde)
        if do_restaurante >= settings.VOICE_SESSIONS_PER_RESTAURANT_PER_DAY:
            logger.warning(
                "[Voz] cota do restaurante estourada | restaurant_id=%s | usadas=%d",
                restaurant_id,
                do_restaurante,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="O atendimento por voz deste restaurante atingiu o limite do dia.",
            )

    def registrar_conexao(self, sessao_id: uuid.UUID, call_id: str) -> bool:
        """Guarda o `call_id` que o navegador leu do cabecalho `Location`.

        E a unica ponte entre uma sessao do livro-razao e a chamada de verdade
        na OpenAI. Sem ela o desligamento pelo servidor nao existe para aquela
        sessao.
        """
        sessao = self.repository.get(sessao_id)
        if sessao is None:
            return False

        sessao.openai_call_id = call_id
        self.db.commit()
        logger.info(
            "[Voz] conexao reportada | sessao_id=%s | call_id=%s",
            sessao_id,
            call_id,
        )
        return True

    def encerrar(self, sessao_id: uuid.UUID, motivo: str) -> bool:
        """Fecha a linha e, se houver `call_id`, desliga na OpenAI tambem.

        Desliga mesmo quando o cliente diz que ja encerrou: o cliente reporta o
        que ELE fez, e a conexao pode ter sobrevivido do outro lado.
        """
        sessao = self.repository.get(sessao_id)
        if sessao is None or sessao.ended_at is not None:
            return False

        if sessao.openai_call_id:
            hangup_call(sessao.openai_call_id)

        sessao.ended_at = _agora()
        sessao.ended_reason = motivo[:200]
        self.db.commit()
        logger.info(
            "[Voz] sessao encerrada | sessao_id=%s | motivo=%s",
            sessao_id,
            sessao.ended_reason,
        )
        return True

    def encerrar_vencidas(self) -> int:
        """Desliga o que passou do teto. Devolve quantas linhas foram fechadas.

        Sessao vencida SEM `call_id` tambem e fechada, com o motivo dizendo
        que nao deu para desligar. Deixar a linha aberta faria a varredura
        olhar para ela em toda emissao, para sempre, sem nunca poder fazer
        nada — e a contagem de sessoes abertas deixaria de significar alguma
        coisa.
        """
        agora = _agora()
        vencidas = self.repository.listar_vencidas_em_aberto(agora)
        if not vencidas:
            return 0

        for sessao in vencidas:
            if sessao.openai_call_id:
                hangup_call(sessao.openai_call_id)
                sessao.ended_reason = "teto de duracao — desligada pelo servidor"
            else:
                logger.warning(
                    "[Voz] sessao vencida sem call_id | sessao_id=%s "
                    "| restaurant_id=%s",
                    sessao.id,
                    sessao.restaurant_id,
                )
                sessao.ended_reason = SEM_CALL_ID
            sessao.ended_at = agora

        self.db.commit()
        logger.info("[Voz] varredura de vencidas | fechadas=%d", len(vencidas))
        return len(vencidas)


def _agora() -> datetime:
    return datetime.now(timezone.utc)
