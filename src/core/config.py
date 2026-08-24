from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


#: O que `GIT_SHA` vale quando a imagem foi construida sem o build arg. Nao e
#: string vazia de proposito: ele APARECE no log, e um campo vazio numa linha
#: de log e indistinguivel de um campo que nao existe.
GIT_SHA_NAO_CARIMBADO = "nao-carimbado"


class Settings(BaseSettings):
    APP_NAME: str = "PedeAqui API"
    APP_ENV: str = "development"
    DEBUG: bool = True

    # O commit de que esta imagem foi construida. Entra pelo `ARG GIT_SHA` do
    # `Dockerfile`, que o `docker-compose.yml` preenche a partir do ambiente.
    #
    # POR QUE ISTO EXISTE. Em 24/08/2026, tres commits ficaram sem `push` e a
    # producao seguiu rodando o codigo anterior. Descobrir isso custou uma
    # bateria de medicao em producao e meia hora de arqueologia de log — e o
    # unico jeito que havia de saber qual codigo estava rodando era comparar
    # frases de log com `git log -S`. Nenhuma linha dizia a versao.
    #
    # O valor sai em tres lugares, de proposito, porque respondem a perguntas
    # diferentes: no boot (que imagem subiu), em `[AI /chat] Nova requisicao`
    # (de que build veio ESTE turno que estou medindo) e em `GET /health`
    # (qual esta no ar agora, sem precisar de log).
    #
    # NAO derruba o boot quando falta. E observabilidade, nao configuracao —
    # a mesma divisao de `warmup` contra `startup_checks`. O que ele faz e
    # gritar: ver a linha de aviso no lifespan de `main.py`.
    GIT_SHA: str = GIT_SHA_NAO_CARIMBADO

    # Deixe em None para seguir o APP_ENV (desligado em producao).
    # Defina explicitamente para forcar um dos dois lados.
    ENABLE_API_DOCS: bool | None = None

    DATABASE_URL: str

    SUPABASE_URL: str
    SUPABASE_STORAGE_BUCKET: str = "restaurant-assets"
    # Chave de servico do Supabase, usada SOMENTE para gravar no Storage
    # (upload de imagem do painel). O bucket e publico para leitura, entao
    # nada mais precisa dela. Vazia = o upload responde 503; o resto da API
    # continua funcionando.
    SUPABASE_SERVICE_ROLE_KEY: str | None = None
    SUPABASE_STORAGE_TIMEOUT_SECONDS: float = 15

    # DEPRECIADA na Fase 1. Nenhuma rota HTTP usa mais esta chave: as rotas
    # /admin passaram a exigir JWT de lojista. Continua opcional aqui apenas
    # para que um .env antigo nao derrube o boot; pode ser removida do
    # ambiente depois que a Fase 1 estiver no ar.
    INTERNAL_API_KEY: str | None = None
    CUSTOMER_AUTH_SECRET: str
    CUSTOMER_JWT_SECRET: str | None = None
    CUSTOMER_ACCESS_TOKEN_MINUTES: int = 10080
    PASSWORD_RESET_TOKEN_MINUTES: int = 15

    # Segredo dos tokens de lojista. Obrigatorio e SEPARADO do de cliente:
    # os dois publicos nao podem compartilhar chave de assinatura (armadilha
    # 32). Sem default de propriedade — o boot falha aqui, e nao meses depois.
    ADMIN_AUTH_SECRET: str
    # Jornada de lojista e turno de trabalho, nao sessao de semanas como a do
    # cliente: o token do painel expira em 12h.
    ADMIN_ACCESS_TOKEN_MINUTES: int = 720

    RESEND_API_KEY: str | None = None
    EMAIL_FROM: str = "Rapidex <no-reply@pederapidex.com>"
    EMAIL_CODE_SECRET: str
    PASSWORD_RESET_SECRET: str

    OPENAI_API_KEY: str
    MODEL_NAME: str = "gpt-5-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    VOICE_MODEL: str = "gpt-realtime-mini"
    VOICE_NAME: str = "marin"

    # O TETO DA RESPOSTA DO RAPI, E ELE E VALVULA DE SEGURANCA — NAO CONTROLE
    # DE COMPRIMENTO.
    #
    # Ficou em 300 fixo no codigo ate 24/08/2026, e nesse dia derrubou o
    # `/chat` em producao (build `1ca8708`): a resposta bateu no teto no meio
    # da lista de `selected_product_ids`, o JSON chegou cortado, o Pydantic
    # recusou e o cliente viu erro na tela.
    #
    # O ERRO DE LEITURA QUE FEZ O 300 PARECER SEGURO era achar que o teto
    # media o TEXTO da resposta: "o Rapi responde 120 tokens, 300 e o dobro
    # com folga". O teto mede o que o modelo GERA, e o texto e menos da
    # metade disso. Medido contra a API em 24/08/2026, com o cardapio do
    # piloto:
    #
    #     output_tokens cobrado                    129
    #     JSON inteiro, por tiktoken               104
    #       ├─ message (o texto que o cliente le)   41
    #       └─ 2 uuids de selected_product_ids      52
    #     andaime do structured output              25
    #
    # **O UUID CUSTA 26 TOKENS, e sao dois a tres por resposta.** Numa
    # resposta de recomendacao os ids sozinhos passam do texto: 52 contra 41
    # no exemplo acima. Esse e o item que estoura o teto, e ele nao aparece em
    # lugar nenhum da tela.
    #
    # NAO E RACIOCINIO. Foi a primeira hipotese na investigacao e ela nao se
    # sustentou: com `reasoning_effort="minimal"`, `reasoning` veio **0** em
    # todas as dezesseis chamadas medidas, e a soma acima fecha sem ele. Se
    # alguem trocar o `reasoning_effort` um dia, ai sim ele passa a disputar
    # este orcamento — e sera preciso refazer a conta.
    #
    # A conta do 800, para a resposta mais cheia que o prompt permite:
    #
    #     message de 2 paragrafos       ~200 tokens
    #     3 uuids (o teto de produtos)    78 tokens
    #     envelope JSON + andaime         35 tokens
    #                                   -------------
    #                                   ~315 tokens
    #
    # O turno mais longo realmente medido deu **242**, ou 81% dos 300 antigos
    # — quer dizer, o teto velho nao tinha margem nenhuma, e nao foi a
    # mudanca do prompt que criou o problema: ela so gastou a folga que ja
    # estava no fim. 800 e ~2,5x o pior caso.
    #
    # NAO CUSTA A MAIS: cobra-se por token GERADO, e o teto so decide onde a
    # geracao e cortada. Quem controla o comprimento e o prompt ("nunca passe
    # de 2 paragrafos", "no maximo 3 produtos"); bater aqui deve ser BUG, e
    # `_avisar_se_bateu_no_teto` passou a gritar quando acontece.
    #
    # SAI DO AMBIENTE pelo mesmo motivo de `MODEL_NAME`, e este incidente e a
    # prova: o unico botao capaz de reabrir o `/chat` naquele momento exigia
    # build e deploy para ser girado.
    AI_MAX_COMPLETION_TOKENS: int = 800



    # Piso de similaridade da busca vetorial. Abaixo disto o produto nao chega
    # ao modelo.
    #
    # 0,30 saiu de medicao contra o cardapio real (161 produtos, 15/08/2026), e
    # o que a medicao mostrou foi principalmente o que este numero NAO faz.
    #
    # As similaridades observadas ficam todas entre 0,17 e 0,66 — pergunta boa
    # ("picanha") da 0,66, e pergunta legitima porem vaga ("tem porcao para
    # dividir") da 0,42. Pergunta sem resposta no cardapio ("comida japonesa")
    # da 0,41. Os dois grupos SE SOBREPOEM: nao existe corte que separe
    # relevante de irrelevante, e qualquer valor acima de 0,42 comeca a matar
    # pergunta boa.
    #
    # O que 0,30 corta e so o absurdo — "consulta odontologica" topou em 0,19.
    # Serve para o modelo nao receber cinco produtos aleatorios quando a
    # pergunta nao e sobre comida. Nao serve, e nao ha numero que sirva, para
    # dizer "nao temos comida japonesa": isso e trabalho do prompt.
    #
    # Cuidado ao mexer: 0,7 ou 0,8 parecem valores razoaveis e devolveriam
    # SEMPRE zero produtos, porque o teto medido foi 0,66.
    AI_SEARCH_MIN_SIMILARITY: float = 0.30

    # Aquecimento no boot: uma consulta ao banco e uma chamada de embedding,
    # para o primeiro cliente do dia nao pagar a abertura das conexoes. Medido
    # em 24/08/2026: `embedding_ms=3534` na primeira requisicao do processo,
    # contra ~400 nas seguintes. Ver `src/core/warmup.py`.
    #
    # Desligado, a API sobe igual e o custo volta para a primeira pergunta.
    # `tests/conftest.py` o desliga: nenhum teste da suite chama servico
    # externo, e o warmup e a unica coisa do boot que chamaria.
    AI_WARMUP_ENABLED: bool = True

    # Quanto o boot espera POR ETAPA do aquecimento (banco, embedding, LLM).
    # Estourar nao cancela a chamada — a thread e daemon e segue aquecendo o
    # pool; o boot so para de esperar por ela. Ver `_with_timeout`.
    #
    # 8 s, e o numero desceu de 15 s em 24/08/2026 porque a premissa do 15
    # estava errada. Ela lia os 4844 ms do embedding como "boot normal" e
    # pedia 3x de folga sobre ele. Mas 4844 ms E o caso lento — e a chamada
    # FRIA, a unica que existe no boot, DNS e TLS inclusos. Folga de 3x sobre
    # o pior caso ja observado nao e margem, e teto que nunca dispara.
    #
    # E errar para MENOS aqui e barato, justamente por causa do desenho da
    # thread: estourar nao cancela nada, so para de esperar (ver
    # `_with_timeout`). O custo de disparar a toa e uma linha de log e um
    # aquecimento que termina alguns segundos depois do boot. O custo de nao
    # disparar e o boot pendurado 45 s servindo 502.
    #
    # O teto do BOOT e 3 x este valor: 24 s, contra 45 s antes. Ele so cabe
    # porque o servico da API nao tem healthcheck no compose — ver a
    # armadilha 40 da skill `rapidex-backend` antes de acrescentar um.
    AI_WARMUP_TIMEOUT_SECONDS: float = 8.0

    # Atendimento por voz em tempo real (src/ai/voice/, rotas sob /voice).
    #
    # Desligado, as rotas nao existem — nem no /docs. Ligado, elas sobem com
    # login de cliente, rate limit e cota; ver `src/api/voice.py`.
    VOICE_ENABLED: bool = False

    # Tetos de sessao de voz. O relogio da OpenAI comeca quando o navegador
    # abre a conexao de audio e nao para sozinho: aba esquecida aberta fatura
    # em silencio ate alguem fechar.
    #
    # Cinco minutos e mais do que qualquer duvida de cardapio precisa e o
    # suficiente para o custo de uma sessao ser um numero conhecido em vez de
    # uma incognita. Ver `docs/` e o cabecalho de sessao_service.py.
    VOICE_MAX_SESSION_SECONDS: int = 300
    # Silencio que encerra. Conta desde o ultimo `speech_started`.
    VOICE_IDLE_SECONDS: int = 45
    # Quanto antes do corte o atendente avisa por fala. Falar antes de cortar
    # e o que separa "encerrou" de "caiu" para quem esta do outro lado.
    VOICE_WARN_BEFORE_END_SECONDS: int = 10

    # Cota de voz. A unidade e SESSAO, e nao minuto, e a escolha nao e de
    # conveniencia: o backend conta emissao com CERTEZA e nao consegue medir
    # minuto — a conversa acontece entre o navegador e a OpenAI, e o servidor
    # nao a ve. Com o teto de 5 minutos por sessao, N sessoes valem no maximo
    # N x 5 minutos, que e um numero que da para prometer. Cota em minutos
    # seria palpite com cara de precisao.
    #
    # O CUSTO DE UMA SESSAO, de onde saem os numeros abaixo.
    #
    # Taxas da OpenAI para audio (`gpt-realtime-mini`), consultadas em
    # 15/08/2026: entrada 1 token por 100 ms (600 tok/min) a US$ 10 / 1M;
    # saida 1 token por 50 ms (1200 tok/min) a US$ 20 / 1M; entrada em cache
    # a US$ 0,30 / 1M. Ou seja US$ 0,006 por minuto ouvido e US$ 0,024 por
    # minuto falado.
    #
    # Uma sessao de 5 minutos com o atendente falando ~40% do tempo:
    #     entrada nova   5 min x 600 tok   = US$ 0,030
    #     saida          2 min x 1200 tok  = US$ 0,048
    #     contexto reenviado a cada turno, quase todo em cache  ~ US$ 0,008
    #     texto (instrucoes + resultado da busca), quase todo em cache < US$ 0,002
    #                                              -----------------------
    #                                              cerca de US$ 0,09
    # Com o atendente falando 70% do tempo, US$ 0,12. Use US$ 0,10 a US$ 0,13
    # como teto de uma sessao que vai ate o fim dos 5 minutos.
    #
    # Cinco sessoes por cliente = teto de ~US$ 0,55 por dia por pessoa.
    VOICE_SESSIONS_PER_CUSTOMER_PER_DAY: int = 5
    # A rede de seguranca: mesmo que a cota por cliente falhe ou que aparecam
    # muitos clientes de uma vez, o gasto de uma loja tem teto. Cem sessoes de
    # 5 minutos = ~US$ 11 por dia, no pior caso em que TODAS vao ate o fim.
    VOICE_SESSIONS_PER_RESTAURANT_PER_DAY: int = 100
    # Janela ROLANTE, e nao dia de calendario: um teto que zera a meia-noite
    # convida a gastar o dobro entre 23h59 e 00h01.
    VOICE_QUOTA_WINDOW_HOURS: int = 24

    # Varredura que mantem o indice do Rapi em dia (scripts/reindex_worker.py).
    #
    # Um minuto e o atraso maximo entre o lojista salvar e o Rapi saber. Podia
    # ser menos, mas cada ciclo e uma consulta sobre `products` inteiro: o custo
    # e proporcional ao TAMANHO DO CARDAPIO vezes a frequencia, e nao ao numero
    # de mudancas. Se um dia o catalogo crescer a ponto de isso pesar, o que
    # sobe e este numero — nao o desenho.
    AI_REINDEX_INTERVAL_SECONDS: int = 60
    # Teto de produtos por ciclo. Segura o estrago de uma importacao grande:
    # 500 produtos novos viram 10 ciclos de 50, e nao 500 chamadas seguidas a
    # OpenAI competindo com o resto.
    AI_REINDEX_BATCH_SIZE: int = 50

    GOOGLE_MAPS_ROUTES_API_KEY: str = ""
    GOOGLE_MAPS_ROUTES_BASE_URL: str = "https://routes.googleapis.com"
    GOOGLE_MAPS_TIMEOUT_SECONDS: float = 5
    GOOGLE_MAPS_ROUTING_PREFERENCE: str = "TRAFFIC_AWARE"
    DELIVERY_ESTIMATE_CACHE_TTL_SECONDS: int = 600
    DELIVERY_ESTIMATE_NEGATIVE_CACHE_TTL_SECONDS: int = 120
    DELIVERY_ESTIMATE_PROVIDER: str = "google_routes"
    # Por quanto tempo a estimativa guardada pode ser reaproveitada na
    # criacao do pedido, evitando refazer geocode + rota (as duas chamadas
    # pagas do Google). 15 minutos cobre o tempo de checkout com folga e
    # nao chega a valer para a proxima faixa de horario da filial, cujo
    # tempo de preparo e outro.
    DELIVERY_ESTIMATE_REUSE_TTL_SECONDS: int = 900
    REDIS_URL: str | None = None

    # Teto do corpo da requisicao. O maior payload legitimo e a criacao de
    # pedido, que com os limites de order_schema fica bem abaixo disso.
    MAX_REQUEST_BODY_BYTES: int = 262_144
    # Teto separado para as rotas de upload de imagem: uma foto de produto
    # nao cabe em 256 KB, e subir o limite geral para 3 MB abriria a mesma
    # folga em toda rota de JSON. Ver BodySizeLimitMiddleware.
    MAX_IMAGE_UPLOAD_BYTES: int = 3_145_728

    # Janela em que reenviar a mesma Idempotency-Key devolve a resposta
    # gravada em vez de criar de novo. Passado o TTL a chave e reciclavel.
    IDEMPOTENCY_TTL_HOURS: int = 24
    # False: requisicao sem o header passa sem protecao (so warning no log).
    # Ligue quando nao houver mais cliente antigo em campo — ver
    # normalize_idempotency_key.
    IDEMPOTENCY_REQUIRED: bool = False

    # Gateway de pagamento.
    #
    # "sandbox" e o provider interno: cria a cobranca localmente e aceita
    # webhook assinado com PAYMENT_WEBHOOK_SECRET, sem chamada externa. E o
    # que permite exercitar o fluxo inteiro sem depender do Mercado Pago
    # responder. "mercadopago" chama a API de verdade (ver
    # src/integrations/payment_gateway.py) usando a credencial cadastrada
    # do restaurante do pedido.
    PAYMENT_PROVIDER: str = "sandbox"
    # Segredo do HMAC do webhook do sandbox. Sem ele o webhook e recusado
    # com 503 — nao existe modo "aceita sem verificar".
    PAYMENT_WEBHOOK_SECRET: str | None = None
    # Nao existe MERCADOPAGO_WEBHOOK_SECRET global: cada restaurante tem
    # sua PROPRIA conta no Mercado Pago (nao e um app de marketplace com
    # OAuth compartilhado) e configura a assinatura de notificacao dela
    # mesma no proprio painel. O segredo mora em
    # restaurant_payment_credentials.webhook_secret_encrypted, por
    # restaurante e por ambiente, do mesmo jeito que o access_token — ver
    # PaymentCredentialService.get_active_credential e
    # scripts/register_restaurant_payment_credential.py.

    # Qual conjunto de credencial usar: a de teste (para desenvolver sem
    # mover dinheiro de verdade) ou a de producao. Global e nao por
    # restaurante — todo mundo migra junto no dia do go-live. Trocar aqui
    # nao muda nenhuma linha de codigo, so exige que a credencial de
    # "production" ja esteja cadastrada (ver
    # scripts/register_restaurant_payment_credential.py).
    MERCADOPAGO_ENVIRONMENT: str = "test"
    # Chave Fernet usada para cifrar/decifrar o access_token de cada
    # restaurante em restaurant_payment_credentials. E o unico lugar em que
    # essa chave existe fora do processo que a gerou — perde-la torna toda
    # credencial cadastrada ilegivel, entao troque-a com um plano de
    # recadastro em maos, nunca de surpresa.
    PAYMENT_CREDENTIALS_ENCRYPTION_KEY: str | None = None

    # --- Classificacao RFV da listagem de clientes do painel ---
    #
    # A cadencia NAO e um numero por restaurante: e o intervalo medio do
    # PROPRIO cliente, medido da linha que a consulta ja devolve
    # ((last - first) / (orders_count - 1)). Uma faixa fixa calibrada pela
    # media da casa erraria os dois extremos do mesmo cardapio — o cliente de
    # almoco de terca e o de aniversario — e erraria para o lado caro, porque
    # "em risco" existe para disparar reativacao. Ver
    # `src/services/customer_segment.py`.
    #
    # Estes seis valores sao politica da plataforma, e por isso vivem aqui e
    # nao em `restaurant_settings`: o ritmo de cada loja ja esta resolvido
    # pela cadencia do cliente, e coluna por restaurante seria botao de tela
    # que ninguem pediu para girar. Se um dia for preciso, a coluna nasce
    # nullable e ESTE valor vira o padrao herdado (armadilha 35).
    RFV_AT_RISK_FACTOR: float = 2.0
    RFV_LOST_FACTOR: float = 4.0
    # Piso: abaixo de uma semana a media de dois pedidos e ruido. Sem ele,
    # dois pedidos no mesmo almoco (esqueceu a bebida) dao cadencia ~0 e o
    # cliente vira "perdido" no dia seguinte. O preco do piso e explicito:
    # quem almoca todo dia so entra em "em risco" depois de 14 dias.
    RFV_MIN_CADENCE_DAYS: int = 7
    # Teto: sem ele, dois pedidos separados por oito meses dao cadencia de
    # 240 dias e o cliente nunca sai de "fiel".
    RFV_MAX_CADENCE_DAYS: int = 60
    # Quem tem UM pedido nao tem intervalo medivel — nao ha o que dividir.
    RFV_FALLBACK_CADENCE_DAYS: int = 30
    # A partir do terceiro pedido existe padrao; dois podem ser coincidencia.
    RFV_ORDERS_FOR_LOYAL: int = 3
    # "Novo" conta do PRIMEIRO pedido, e nao do ultimo: o que envelhece e o
    # relacionamento. Sem esta janela, cliente com dois pedidos espacados em
    # dez meses que pediu semana passada sairia como "novo" — a tela mentindo
    # na primeira leitura. Ele cai em "ocasional".
    RFV_NEW_WINDOW_DAYS: int = 30

    RATE_LIMIT_ENABLED: bool = True
    # Cabecalho com o IP real do cliente. Atras do Traefik o socket peer e o
    # proxy; deixe vazio apenas se a API for exposta sem proxy na frente.
    RATE_LIMIT_CLIENT_IP_HEADER: str = "x-real-ip"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() in {"production", "prod"}

    @property
    def api_docs_enabled(self) -> bool:
        if self.ENABLE_API_DOCS is not None:
            return self.ENABLE_API_DOCS
        return not self.is_production

    @field_validator("GIT_SHA", mode="before")
    @classmethod
    def _sha_vazio_e_sha_ausente(cls, valor: object) -> object:
        """String vazia vira `nao-carimbado`. O default do campo NAO faz isso.

        O BUG QUE ISTO FECHA, visto em producao em 24/08/2026: o boot escreveu
        `git_sha=` — vazio — e o aviso de "imagem sem carimbo" NAO disparou.
        Os dois sintomas sao a mesma causa, e ela atravessa tres arquivos:

            docker-compose.yml   args: GIT_SHA: "${GIT_SHA:-}"
            Dockerfile           ARG GIT_SHA=nao-carimbado

        `${GIT_SHA:-}` com a variavel ausente nao omite o build arg: expande
        para STRING VAZIA e o compose manda `--build-arg GIT_SHA=`. Um valor
        explicito, ainda que vazio, **sobrepoe** o default do `ARG` — entao a
        imagem nasce com `ENV GIT_SHA=""`, a variavel EXISTE no ambiente, e o
        default deste campo (`GIT_SHA_NAO_CARIMBADO`) nunca chega a valer.
        Pydantic so aplica default de campo AUSENTE, e este estava presente.

        Depois disso, `main.py` compara `settings.GIT_SHA == "nao-carimbado"`,
        recebe `"" == "nao-carimbado"` e segue em frente calado. O aviso que
        existia justamente para gritar "voce nao sabe que codigo esta rodando"
        ficou mudo no unico deploy em que precisava falar.

        POR QUE AQUI E NAO NO COMPOSE. Consertar o `:-` faria o caminho do
        compose funcionar e deixaria todos os outros abertos — `GIT_SHA=`
        exportado a mao, outro orquestrador, um `docker run -e GIT_SHA=`. O
        campo e quem sabe o que "sem carimbo" significa, entao a normalizacao
        mora nele. O comentario errado do `docker-compose.yml` foi corrigido
        junto, porque era ele que ensinava o modelo mental errado.
        """
        if isinstance(valor, str) and not valor.strip():
            return GIT_SHA_NAO_CARIMBADO
        return valor

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        enable_decoding=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
