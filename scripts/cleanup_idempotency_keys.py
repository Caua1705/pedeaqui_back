"""Remove chaves, estimativas, codigos, feedback, comentario e relato.

O nome do arquivo ficou para tras: ele cuida de SEIS tabelas hoje, nao so
de `idempotency_keys`. Nao foi renomeado de proposito — o nome aparece no
`command` do container `limpeza` em `docker-compose.yml`, na secao "Uso"
abaixo e em `docs/`, e trocar tres referencias para ganhar um nome melhor nao
paga o risco de o container subir apontando para um arquivo que nao existe.

Rodar por cron. Apagar linha vencida NAO afeta correcao: passado o TTL a
chave ja nao protege mais nada, e reenviar um pedido de 24h atras deve
mesmo criar um pedido novo. Vale o mesmo para `delivery_estimates`: depois
do TTL a estimativa nao pode mais ser reaproveitada, entao a linha so ocupa
espaco. O objetivo aqui e so nao deixar as tabelas crescerem para sempre.

Os codigos de verificacao entraram na frente 5 (LGPD) por um motivo
diferente dos outros dois: o que aquelas duas tabelas acumulavam para sempre
era o E-MAIL de todo mundo que ja pediu um codigo, muito depois de o codigo
ter deixado de servir para qualquer coisa. Nao e espaco em disco, e dado
pessoal sem prazo.

`ai_feedback` entrou depois, pelo mesmo motivo dos codigos e com o mesmo
formato: `user_message` guarda em texto puro o que a pessoa digitou para o
Rapi, e a tabela nao tem `customer_id` — a exclusao de conta nunca alcancou
aquelas linhas. Quem sabe ate quando e `chat_service.feedback_retention_cutoff`.

`order_reviews` e a QUINTA, e a UNICA que nao apaga linha: ela apaga so o
`comment`, e a nota fica. Apagar a linha levaria a nota junto e reescreveria
a media historica do lojista todo mes. Ela tambem e a unica com duas defesas
— a exclusao de conta ja limpa o comentario de quem TEM conta, e este
expurgo cobre o que sobra, que e o pedido de convidado (`customer_id` nulo,
inalcancavel a partir de conta nenhuma). Quem sabe ate quando e
`order_review_service.review_retention_cutoff`.

`admin_error_reports` e a SEXTA, e entrou pelo mesmo motivo do `ai_feedback`:
`description` e `error_log` guardam texto livre escrito por um lojista, que
descreve um cliente que nao tem como saber que o registro existe, e a tabela
nao tem `customer_id` — nenhuma exclusao de conta vai alcanca-la, hoje ou
nunca. Quem sabe ate quando e
`admin_error_report_service.error_report_retention_cutoff`.

As seis apagam poucas linhas por execucao — dezenas ou centenas —, entao
todas cabem numa transacao so, com um commit no fim. Foi a sexta tabela
(`menu_events`, do funil do cardapio) que exigia expurgo EM LOTES, e ela saiu
junto com a frente inteira de funil e origem: se algum dia entrar aqui uma
tabela que cresca naquela ordem de grandeza, o expurgo dela nasce em lotes com
commit entre um e outro, e nao pendurado neste commit unico.

E os codigos NAO sao cortados pelo `expires_at`, ao contrario dos outros dois. A
linha continua valendo depois de o codigo vencer — para o teto de reenvios e
para o token de reset — e quem sabe ate quando e
`auth_service.codes_retention_cutoff`, que explica as duas janelas.

Uso:

    python scripts/cleanup_idempotency_keys.py
    python scripts/cleanup_idempotency_keys.py --dry-run

No container:

    docker exec pedeaqui-api python scripts/cleanup_idempotency_keys.py
"""

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import func, select

