"""O cliente desistindo do proprio pedido, antes do preparo.

Ate 25/08/2026 nao existia caminho nenhum: quem desistia tinha que ligar para
o restaurante e pedir que o lojista cancelasse pelo painel. Isso empurrava
para o lojista um trabalho que o cliente resolve sozinho — e, no pedido pago
por pix que o cliente desistiu em trinta segundos, atrasava a devolucao do
dinheiro ate alguem atender o telefone.

**Ate onde ele vai: `pending` e `accepted`.** Antes do preparo ninguem gastou
nada e desistir e barato para os dois lados. A partir de `preparing` o insumo
ja saiu do estoque, e quem decide quem come o prejuizo passa a ser o lojista
— que tem a rota do painel, com motivo obrigatorio e confirmacao explicita.
Deixar o cliente cancelar um pedido em producao daria a ele o poder de gerar
prejuizo com um toque, sem ninguem do outro lado saber por que. A regra mora
em `order_state_machine.ensure_customer_can_cancel`.

**A ESCRITA e a mesma do painel**, e isso e o ponto do arquivo:
`OrderStatusChangeService` e o unico lugar que grava status de pedido. Um
cancelamento pelo cliente com codigo proprio seria um caminho em que o cupom
nao volta, o cashback fica retido, o historico nao registra quem cancelou ou
o pagamento nao e estornado — quatro bugs de dinheiro por um copiar e colar.
O que este service acrescenta e so o que e DELE: quem autoriza, ate onde vai,
e como o motivo aparece no historico.

**Sao DUAS portas, e cada uma prova "fui eu" de um jeito.**

`cancel` autoriza pelo `tracking_token`: quem o tem foi quem fez o pedido.
Sem login de proposito — pedido de convidado e caso normal, e exigir conta ali
deixaria justamente o convidado sem saida.

`cancel_for_customer` autoriza pelo Bearer do cliente logado, e o vinculo sai
de `orders.customer_id`. Ela existe porque o token so vive no `localStorage`
do aparelho que fez o pedido: quem pediu pelo celular e abriu o app no
computador nao conseguia cancelar o PROPRIO pedido. A alternativa que o front
levantou — publicar o `tracking_token` no `OrderDetailResponse` — foi recusada,
e o motivo esta no docstring do metodo.

O que muda entre as duas e SO quem autoriza. A janela, o motivo, a assinatura
no historico e a escrita sao os mesmos.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.customer_model import Customer
from src.repositories.order_repository import OrderRepository
from src.schemas.order_schema import CustomerCancelOrderRequest, OrderDetailResponse
from src.services.order_state_machine import ensure_customer_can_cancel
from src.services.order_status_change_service import OrderStatusChangeService
from src.services.restaurant_service import RestaurantService


CUSTOMER_CANCEL_ROUTE = "POST /restaurants/{slug}/orders/track/{token}/cancel"

# A porta autenticada. Escopo de idempotencia PROPRIO, e nao o da rota do
# token: a mesma `Idempotency-Key` chegando pelas duas portas sao dois pedidos
# diferentes, e trata-los como repeticao de um so devolveria a resposta errada.
CUSTOMER_ACCOUNT_CANCEL_ROUTE = "POST /customers/me/orders/{id}/cancel"

# Como o cancelamento do cliente assina `order_status_history.changed_by`.
#
# Constante e nao f-string com dado do cliente: o campo e lido por gente
# (suporte, lojista) numa tela ao lado do nome e do telefone, que ja estao
# ali. Repetir identificacao no autor nao acrescenta nada e espalha dado
# pessoal por mais uma coluna.
CUSTOMER_SIGNATURE = "cliente"

# O motivo padrao quando o cliente nao escreve nenhum. O campo e OPCIONAL
# aqui e obrigatorio no painel, e a assimetria e proposital: exigir
# justificativa de quem desiste de um pedido que nem comecou vira um campo
# que todo mundo preenche com "a" — e o historico ganha ruido em vez de
# informacao. O que o suporte precisa saber (quem cancelou) esta em
# `changed_by`.
DEFAULT_REASON = "Cancelado pelo cliente"


class CustomerOrderCancelService:
    def __init__(self, db: Session):
        self.db = db
        self.order_repository = OrderRepository(db)
        self.restaurant_service = RestaurantService(db)
        self.status_change_service = OrderStatusChangeService(db)

    def cancel(
        self,
        restaurant_slug: str,
        tracking_token: str,
        payload: CustomerCancelOrderRequest | None = None,
    ) -> OrderDetailResponse:
        """Cancela o pedido de quem tem o token, se ainda der tempo.

        O estorno do pagamento sai de graca: quem cancela e o mesmo writer do
        painel, e ele ja devolve o dinheiro do pedido online. Um pix pago e
        cancelado em seguida volta sem ninguem ligar para lugar nenhum.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        order = self.order_repository.get_order_by_tracking_token(restaurant.id, tracking_token)
        if order is None:
            # 404 e nao 403: 403 confirmaria que aquele pedido existe.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado"
            )

        ensure_customer_can_cancel(order.status)

        return self.status_change_service.apply(
            order=order,
            restaurant_id=restaurant.id,
            new_status="cancelled",
            note=self._reason(payload),
            changed_by=CUSTOMER_SIGNATURE,
            # O escopo da idempotencia leva o PEDIDO e nao o cliente: nao ha
            # cliente autenticado aqui, e o token ja identifica um pedido so.
            # Sem isso, dois clientes diferentes reusando a mesma
            # `Idempotency-Key` colidiriam.
            requester=f"cliente:{order.id}",
            route=CUSTOMER_CANCEL_ROUTE,
            # A rota nao aceita `Idempotency-Key`, e o motivo e que ela nao
            # precisa: o segundo clique chega com o pedido ja em `cancelled`,
            # e a maquina de estados responde 409 sozinha. Chave a mais seria
            # contrato a mais para o app manter sem ganhar nada.
            idempotency_key=None,
        )

    def cancel_for_customer(
        self,
        customer: Customer,
        order_id: uuid.UUID,
        payload: CustomerCancelOrderRequest | None = None,
    ) -> OrderDetailResponse:
        """Cancela o proprio pedido do cliente logado, de qualquer aparelho.

        ## Por que ela existe, e por que o token NAO foi publicado

        O `tracking_token` so existe no `localStorage` de quem fez o pedido,
        entao quem pediu pelo celular e abriu o app no computador nao
        alcancava o proprio pedido. O front propos duas saidas, e esta e a
        outra: publicar o token no `OrderDetailResponse` foi recusado porque

        - **ele e credencial portadora, e nao um id.** Quem o tem abre o
          detalhe inteiro do pedido (nome, telefone, endereco), avalia,
          consulta o pagamento e cancela. Sao 256 bits sem rota de reemissao
          e sem prazo: nao da para revogar nem para expirar;
        - **este projeto ja o trata como segredo.** `CreateOrderResponse` diz
          "devolvido somente aqui"; `AdminErrorReportService` o MASCARA antes
          de o registro existir; e o docstring de `GET /me/orders/{id}` ja
          dizia, sobre o caminho autenticado, "nao ha token nenhum para
          guardar nem para vazar". Publica-lo contradiria uma decisao escrita
          em tres lugares;
        - **`OrderDetailResponse` nao e so do app.** E o `response_model` de
          tres rotas do painel tambem, entao o campo cairia na resposta do
          lojista junto;
        - **nao resolvia bem nem o caso pedido.** O app teria que buscar o
          detalhe e so entao chamar a rota do token, e o token passaria a
          existir no segundo aparelho — memoria, log, historico de URL. Uma
          identidade autenticada e revogavel viraria um segredo compartilhado
          que nao e nem uma coisa nem outra;
        - **nao tem volta.** Campo publicado nao se despublica sem quebrar
          contrato no app e no painel.

        Aqui nada disso acontece: o vinculo e `orders.customer_id`, o mesmo de
        `get_customer_order`, e um cliente so alcanca o proprio pedido mesmo
        tendo o UUID de outro.

        A rota do token CONTINUA existindo, e nao e redundancia: ela e a unica
        saida do convidado, que nao tem conta para autenticar.
        """
        order = self.order_repository.get_order_detail_for_customer(order_id, customer.id)
        if order is None:
            # 404 e nao 403, pelo mesmo motivo da outra porta: 403
            # confirmaria que aquele pedido existe.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado"
            )

        ensure_customer_can_cancel(order.status)

        return self.status_change_service.apply(
            order=order,
            # Do PEDIDO, e nao de uma busca por slug como na outra porta. O
            # `order.restaurant_id` e autoritativo (nao veio da URL), e nao
            # exigir o restaurante ativo aqui e proposital: uma loja
            # desativada com pedido pago em aberto nao pode ser motivo para o
            # cliente ficar sem cancelar — e sem o estorno que vem junto.
            restaurant_id=order.restaurant_id,
            new_status="cancelled",
            note=self._reason(payload),
            changed_by=CUSTOMER_SIGNATURE,
            # Aqui HA cliente autenticado, entao o escopo leva ele. Na outra
            # porta nao ha, e por isso ela usa o pedido.
            requester=f"cliente:{customer.id}",
            route=CUSTOMER_ACCOUNT_CANCEL_ROUTE,
            idempotency_key=None,
        )

    @staticmethod
    def _reason(payload: CustomerCancelOrderRequest | None) -> str:
        if payload is None or not payload.reason:
            return DEFAULT_REASON
        # O texto do cliente entra no historico PRECEDIDO do padrao: sem
        # isso, "mudei de ideia" sozinho na coluna nao diz de quem partiu, e
        # `changed_by` fica sendo o unico sinal.
        return f"{DEFAULT_REASON}: {payload.reason}"
