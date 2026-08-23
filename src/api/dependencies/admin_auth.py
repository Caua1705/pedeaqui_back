"""Injeta o lojista autenticado nas rotas /admin.

Toda rota /admin recebe um AdminUser, e o `restaurant_id` dele e a unica
fonte de escopo que existe: desde a Fase 3 nenhuma rota /admin aceita
restaurante no path ou no corpo, entao nao ha o que confrontar — era
exatamente essa confianca que permitia ler pedido de outro restaurante.

Quem quiser o escopo pronto (restaurante + filial) usa `get_admin_scope`,
em `admin_scope.py`. Esta dependencia continua exposta para as duas rotas
que so precisam da identidade do lojista, nao do recorte de dados: `GET
/admin/auth/me` e a emissao do ticket de stream.
"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.services.admin_auth_service import AdminAuthService


bearer_scheme = HTTPBearer(auto_error=False)

# As duas rotas que quem esta com senha temporaria ainda alcanca: descobrir
# quem e, e trocar a senha. Sem a primeira o painel nao tem como saber para
# onde mandar a pessoa; sem a segunda ela nao tem como sair do estado.
ROTAS_LIBERADAS_COM_SENHA_TEMPORARIA = frozenset(
    {"/admin/auth/me", "/admin/auth/password"}
)

TROQUE_A_SENHA = "Troque a senha temporaria antes de usar o painel"


def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token ausente"
        )
    admin_user = AdminAuthService(db).get_admin_from_token(credentials.credentials)
    _ensure_temporary_password_was_changed(admin_user, request)
    return admin_user


def _ensure_temporary_password_was_changed(admin_user: AdminUser, request: Request) -> None:
    """Senha temporaria abre a troca de senha, e mais nada.

    **A checagem e do BACKEND, e nao so da tela.** A senha temporaria atravessa
    um canal informal — WhatsApp, papel, voz no balcao —, e o que limita o
    prejuizo disso e a troca obrigatoria. Se ela morasse so no painel, quem
    interceptasse a senha chamaria a API direto e a limitacao seria enfeite:
    a lista de clientes com telefone e o faturamento estao a um `curl` de
    distancia da tela que estaria "obrigando" a troca.

    Mora em `get_current_admin` porque e por ele que passa TODA rota /admin com
    Bearer, inclusive `exigir_papel` e `get_admin_scope`. As duas que nao
    passam ficam cobertas de graca: `POST /admin/auth/login` nao tem token, e o
    stream SSE autentica por ticket — que so se obtem numa rota que passa aqui.

    403 e nao 401: o token e valido e a identidade e conhecida. Um 401 mandaria
    o painel para a tela de login, que e exatamente onde a pessoa nao resolve
    nada — ela ja entrou.

    O sinal que o painel deve obedecer NAO e este 403, e sim o
    `must_change_password` que `POST /admin/auth/login` e `GET /admin/auth/me`
    ja devolvem. Isto aqui e a rede embaixo.
    """
    if not admin_user.must_change_password:
        return
    rota = request.scope.get("route")
    if getattr(rota, "path", None) in ROTAS_LIBERADAS_COM_SENHA_TEMPORARIA:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=TROQUE_A_SENHA)
