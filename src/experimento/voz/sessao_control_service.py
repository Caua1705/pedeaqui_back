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

from sqlalchemy.orm import Session

from src.core.config import settings
from src.experimento.voz.sessao_repository import VozSessaoRepository
from src.experimento.voz.sessao_service import desligar_na_openai, emitir_credencial_efemera
from src.models.ai_voice_session_model import AIVoiceSession


logger = logging.getLogger("uvicorn.error")

SEM_CALL_ID = "vencida sem call_id — servidor nao consegue desligar"


class VozSessaoControlService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = VozSessaoRepository(db)

    def abrir(
        self,
        restaurant_id: uuid.UUID,
        customer_id: uuid.UUID | None,
        restaurant_context: str,
    ) -> tuple[AIVoiceSession, dict]:
        """Varre o que venceu, emite a credencial e registra a sessao nova.

        A varredura vem ANTES da emissao para o custo de uma sessao esquecida
        parar no momento em que alguem comeca outra, e nao depois.

        A credencial e pedida antes de gravar a linha: emissao recusada pela
        OpenAI nao pode deixar sessao fantasma no livro-razao consumindo cota.
        """
        self.encerrar_vencidas()

        credencial = emitir_credencial_efemera(restaurant_id, restaurant_context)
        expira_em = _agora() + timedelta(seconds=settings.VOZ_DURACAO_MAXIMA_SEGUNDOS)
        sessao = self.repository.registrar(
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            expires_at=expira_em,
        )
        self.db.commit()

        logger.info(
            "[Experimento voz] sessao registrada | sessao_id=%s | restaurant_id=%s "
            "| customer_id=%s | expira_em=%s",
            sessao.id,
            restaurant_id,
            customer_id,
            expira_em.isoformat(),
        )
        return sessao, credencial

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
            "[Experimento voz] conexao reportada | sessao_id=%s | call_id=%s",
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
            desligar_na_openai(sessao.openai_call_id)

        sessao.ended_at = _agora()
        sessao.ended_reason = motivo[:200]
        self.db.commit()
        logger.info(
            "[Experimento voz] sessao encerrada | sessao_id=%s | motivo=%s",
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
                desligar_na_openai(sessao.openai_call_id)
                sessao.ended_reason = "teto de duracao — desligada pelo servidor"
            else:
                logger.warning(
                    "[Experimento voz] sessao vencida sem call_id | sessao_id=%s "
                    "| restaurant_id=%s",
                    sessao.id,
                    sessao.restaurant_id,
                )
                sessao.ended_reason = SEM_CALL_ID
            sessao.ended_at = agora

        self.db.commit()
        logger.info("[Experimento voz] varredura de vencidas | fechadas=%d", len(vencidas))
        return len(vencidas)


def _agora() -> datetime:
    return datetime.now(timezone.utc)
