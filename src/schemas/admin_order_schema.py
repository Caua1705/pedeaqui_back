from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AdminOrderListItem(BaseModel):
    id: UUID
    order_number: int
    customer_name_snapshot: str
    customer_phone_snapshot: str
    order_type: str
    status: str
    # A cozinha precisa ver o pagamento junto do status: um pedido em
    # `pending` com `payment_status='pending'` esta esperando o gateway, nao
    # esperando o lojista.
    payment_method: str | None = None
    payment_status: str
    total: float
    created_at: datetime | None = None


class UpdateOrderStatusRequest(BaseModel):
    """Corpo do PATCH de status.

    `changed_by` foi REMOVIDO do contrato: quem mudou passou a sair do token
    do lojista (AdminOrderService._admin_signature). Era texto livre vindo do
    cliente, entao o historico do pedido registrava qualquer autor que o
    painel quisesse escrever. Clientes antigos que ainda mandam o campo nao
    quebram — o Pydantic ignora chave desconhecida —, ele so nao tem mais
    efeito.
    """

    status: str
    note: str | None = None
