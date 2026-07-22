# Cupons no backend Rapidex

## Escopo implementado

Foi implementado um cupom por pedido, sem alterações no frontend, nas regras de cashback ou em descontos por produto/categoria. `CouponTemplate` permanece como preset visual e `RestaurantCoupon` passa a ser a fonte da configuração e das regras da campanha.

## Arquivos alterados

- `src/models/coupon_model.py`: configuração real da campanha.
- `src/models/coupon_redemption_model.py`: registro de aplicação e reversão.
- `src/models/order_model.py`: vínculo, snapshot e totais de desconto.
- `src/models/__init__.py`: exports dos models.
- `src/schemas/coupon_schema.py`: contratos administrativos, listagem e preview.
- `src/schemas/order_schema.py`: seletor de cupom e campos financeiros nas respostas.
- `src/schemas/customer_schema.py`: campos financeiros no histórico.
- `src/repositories/coupon_repository.py`: consultas, contagens, lock e persistence de redemptions.
- `src/repositories/menu_repository.py`: menu legado limitado a campanhas públicas no período válido.
- `src/services/coupon_service.py`: validação e cálculo centralizados.
- `src/services/order_service.py`: aplicação transacional no pedido.
- `src/services/admin_order_service.py`: reversão em cancelamento/rejeição.
- `src/services/customer_service.py`: exposição segura no histórico.
- `src/services/menu_service.py`: mantém o contrato legado usando os dados reais da campanha.
- `src/api/endpoints/coupons.py` e `main.py`: novas rotas.
- `migrations/20260722_add_coupon_campaigns.sql`: atualização idempotente, índices e constraints.
- `tests/test_coupons.py`: cobertura funcional e transacional de cupons.
- `tests/test_delivery_estimate.py`: fixtures de sucesso tornadas determinísticas com tempo de preparo.

## Rotas criadas

- `GET /restaurants/{slug}/coupons/available` — autenticação opcional; aceita `subtotal`, `delivery_fee` e `order_type`.
- `POST /restaurants/{slug}/coupons/preview` — exige cliente autenticado e não grava redemption.
- `GET /admin/restaurants/{restaurant_id}/coupons` — exige a autenticação administrativa existente.
- `POST /admin/restaurants/{restaurant_id}/coupons` — cria campanha.
- `PATCH /admin/restaurants/{restaurant_id}/coupons/{coupon_id}` — atualiza ou encerra com `is_active=false`.

O endpoint de menu mantém sua lista `coupons` para compatibilidade, mas ela não deve ser usada para elegibilidade individual. A fonte de elegibilidade é `/coupons/available`.

## Regra financeira

Todos os cálculos novos usam `Decimal` e arredondamento `ROUND_HALF_UP` em duas casas.

- Fixo: `min(discount_value, subtotal)`.
- Percentual: `subtotal * discount_value / 100`, limitado por `max_discount_amount` quando configurado e nunca acima do subtotal.
- Frete grátis: valor real da entrega; para retirada, a entrega considerada é zero.
- Pedido mínimo: avaliado somente sobre o subtotal real de produtos e opções, antes de entrega, taxa, cupom e cashback.
- Total: `subtotal + delivery_fee + service_fee - discount_total`.

Subtotal, desconto e total enviados como campos extras pelo cliente são ignorados. O backend recalcula produtos, opções, entrega, taxa e cupom.

## Fluxo de redemption

Na criação do pedido, a campanha é buscada com `SELECT FOR UPDATE`, revalidada e calculada. Pedido, snapshot do código, desconto, itens, histórico e `CouponRedemption(status="applied")` são gravados na mesma transação. `order_id` e `idempotency_key` possuem unicidade para impedir consumo duplicado.

Em `cancelled` ou `rejected`, a redemption passa de `applied` para `reversed` e recebe `reversed_at`. A operação é idempotente, e usos revertidos não entram nas contagens futuras.

## Testes

Cobertura adicionada para cálculos fixo, percentual, teto, frete grátis, mínimo, estado e validade, limites total/cliente, primeira compra, anonimato, código sem distinção de caixa, seletor único, restaurante incorreto, preview sem persistência, idempotência/reversão, serialização do último uso, valores não confiáveis do frontend, total final, snapshot, redemption e cancelamento.

Resultado local:

```text
57 passed in 3.27s
```

O Windows deste ambiente não possui o alias `python` funcional; a mesma execução solicitada foi feita pelo launcher oficial instalado: `py -m pytest`.

## Pendências operacionais

- Aplicar `migrations/20260722_add_coupon_campaigns.sql` no ambiente PostgreSQL/Supabase antes do deploy da aplicação.
- Executar um teste de concorrência de integração contra o PostgreSQL do ambiente de homologação; a suíte unitária valida o lock/recheck e a migration cria as garantias de unicidade, mas não abre duas conexões reais ao Supabase.
- Nenhuma alteração de frontend foi realizada.
