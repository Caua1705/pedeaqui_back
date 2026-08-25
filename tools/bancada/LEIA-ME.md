# Bancada do assistente — como rodar

Mora em `tools/bancada/`, no repositório do backend, e é versionada: quando
`POST /voice/search` ou os contadores do `/ended` mudarem, esta página muda no
mesmo commit. Não toca no app do cliente — só chama rotas que já existem.

Versionada, mas **fora da imagem Docker**: o `.dockerignore` a exclui do
`COPY . .`. Ela roda na máquina de quem desenvolve, não no servidor.

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
nada lá. `localhost` também é contexto seguro, que é o que o microfone exige.
De outro aparelho na rede não funciona: o navegador recusa microfone em `http://`
que não seja localhost.

## Para a voz funcionar, são duas chaves

Uma no ambiente, outra no banco. As duas.

1. **`VOICE_ENABLED=true`** no `.env` de produção, e reiniciar a API.
   Desligada, as rotas `/voice` **não existem** — a bancada recebe 404.

2. **A coluna do restaurante**, por SQL (não há campo no painel, de propósito —
   quem paga a OpenAI é a plataforma):

   ```sql
   UPDATE restaurant_settings SET voice_enabled = true
    WHERE restaurant_id = '<uuid do Júnior>';
   ```

   Sem isso: **403** `"O atendimento por voz nao esta disponivel neste restaurante."`

O `restaurant_id` sai do próprio botão **Resolver loja e filiais**.

## A ordem na tela

1. **Resolver loja e filiais** — busca o `restaurant_id` pelo slug e lista as
   filiais. A Matriz vem marcada sozinha (`is_main`); os dois UUIDs ficam à
   vista para conferir.
2. **Entrar** com um cliente de verdade. Só a emissão da voz exige login; o
   texto não.
3. Texto e voz podem rodar na mesma página, na mesma loja e filial.

## O que limita o teste

| | |
|---|---|
| sessões de voz por cliente | **5 a cada 24 h** — costuma acabar antes do dinheiro |
| sessões por restaurante | 100 / 24 h |
| emissão de credencial | 3/min e 20/h por IP |
| teto de uma sessão | 300 s |
| silêncio que encerra | 45 s (com aviso falado 10 s antes) |
| mensagens de texto | 20/min, 200/h |

Precisando de mais que 5 conversas de voz num dia: use outra conta de cliente,
ou suba `VOICE_SESSIONS_PER_CUSTOMER_PER_DAY`.

## Custo

- **Voz:** US$ 0,006 por minuto ouvido, US$ 0,024 por minuto falado. Uma sessão
  até o teto de 5 min dá **US$ 0,09 a US$ 0,13**. Uma conversa normal de 2 min
  fica em torno de **US$ 0,04**. Cinco sessões (a cota do dia) = **menos de
  US$ 0,70**.
- **Texto:** ordem de **US$ 0,001 por mensagem** — a saída é limitada a 300
  tokens no servidor. Uma conversa de 10 idas e vindas não chega a um centavo.
- A caixinha **transcrever o meu áudio** liga um serviço à parte, cobrado por
  minuto. Deixe desligada quando quiser o número de custo limpo.

Emitir a credencial não custa nada: o relógio começa quando a conexão de áudio
abre. Aba esquecida não fatura para sempre — teto, inatividade e aba escondida
encerram sozinhos.

## Onde os números da tela não são medidos

- **Texto, "1ª palavra" = "fim".** O `POST /chat` não faz streaming: devolve o
  JSON pronto. Não existe primeira palavra antes da última, e esse número só
  melhora encurtando a resposta inteira. A coluna "cabeçalhos" mostra o quanto
  haveria a ganhar se a rota passasse a transmitir aos poucos.
- **Texto, tokens = estimativa.** O `/chat` não devolve `usage`. A saída sai de
  caracteres÷4; a entrada é o campo editável. Ordem de grandeza, não fatura.
- **Voz, tokens = reais.** Vêm do `usage` de cada `response.done`, e são os
  mesmos seis contadores que a bancada reporta no `/ended` — o que alimenta
  `scripts/voice_usage_report.py`.
- **Voz, "1ª palavra" = medida de verdade**, do fim da sua fala até o primeiro
  som no alto-falante, lido do próprio áudio. A transcrição chega depois do som
  e aparece só como número secundário.
- **Texto não tem tool call visível.** A busca no cardápio roda dentro do
  servidor; o cliente só vê os produtos que voltaram. Na voz a tool é do
  navegador, então argumentos e latência aparecem inteiros.
