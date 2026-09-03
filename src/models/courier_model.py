"""Entregadores: quem leva o pedido, e a que pedido cada um foi atribuido.

Duas tabelas, e a segunda existe para o dono conseguir PAGAR o motoboy: a
soma das taxas de um periodo sai das atribuicoes, com a taxa congelada no
momento em que cada uma foi feita.

O entregador NAO e `admin_user`. Ele nao tem tela no painel, nao tem Bearer e
nao alcanca rota nenhuma de `/admin`. O que ele tem e um link individual
(256 bits) e um codigo curto, os dois em HASH — ver `src/utils/security.py`
e `src/api/dependencies/courier_auth.py`.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Courier(Base):
    """Um motoboy de UMA filial.

    Da filial, e nao do restaurante, pelo criterio do resto do sistema: e um
    objeto fisico da loja, como o setor de impressao e o agente. O motoboy sai
    de uma cozinha, a taxa dele e configuracao daquela loja, e o gerente
    preso a uma filial precisa ver so os dele. Quem serve duas lojas tem dois
    cadastros (dois links).

    `restaurant_id` vai junto, como em `orders`, para o WHERE do repositorio
    barrar a filial de outro restaurante sem juncao.

    **Excluir e `deleted_at`, nunca DELETE.** `courier_assignments` referencia
    esta tabela, e o historico de corridas e o que o dono usa para pagar —
    ele tem que sobreviver ao motoboy que saiu. Excluido some de toda lista e
    perde o acesso na hora; inativo (`is_active = false`) continua listado
    (ferias) e perde o acesso tambem.

    As duas credenciais moram aqui em hash. `access_link_hash` e sha-256 sem
    chave do token de 256 bits do link (a mesma disciplina do
    `tracking_token`, e pelo mesmo motivo: entrada de 256 bits nao precisa de
    chave). `access_code_hash` e HMAC do codigo de 6 digitos com o PROPRIO
    link como chave: seis digitos precisam de chave contra forca bruta num
    dump, e o dump nao tem o link. Nulos = acesso nunca gerado ou revogado.

    **A trava por falhas mora aqui, e nao numa tabela de tentativas**, porque
    a pergunta e "este cadastro pode tentar agora?" — estado de uma linha,
    lido em TODA requisicao (o par viaja sempre, sem sessao). Quem escreve as
    tres e `CourierDeliveryService.authenticate`; quem as zera sao o acerto e
    a regeneracao do acesso pelo painel, que e a saida do motoboy travado.
    """

    __tablename__ = "couriers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # So digitos, pela mesma regra do telefone do cliente (armadilha 27): e o
    # que permite a unicidade por filial nao ser furada por um hifen.
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    access_link_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    access_code_hash: Mapped[str | None] = mapped_column(Text)
    access_generated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # A trava por falhas de codigo. Zero e "nunca errou", e por isso a coluna
    # e NOT NULL: nulo aqui nao teria significado nenhum, e `NULL >= 5` nao e
    # falso (armadilha 54). Os dois instantes sao nullable porque neles o
    # nulo SIGNIFICA — "nunca falhou" e "nao esta travado".
    access_failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    access_failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    access_blocked_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    branch = relationship("Branch")
    assignments = relationship("CourierAssignment", back_populates="courier")


class CourierAssignment(Base):
    """Um pedido nas maos de um entregador, do momento em que foi entregue a
    ele ate ser desatribuido.

    Tabela propria, e nao uma coluna em `orders`, por tres motivos: o
    historico de reatribuicao se perde numa coluna; a taxa e um snapshot com
    autor e instante proprios; e `orders` e a tabela mais sensivel do sistema
    — cada coluna nova nela e um schema de resposta a mais para tocar.

    **Uma atribuicao ATIVA por pedido**: indice parcial unico em `order_id`
    onde `unassigned_at IS NULL`. Reatribuir e fechar a antiga e abrir outra;
    o historico guarda as duas.

    `courier_fee_snapshot` e a taxa do entregador CONGELADA no momento da
    atribuicao, calculada a partir da configuracao da filial e da distancia
    que o pedido ja tinha gravado (`src/services/courier_fee.py`). Nulo
    significa "sem taxa configurada" e nunca zero: zero soma, e faria a
    corrida aparecer como gratis no historico que o dono usa para pagar.
    """

    __tablename__ = "courier_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    courier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("couriers.id"), nullable=False
    )
    assigned_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id")
    )
    assigned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    unassigned_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    unassigned_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id")
    )
    courier_fee_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # Copia de `orders.delivery_distance_km` no momento da atribuicao, para o
    # historico mostrar de onde a taxa saiu sem juncao. Nulo quando o pedido
    # foi precificado sem rota (Google fora do ar).
    distance_km_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    courier = relationship("Courier", back_populates="assignments")
    order = relationship("Order")
