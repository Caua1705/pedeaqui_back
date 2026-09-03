"""Injeta o ENTREGADOR autenticado nas rotas `/courier`.

Nao e Bearer, nao e JWT, nao e `admin_user`. Sao duas credenciais que
viajam juntas em TODA requisicao:

- o link, no caminho (`/courier/{link_token}/...`): 256 bits, sorteado
  pelo painel, guardado em sha-256 — a mesma disciplina do `tracking_token`;
- o codigo, no cabecalho `X-Courier-Code`: seis digitos, nunca na URL,
  guardado em HMAC com o proprio link como chave.

Sem sessao de proposito. E isso que faz "regenerar o codigo mata o link
velho NA HORA" ser verdade literal: nao ha token derivado que sobreviva a
troca dos dois hashes. O preco e um SELECT por requisicao, o mesmo que o
Bearer do lojista ja paga para recarregar o `admin_user`.

Rota `/courier` nenhuma aceita Bearer, e rota `/admin` nenhuma aceita este
par: `scripts/escopo_das_rotas.py` cobra que toda rota `/courier` receba o
`Courier` daqui e nao aceite identificador nenhum de fora.
"""

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.models.courier_model import Courier
from src.services.courier_delivery_service import CourierDeliveryService


COURIER_CODE_HEADER = "X-Courier-Code"


def get_current_courier(
    link_token: str,
    x_courier_code: str | None = Header(
        default=None,
        alias=COURIER_CODE_HEADER,
        description="O codigo de 6 digitos entregue junto com o link. Vai em toda requisicao.",
    ),
    db: Session = Depends(get_db),
) -> Courier:
    return CourierDeliveryService(db).authenticate(link_token, x_courier_code)