from src.db.session import SessionLocal
from src.models.admin_error_report_model import AdminErrorReport
from src.models.ai_feedback_model import AIFeedback
from src.models.customer_model import EmailVerificationCode, PasswordResetCode
from src.models.delivery_estimate_model import DeliveryEstimate
from src.models.order_review_model import OrderReview
from src.models.idempotency_key_model import IdempotencyKey
from src.repositories.admin_error_report_repository import AdminErrorReportRepository
from src.repositories.ai_feedback_repository import AIFeedbackRepository
from src.repositories.delivery_estimate_repository import DeliveryEstimateRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.idempotency_repository import IdempotencyRepository
from src.repositories.order_review_repository import OrderReviewRepository
from src.services.admin_error_report_service import error_report_retention_cutoff
from src.services.auth_service import codes_retention_cutoff
from src.services.chat_service import feedback_retention_cutoff
from src.services.order_review_service import review_retention_cutoff
from src.utils.security import utcnow


def conferir_despejo_do_redis(redis_client=None) -> int:
    """Grita se o Redis andou DESPEJANDO chave. Nunca levanta, nunca falha o script.

    ===================================================================
    POR QUE ISTO MORA NO CONTAINER DE LIMPEZA
    ===================================================================

    O Redis sobe com `--maxmemory 256mb --maxmemory-policy volatile-lru`, e
    `volatile-lru` escolhe a vitima entre as chaves **com TTL**. Todas as
    nossas tem: o contador de rate limit (a janela E o TTL), a estimativa de
    entrega, e o cache de embedding do Rapi.

    O RISCO CONCRETO E O CACHE DERRUBAR O RATE LIMIT. Um embedding ocupa 6 KB;
    um contador de limite, algumas dezenas de bytes. Sob pressao, um contador
    parado ha trinta segundos e "menos recentemente usado" que um embedding
    lido ha dez — e sai. E sair nao da erro nenhum: o `slowapi` le o contador
    ausente como zero, o cliente ganha orcamento novo, e o limite deixa de
    valer **em silencio**. O `in_memory_fallback_enabled` nao ajuda aqui: ele
    cobre o Redis fora do ar, nao o Redis respondendo com uma chave que foi
    apagada.

    POR QUE DIARIO, E NAO NO BOOT. `evicted_keys` e um acumulado que so zera
    quando o **Redis** reinicia, nao quando a API reinicia. Conferir no boot da
    API mediria uma janela que nao tem relacao com o contador. Aqui roda uma
    vez por dia, no container que ja existe, com cadencia propria.

    POR QUE NAO E ERRO. Este script esta encadeado com `&&` ao
    `expire_cashback.py`, que VENCE SALDO DE CLIENTE. Falhar aqui impediria a
    expiracao de rodar por causa de uma leitura de diagnostico — trocar um
    aviso por um bug de dinheiro. Devolve sempre 0.
    """
    if redis_client is None:
        from src.ai.services.chat_cache import cliente_redis

        redis_client = cliente_redis()
    if redis_client is None:
        print("[redis] sem REDIS_URL; despejo nao conferido.")
        return 0

    try:
        info = redis_client.info()
    except Exception as erro:  # noqa: BLE001
        print(f"[redis] nao deu para ler o INFO ({type(erro).__name__}); seguindo.")
        return 0

    def numero(chave: str) -> int:
        valor = info.get(chave, info.get(chave.encode(), 0))
        try:
            return int(valor)
        except (TypeError, ValueError):
            return 0

    despejadas = numero("evicted_keys")
    usada_mb = numero("used_memory") / (1024 * 1024)
    teto_mb = numero("maxmemory") / (1024 * 1024)

    if despejadas == 0:
        print(
            f"[redis] evicted_keys=0 | used_memory={usada_mb:.1f} MB"
            f" de {teto_mb:.0f} MB. Ok."
        )
        return 0

    # O numero esperado e ZERO, para sempre. Diferente disso e um estado que
    # ninguem projetou, e a linha tem que dizer quem e o suspeito — senao o
    # aviso vira um numero esperando alguem lembrar dele.
    # Uma linha por ideia, e cada uma com o prefixo `[redis]`: o stdout deste
    # container e lido com `docker logs | grep`, e um aviso de quatro linhas
    # sem prefixo perde tres quartos de si no primeiro grep.
    for linha in (
        f"ATENCAO: evicted_keys={despejadas} | "
        f"used_memory={usada_mb:.1f} MB de {teto_mb:.0f} MB.",
        "O Redis esta despejando chave por falta de memoria, e `volatile-lru` "
        "nao distingue as nossas: o CONTADOR DE RATE LIMIT pode estar sendo "
        "apagado junto, e um contador apagado vira limite que nao vale — sem "
        "erro e sem log.",
        "Suspeito principal: o cache de embedding do Rapi (`emb:v1:*`), que "
        "ocupa ~6 KB por pergunta contra dezenas de bytes de um contador. "
        "Confira com `redis-cli --bigkeys` e `redis-cli info keyspace`.",
        "Conserto estrutural: segunda instancia de Redis so para o cache do "
        "Rapi, com `maxmemory` proprio. Banco separado (`/0` vs `/1`) NAO "
        "resolve — `maxmemory` e a politica de despejo sao por INSTANCIA. "
        "Ver a armadilha 41 da skill `rapidex-backend`.",
    ):
        print(f"[redis] {linha}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove chaves, estimativas, codigos, feedback, comentarios e relatos vencidos."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas conta o que seria apagado, sem apagar.",
    )
    args = parser.parse_args()

    now = utcnow()
    cutoff = codes_retention_cutoff(now)
    feedback_cutoff = feedback_retention_cutoff(now)
    review_cutoff = review_retention_cutoff(now)
    relato_cutoff = error_report_retention_cutoff(now)
    with SessionLocal() as db:
        if args.dry_run:
            keys = db.scalar(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.expires_at <= now)
            )
            estimates = db.scalar(
                select(func.count())
                .select_from(DeliveryEstimate)
                .where(DeliveryEstimate.expires_at <= now)
            )
            codes = sum(
                db.scalar(
                    select(func.count()).select_from(modelo).where(modelo.created_at < cutoff)
                )
                for modelo in (EmailVerificationCode, PasswordResetCode)
            )
            feedback = db.scalar(
                select(func.count())
                .select_from(AIFeedback)
                .where(AIFeedback.created_at < feedback_cutoff)
            )
            comentarios = db.scalar(
                select(func.count())
                .select_from(OrderReview)
                .where(
                    OrderReview.created_at < review_cutoff,
                    OrderReview.comment.is_not(None),
                )
            )
            relatos = db.scalar(
                select(func.count())
                .select_from(AdminErrorReport)
                .where(AdminErrorReport.created_at < relato_cutoff)
            )
            print(
                f"[dry-run] {keys} chave(s), {estimates} estimativa(s), "
                f"{codes} codigo(s), {feedback} feedback(s) e {relatos} relato(s) "
                f"seriam removidos e {comentarios} comentario(s) de avaliacao "
                f"seriam limpos."
            )
            # Tambem no dry-run: ler o INFO do Redis nao muda nada, e quem
            # chama o dry-run a mao costuma estar justamente investigando.
            conferir_despejo_do_redis()
            return 0

        removed_keys = IdempotencyRepository(db).delete_expired(now=now)
        removed_estimates = DeliveryEstimateRepository(db).delete_expired(now)
        removed_codes = CustomerRepository(db).delete_codes_created_before(cutoff)
        removed_feedback = AIFeedbackRepository(db).delete_created_before(feedback_cutoff)
        cleared_comments = OrderReviewRepository(db).clear_comments_created_before(review_cutoff)
        removed_reports = AdminErrorReportRepository(db).delete_created_before(relato_cutoff)
        db.commit()

        print(
            f"{removed_keys} chave(s), {removed_estimates} estimativa(s), "
            f"{removed_codes} codigo(s), {removed_feedback} feedback(s) e "
            f"{removed_reports} relato(s) removidos; {cleared_comments} "
            f"comentario(s) de avaliacao limpos."
        )

    # DEPOIS do commit, e fora do `with`: e diagnostico, nao expurgo, e nao
    # tem nada que segurar a transacao aberta.
    conferir_despejo_do_redis()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
