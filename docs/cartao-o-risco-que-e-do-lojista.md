# Cartão: o risco que é do lojista

Este documento não é sobre código. É **o que precisa ser dito ao lojista antes
do primeiro pedido pago com cartão** — e a frase para dizer está abaixo, pronta.

Ele existe porque a decisão de não implementar 3DS foi tomada com o risco
assumido, em 25/08/2026. Risco assumido que ninguém comunica não é risco
assumido: é surpresa agendada.

---

## A frase

> "No cartão, o dinheiro cai direto na sua conta do Mercado Pago — e é por isso
> que a contestação também é sua. Se o cliente reclamar a compra no banco dele,
> o Mercado Pago debita o valor de você e me avisa; eu não tenho como segurar
> esse dinheiro, porque ele nunca passou por mim. Isso se chama chargeback, e no
> delivery é raro — mas não é zero. Existe uma proteção que passa esse risco
> para o banco do cliente; ela custa um passo a mais no checkout e ainda não
> está ligada, de propósito. Se acontecer uma vez com você, me avisa no mesmo
> dia: guarde o comprovante de entrega, que é o que se usa para contestar de
> volta, e eu ligo a proteção."

**Diga inteira, e diga antes.** A metade que costuma ser cortada — "eu não tenho
como segurar esse dinheiro" — é justamente a que evita a ligação de quem acha
que a plataforma reembolsa.

---

## Por que é verdade

A credencial do Mercado Pago é **do restaurante** (`restaurant_payment_credentials`,
uma linha por restaurante e por ambiente). A cobrança é criada em nome da conta
dele e o dinheiro cai na conta dele. A plataforma nunca é custodiante: não há
saldo nosso de onde debitar um estorno, e `application_fee` (o split) não está
preenchido — não há contrato de marketplace.

Consequência direta: **contestação, estorno e chargeback acontecem inteiramente
entre o cliente, o banco dele e a conta do lojista.** O nosso backend só fica
sabendo pelo webhook, e o que ele faz é registrar (`refunded_amount`,
`payment_status`). Ver `docs/pagamentos-e-comissao.md`, seções 4 e 8.

E a comissão da plataforma **não é reduzida** por estorno parcial (seção 7 do
mesmo documento). Isso é decisão consciente, e é mais uma coisa que o lojista
precisa ouvir de nós antes de descobrir sozinho no extrato.

---

## O que o 3DS mudaria, e por que está desligado

Numa transação autenticada por 3DS 2.0, a **responsabilidade pelo chargeback
passa para o emissor** — o banco do cliente. É esse o ganho que justifica o
trabalho, e ele cresce com o ticket médio. O segundo ganho é aprovação maior:
transação autenticada passa mais.

Está desligado porque o desafio é **opt-in nosso** (`three_d_secure_mode:
"optional"` no corpo do pagamento), não imposto pelo emissor, e porque no ticket
de delivery chargeback quase não acontece. Sem o campo, `pending_challenge` não
existe e não há pedido pendurado.

**O gatilho para reconsiderar** — escrito aqui para não depender de alguém
lembrar:

- primeiro chargeback em qualquer restaurante da base, ou
- ticket médio de cartão subindo a ponto de um chargeback doer, ou
- lojista pedindo.

Qualquer um dos três tira o 3DS de "melhoria futura" e põe em trabalho. O que já
está levantado para o dia (ativação, formato do desafio, cartões de teste) está
na seção 8 do `docs/pagamentos-e-comissao.md`.

---

## Quando acontecer

1. **Comprovante de entrega é a defesa.** É o que o lojista manda para contestar
   de volta no painel do Mercado Pago. Sem ele, a contestação é perdida por
   omissão.
2. **Chargeback continua sendo do lojista, e o estorno automático não o
   alcança.** Desde 25/08/2026 cancelar um pedido pago devolve o dinheiro
   sozinho — mas isso é o lojista (ou a plataforma) decidindo devolver. Um
   *chargeback* é o portador contestando na operadora, sem passar por nós, e
   chega como `charged_back` no webhook: o dinheiro já saiu quando ficamos
   sabendo, e não há o que estornar. O grep que acha dinheiro de cliente parado
   continua sendo `sem estorno automatico`, e hoje ele só sai quando um estorno
   automático **falhou**.
3. **É o gatilho do item acima.** Um chargeback real muda a conta do 3DS, e a
   conversa com o lojista deixa de ser sobre risco hipotético.

---

## Onde mais isso aparece

- `docs/pagamentos-e-comissao.md` §8 — o que ficou de fora e por quê.
- `docs/pagamentos-e-comissao.md` §7.1 — **a taxa que o Mercado Pago não
  devolve no estorno**, com a frase pronta. É a outra conversa que pertence a
  este mesmo momento do cadastro: as duas falam de dinheiro que sai do lojista
  sem ele ter errado, e ouvir as duas juntas é melhor que descobrir uma pela
  fatura.
- `docs/onboarding-de-restaurante.md` — o momento de dizer a frase é o cadastro
  da credencial de cartão, não o primeiro pedido.
