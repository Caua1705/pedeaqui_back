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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
