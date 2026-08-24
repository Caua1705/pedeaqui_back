"""Idempotencia de requisicoes que criam ou mudam estado.

Problema: o cliente envia POST /orders, a resposta se perde (rede caiu, o
usuario tocou duas vezes, o app tentou de novo), ele reenvia e o
restaurante recebe dois pedidos identicos. O mesmo vale para o PATCH de
status, que hoje empilha uma linha em order_status_history por reenvio.

Desenho: o cliente manda um `Idempotency-Key` por operacao logica. Antes de
qualquer escrita, reservamos a chave com INSERT ... ON CONFLICT DO NOTHING
na MESMA transacao do trabalho real. Quem pegou a reserva executa; quem nao
pegou le a linha existente e devolve o que ja aconteceu.

Ficar na mesma transacao e o que torna o mecanismo confiavel: se o pedido
falhar no meio, o rollback leva a reserva junto e a chave volta a ser
utilizavel. Nao existe estado "reservado mas nada aconteceu" preso no banco.
"""

import hashlib
import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.idempotency_key_model import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_IN_PROGRESS,
)
from src.repositories.idempotency_repository import IdempotencyRepository
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")

MAX_IDEMPOTENCY_KEY_LENGTH = 200


class IdempotencyService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = IdempotencyRepository(db)
        self._reserved_id: uuid.UUID | None = None

    @staticmethod
    def build_scope(*, restaurant_id: uuid.UUID, route: str, requester: str) -> str:
        """Monta o escopo da chave.

        Tres componentes, todos necessarios:

        - `restaurant_id`: sem ele a chave e global e dois restaurantes que
          por acaso gerarem o mesmo UUID de chave colidem entre si — um
          tenant conseguiria ler a resposta gravada pelo outro.
        - `route`: a mesma chave reaproveitada em outra operacao (criar
          pedido e depois mudar status) tem que ser tratada como outra coisa.
        - `requester`: identidade de quem chamou (customer_id autenticado ou
          telefone do pedido guest). Impede que um cliente adivinhe a chave
          de outro e receba de volta a resposta dele, que carrega dados
          pessoais do pedido.
        """
        return f"{restaurant_id}|{route}|{requester}"

    @staticmethod
    def fingerprint(payload: Any) -> str:
        """sha256 do corpo canonicalizado.

        `sort_keys` para que a ordem das chaves no JSON do cliente nao mude o
        hash — so o conteudo importa.
        """
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def begin(
        self,
        *,
        scope: str,
        key: str | None,
        request_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Reserva a chave ou devolve a resposta ja gravada.

        Retorna None quando o chamador deve seguir e executar a operacao, e
        um dict quando a operacao ja foi executada antes e a resposta
        armazenada deve ser devolvida como esta.
        """
        if key is None:
            # Sem header: segue sem protecao. A justificativa esta em
            # _require_key_or_none.
            return None

        reserved_id = self.repository.reserve(
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            expires_at=utcnow() + timedelta(hours=settings.IDEMPOTENCY_TTL_HOURS),
        )
        if reserved_id is not None:
            self._reserved_id = reserved_id
            return None

        existing = self.repository.get(scope=scope, key=key)
        if existing is None:
            # A linha conflitante sumiu entre o INSERT e o SELECT — so
            # acontece se a limpeza por TTL rodou exatamente agora. Tratar
            # como conflito e mais seguro que criar um pedido duplicado.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Requisição em andamento. Tente novamente em instantes.",
            )

        if existing.request_fingerprint != request_fingerprint:
            # Mesma chave, corpo diferente: o cliente esta reciclando a
            # chave. Devolver a resposta antiga seria mentir sobre o que foi
            # gravado, entao recusamos.
            #
            # AS TRES RECUSAS DESTE METODO PEDEM COISAS DIFERENTES AO APP, e
            # duas delas compartilham o 409: "Requisicao em andamento" quer que
            # ele espere e repita a MESMA chave, e a de resposta gravada por
            # versao anterior quer que ele gere uma NOVA. O 422 daqui tambem
            # quer chave nova. Nao ha codigo de erro que as separe — hoje so o
            # texto as distingue, e por isso mexer nele nao e refatoracao: se um
            # dia o app precisar decidir entre os dois 409, o caminho e um enum
            # `str, Enum` no corpo, como o `PaymentErrorCode` (armadilha 16), e
            # nao um `if` sobre a frase.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Idempotency-Key já utilizada com um corpo diferente. "
                    "Gere uma nova chave para uma nova requisição."
                ),
            )

        if existing.status == IDEMPOTENCY_IN_PROGRESS:
            # Uma linha in_progress committed so aparece se o processo morreu
            # depois do commit da reserva e antes de concluir. Requisicoes
            # concorrentes normais nao chegam aqui: elas ficam bloqueadas no
            # indice unico dentro de repository.reserve e, quando destravam,
            # ja encontram completed.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Requisição em andamento. Tente novamente em instantes.",
            )

        if existing.status == IDEMPOTENCY_COMPLETED and existing.response_body is not None:
            logger.info(
                "[Idempotency] replay scope=%s key_digest=%s",
                scope,
                _key_digest(key),
            )
            return existing.response_body

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Requisição em andamento. Tente novamente em instantes.",
        )

    @staticmethod
    def parse_stored_response(model: type[BaseModel], response_body: dict[str, Any]):
        """Converte a resposta gravada de volta no schema de hoje.

        Uma resposta gravada ANTES de um deploy que acrescentou campo
        obrigatorio nao valida mais. Aconteceu na Fase 2: `tracking_token`,
        `payment_flow` e `payment_status` entraram em CreateOrderResponse, e
        chaves criadas antes do deploy ficam ate 24h no banco sem esses
        campos.

        Nesse caso respondemos 409 pedindo chave nova. As outras duas saidas
        seriam piores: 500 nao diz o que fazer, e seguir em frente criaria um
        SEGUNDO pedido — exatamente o que a chave existe para impedir.
        """
        try:
            return model.model_validate(response_body)
        except ValidationError:
            logger.warning(
                "[Idempotency] resposta gravada incompativel com o schema atual model=%s",
                model.__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A resposta original desta requisição foi gravada por uma "
                    "versão anterior da API. Gere uma nova Idempotency-Key."
                ),
            ) from None

    @property
    def has_reservation(self) -> bool:
        """True quando esta requisicao pegou a reserva.

        Serve para o chamador nao pagar a serializacao da resposta quando
        nao ha chave para gravar.
        """
        return self._reserved_id is not None

    def complete(
        self,
        *,
        response_body: dict[str, Any],
        order_id: uuid.UUID | None = None,
    ) -> None:
        """Marca a reserva como concluida e guarda a resposta.

        Nao commita: quem chama commita junto com o resto do trabalho, para
        que resposta gravada e efeito real fiquem na mesma transacao.
        """
        if self._reserved_id is None:
            return
        self.repository.complete(
            record_id=self._reserved_id,
            response_body=response_body,
            order_id=order_id,
        )


def normalize_idempotency_key(raw: str | None) -> str | None:
    """Valida o header e decide o que fazer quando ele nao vem.

    DECISAO: sem header, a requisicao passa SEM protecao de idempotencia
    (apenas com um warning no log), salvo se IDEMPOTENCY_REQUIRED estiver
    ligado.

    Por que nao rejeitar com 400: o front atual sempre envia a chave, mas um
    app antigo em campo nao. Rejeitar transforma um risco de "pedido
    duplicado, que o lojista cancela em dois cliques" em "pedido perdido e
    cliente sem jantar" — o modo de falha caro. O flag existe para o dia em
    que der para afirmar que nao ha cliente antigo; ai vira 400 sem mudar
    codigo.
    """
    if raw is None or not raw.strip():
        if settings.IDEMPOTENCY_REQUIRED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Header Idempotency-Key é obrigatório.",
            )
        logger.warning("[Idempotency] requisicao sem Idempotency-Key, seguindo sem protecao")
        return None

    key = raw.strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Idempotency-Key excede {MAX_IDEMPOTENCY_KEY_LENGTH} caracteres.",
        )
    return key


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
