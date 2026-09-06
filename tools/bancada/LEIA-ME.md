# Bancada do assistente — como rodar

Mora em `tools/bancada/`, no repositório do backend, e é versionada: quando o
contrato do `POST /chat` mudar, esta página muda no mesmo commit. Não toca no
app do cliente — só chama rotas que já existem.

Versionada, mas **fora da imagem Docker**: o `.dockerignore` a exclui do
`COPY . .`. Ela roda na máquina de quem desenvolve, não no servidor.

> **A metade da VOZ saiu daqui em 06/09/2026**, com o assistente falado. O que
> este arquivo descrevia sobre credencial efêmera, WebRTC, cota de sessão,
> tarifa de áudio e microfone não existe mais — nem na página, nem no backend.
> O que ficou é o `/chat`.

## Não abra com duplo clique

`file://` tem origem `null`, e o CORS da API de produção só aceita origem
conhecida (`main.py`). Toda chamada falharia antes de sair. Sirva de uma porta
que já está na lista:

```bash
cd tools/bancada
python -m http.server 5500
```

Ou, da raiz do projeto, duplo clique no **`bancada.bat`**: ele sobe a API,
serve esta pasta e abre o navegador.

E abra <http://localhost:5500/bancada.html>.

`5500`, `5501` e `5173` já estão liberadas no backend — não é preciso mexer em
nada lá.

## A ordem na tela

1. **Resolver loja e filiais** — busca o `restaurant_id` pelo slug e lista as
   filiais. A Matriz vem marcada sozinha (`is_main`); os dois UUIDs ficam à
   vista para conferir.
2. **Entrar** com um cliente de verdade, se quiser. O `/chat` responde sem
   login — o que muda com ele é a saudação e o que o histórico alcança.
3. Enviar. `session_id` é o histórico; **Nova sessão** começa do zero.

## O que limita o teste

| | |
|---|---|
| mensagens de texto | 20/min, 200/h por IP (`CHAT_RATE_LIMIT`) |
| teto da resposta | `AI_MAX_COMPLETION_TOKENS`, 800 tokens |

## Custo

Ordem de **US$ 0,001 por mensagem**, mais um embedding por pergunta nova — e
esse some quando o cache de busca acerta (20 min, compartilhado entre clientes
quando há `REDIS_URL`). Uma conversa de 10 idas e vindas não chega a um
centavo.

O número **cobrado** não é o desta tela: é o que
`GET /internal/ai-usage` devolve, lido do `usage` que o backend gravou em
`ai_usage_events`. Ver [`docs/custo-de-ia.md`](../../docs/custo-de-ia.md).

## Onde os números da tela não são medidos

- **"1ª palavra" = "fim".** O `POST /chat` não faz streaming: devolve o JSON
  pronto. Não existe primeira palavra antes da última, e esse número só melhora
  encurtando a resposta inteira. A coluna "cabeçalhos" mostra o quanto haveria a
  ganhar se a rota passasse a transmitir aos poucos.
- **Tokens = estimativa.** O `/chat` não devolve `usage` para a página. A saída
  sai de caracteres÷4; a entrada é o campo editável. Ordem de grandeza, não
  fatura.
- **Não há tool call visível.** A busca no cardápio roda dentro do servidor; o
  cliente só vê os produtos que voltaram.
