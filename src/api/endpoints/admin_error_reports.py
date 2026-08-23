"""O botao de reportar erro do painel.

**Papel: `PESSOAS`.** Quem mais esbarra em erro e quem passa o dia na tela, e
esse e o atendente — fechar o relato na gerencia faria o erro chegar de
segunda mao, contado por quem nao viu. Nao ha nada aqui que uma conta de
balcao possa estragar: a rota so escreve, so escreve no proprio restaurante
(que sai do token) e nao le nada.

**`print_agent` fica de fora**, como em toda rota que nao esta na lista curta
do agente. Maquina nao relata erro: o que o agente de impressao tem a dizer
sobre falha ele diz no heartbeat.

**Nao existe rota de LEITURA, e e decisao.** O destinatario do relato e a
plataforma, nao o lojista — `scripts/error_reports.py` le do banco. Uma
`GET /admin/error-reports` daria ao painel uma tela que ninguem pediu, com
texto escrito por outros usuarios da mesma loja dentro, e publicaria no
OpenAPI (armadilha 16) um contrato que teriamos que manter para sempre.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    PESSOAS,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_error_report_schema import (
    CreateErrorReportRequest,
    ErrorReportResponse,
)
from src.services.admin_error_report_service import AdminErrorReportService


router = APIRouter(prefix="/admin/error-reports", tags=["admin error reports"])


@router.post(
    "",
    response_model=ErrorReportResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def create_error_report(
    payload: CreateErrorReportRequest,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> ErrorReportResponse:
    """Registra o relato e devolve o numero dele.

    O corpo leva so o que o lojista sabe: a historia, o log que a tela
    capturou, a tela e opcionalmente o numero do pedido. Restaurante, filial e
    usuario saem do token — mandar qualquer um deles no corpo e 422, por
    `extra="forbid"`.

    Credencial que venha no texto (`Authorization`, JWT, `Idempotency-Key`,
    `tracking_token`, campo de senha) e mascarada antes de o registro existir.
    O relato inteiro e apagado em 90 dias.
    """
    return AdminErrorReportService(db).create(scope, payload)
