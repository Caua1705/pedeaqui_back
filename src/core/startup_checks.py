"""Validacao de configuracao executada no boot da aplicacao.

Roda no lifespan, e nao no import, para que importar `main` (testes,
ferramentas, scripts) nao dependa de um ambiente completo, mas subir a API
com configuracao incompleta falhe alto e na hora.
"""

import logging
import os
import sys
from collections.abc import Mapping

from src.core.config import Settings
from src.utils.security import resolve_admin_auth_secret, resolve_customer_auth_secret


logger = logging.getLogger("uvicorn.error")


class StartupConfigurationError(RuntimeError):
    pass


def detect_worker_count(argv: list[str], env: Mapping[str, str]) -> int:
    """Quantos workers este processo foi mandado subir. 1 quando nao da para saber.

    As duas fontes sao as que uvicorn e gunicorn de fato leem:

    - `--workers N` / `--workers=N` (os dois) e `-w N` (gunicorn);
    - `WEB_CONCURRENCY`, que os dois honram quando a opcao nao vem na linha.

    A PRECEDENCIA copia a do uvicorn (`config.py`: `if workers is None and
    "WEB_CONCURRENCY" in os.environ`) — a opcao ganha do ambiente. Inverter
    faria o aviso mentir na maquina que tem a variavel exportada do jeito
    antigo e passou a mandar `--workers` na linha.

    Valor que nao e inteiro devolve 1, e nao levanta: isto e um aviso de boot,
    e derrubar a API por causa de um `--workers abc` que o proprio uvicorn ja
    vai recusar seria trocar um problema por outro maior.

    ISTO SO FUNCIONA PORQUE `sys.argv` SOBREVIVE AO WORKER. O uvicorn nao
    forka: `uvicorn/_subprocess.py` usa `multiprocessing.get_context("spawn")`
    em toda plataforma. O que salva e o `multiprocessing.spawn.prepare()`, que
    restaura `sys.argv` no filho a partir do pai — conferido. Se um dia o
    uvicorn parar de passar por `multiprocessing`, este guard vira uma funcao
    que devolve 1 para sempre e ninguem percebe; e a razao de
    `tests/test_startup_checks.py` cobrir o caso do argv de worker.
    """
    workers = _worker_count_from_argv(argv)
    if workers is not None:
        return workers
    return _positive_int(env.get("WEB_CONCURRENCY")) or 1


def _worker_count_from_argv(argv: list[str]) -> int | None:
    """O valor de `--workers`/`-w` na linha de comando, se houver."""
    for position, argument in enumerate(argv):
        if argument in ("--workers", "-w"):
            following = argv[position + 1] if position + 1 < len(argv) else None
            return _positive_int(following)
        if argument.startswith("--workers="):
            return _positive_int(argument.split("=", 1)[1])
    return None


def _positive_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def collect_runtime_warnings(argv: list[str], env: Mapping[str, str]) -> list[str]:
    """Avisos sobre COMO o processo subiu, e nao sobre o que esta no `.env`.

    Separado de `collect_configuration_warnings` de proposito: aquele recebe
    `Settings` e responde "a configuracao esta certa?"; este olha a linha de
    comando e responde "este jeito de subir combina com o que o codigo
    assume?". Juntar os dois obrigaria a passar argv e env para uma funcao
    que hoje so precisa de settings.

    O QUE ESTA FUNCAO NAO CONSEGUE VER, e que nao e conserto de redacao: argv
    e do PROCESSO, entao ela enxerga os workers deste container e mais nada.
    Duas replicas atras do Traefik, cada uma com `--workers 1`, sao dois
    processos servindo o mesmo `/chat` — e este guard fica calado, porque do
    lado de ca esta tudo certo mesmo.

    Quem cobre esse pedaco e o lock da migracao (`src/db/advisory_lock.py`):
    a replica que precisa ESPERAR para migrar acabou de provar que existe
    outra, e e ela quem grita. O guard daqui continua sendo o dos workers, e
    os dois avisam sobre o mesmo perigo por dois caminhos que nao se
    substituem.
    """
    warnings: list[str] = []

    workers = detect_worker_count(argv, env)
    # `REDIS_URL` PASSOU A RESOLVER ESTE CASO em 04/09/2026, e por isso a
    # condicao ganhou um segundo termo.
    #
    # Enquanto o historico do `/chat` era um `dict` de modulo, o aviso valia
    # sempre que houvesse mais de um worker — e o texto dele dizia, com todas
    # as letras, que "REDIS_URL NAO resolve este caso". Agora resolve:
    # `src/ai/services/chat_history.py` escolhe o backend de Redis quando a
    # variavel existe, e a conversa passa a ser compartilhada.
    #
    # Manter o aviso incondicional seria pior que nao te-lo: ele apareceria em
    # TODO boot de uma configuracao correta, e um aviso que sempre aparece e um
    # aviso que ninguem le — inclusive no dia em que ele estiver certo.
    if workers > 1 and not env.get("REDIS_URL"):
        # Sai UMA VEZ POR WORKER, porque o lifespan roda em cada um. E ruido
        # aceitavel: N linhas iguais no boot sao mais faceis de notar que
        # uma, e o numero em cada uma delas ja diz quantos processos ha.
        warnings.append(
            f"a API subiu com {workers} workers e o historico de conversa do "
            "/chat vive em MEMORIA DO PROCESSO. Cada worker guarda as proprias "
            "sessoes, entao a conversa se perde entre requisicoes: um turno so "
            f"encontra o historico se cair no mesmo processo do anterior (1 em "
            f"{workers}). O Rapi responde sem contexto, SEM erro e SEM log: a "
            "pessoa so ve o assistente esquecendo o que ela acabou de dizer. "
            "DEFINA REDIS_URL: desde 04/09/2026 o historico do /chat tem "
            "caminho de Redis (src/ai/services/chat_history.py) e passa a ser "
            "compartilhado entre os workers. "
            "Preco e cardapio nao sao afetados (saem do banco); o que se perde "
            "e referencia do tipo 'quanto custa esse?'."
        )

    return warnings


def collect_configuration_errors(settings: Settings) -> list[str]:
    errors: list[str] = []

    if settings.DELIVERY_ESTIMATE_PROVIDER == "google_routes" and not settings.GOOGLE_MAPS_ROUTES_API_KEY.strip():
        errors.append(
            "GOOGLE_MAPS_ROUTES_API_KEY esta vazia com "
            "DELIVERY_ESTIMATE_PROVIDER=google_routes. Sem ela o cliente de "
            "rotas levanta GoogleMapsUnavailableError e TODO pedido de entrega "
            "e recusado com 'route_unavailable', sem erro visivel. Defina a "
            "chave ou mude DELIVERY_ESTIMATE_PROVIDER."
        )

    if settings.PAYMENT_PROVIDER == "mercadopago" and not (settings.PAYMENT_CREDENTIALS_ENCRYPTION_KEY or "").strip():
        # A credencial em si (access_token e webhook_secret) e por
        # restaurante e vive no banco (ver restaurant_payment_credentials);
        # o que da para checar no boot, sem consultar o banco, e se existe
        # chave para decifra-la. Sem isso TODA cobranca online responde 503
        # e TODO webhook responde 503, restaurante cadastrado ou nao.
        errors.append(
            "PAYMENT_PROVIDER=mercadopago sem PAYMENT_CREDENTIALS_ENCRYPTION_KEY. "
            "Nenhuma credencial de restaurante (access_token ou segredo de "
            "webhook) pode ser decifrada, e toda cobranca online e todo "
            "webhook responderiam 503. Gere uma chave com "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "ou volte PAYMENT_PROVIDER para sandbox."
        )

    errors.extend(_erros_dos_segredos_de_assinatura(settings))

    if settings.MERCADOPAGO_ENVIRONMENT not in ("test", "production"):
        errors.append(
            f"MERCADOPAGO_ENVIRONMENT='{settings.MERCADOPAGO_ENVIRONMENT}' invalido: "
            "so 'test' ou 'production' selecionam qual credencial cadastrada "
            "em restaurant_payment_credentials e usada."
        )

    return errors


def _erros_dos_segredos_de_assinatura(settings: Settings) -> list[str]:
    """Nenhum dos dois publicos pode assinar com chave vazia, nem com a do outro.

    Sao TRES portas para o mesmo lugar — token forjado de um publico valendo
    no outro — e ate 05/09/2026 so uma delas era vigiada:

    1. **valor igual nos dois campos.** Copiar o segredo do cliente para
       preencher o do lojista. Era a unica coberta.
    2. **segredo vazio.** `CUSTOMER_AUTH_SECRET=` passa pela pydantic (o tipo
       e `str`, e vazia e uma string), a API sobe, e todo token de cliente
       nasce assinado com "" — que qualquer pessoa consegue reproduzir. O
       sintoma nao aparece: os tokens funcionam.
    3. **a resolucao divergindo da leitura crua.** A comparacao lia
       `settings.CUSTOMER_AUTH_SECRET`, e quem assinava era
       `_customer_auth_secret()`, que caia no antigo `CUSTOMER_JWT_SECRET`
       quando o primeiro ficava vazio. Com esse fallback valendo o mesmo que
       `ADMIN_AUTH_SECRET`, os dois publicos compartilhavam chave e esta
       funcao comparava "" com o segredo de lojista, achando tudo certo.

    A 3 esta fechada porque a comparacao passou a chamar os resolvedores de
    `src.utils.security` — as mesmas funcoes que assinam. Fallback novo entra
    la e chega aqui sozinho.

    A ORDEM importa: os vazios saem primeiro e a funcao RETORNA. Dois
    segredos vazios sao iguais, e reportar "os dois tem o mesmo valor" mandaria
    quem esta lendo o boot gerar um par de valores diferentes — quando o que
    falta e valor nenhum.
    """
    vazios = [
        nome
        for nome in ("CUSTOMER_AUTH_SECRET", "ADMIN_AUTH_SECRET")
        if not (getattr(settings, nome) or "").strip()
    ]
    if vazios:
        return [
            f"{nome} esta vazia: a pydantic aceita o campo em branco (o tipo e "
            "`str`), a API sobe, e todo token daquele publico passa a ser "
            'assinado com "" — que qualquer pessoa reproduz. Nada falha, os '
            "tokens continuam validos, e nao ha sintoma. Gere um valor com "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
            for nome in vazios
        ]

    # Os RESOLVEDORES, e nao os campos: e o que faz esta trava enxergar o
    # segredo que de fato assina, e nao o que esta escrito no `.env`.
    if resolve_admin_auth_secret(settings).strip() == resolve_customer_auth_secret(settings).strip():
        return [
            "ADMIN_AUTH_SECRET e CUSTOMER_AUTH_SECRET tem o MESMO valor: os "
            "tokens de lojista e de cliente ficam assinados com a mesma "
            "chave, e comprometer um segredo compromete os dois publicos. "
            "Gere um valor proprio com "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
        ]

    return []


def collect_configuration_warnings(settings: Settings) -> list[str]:
    warnings: list[str] = []

    if not settings.google_oauth_client_ids:
        # AVISO, e nao erro de boot: entrar com Google e uma forma de login a
        # mais, e derrubar a API inteira de todo ambiente que ainda nao a
        # configurou seria trocar um botao que falta por um servico fora do
        # ar. Mesmo criterio do `PLATFORM_METRICS_KEY`.
        #
        # E ele existe porque o SINTOMA nao aponta para ca. Sem a variavel, as
        # rotas de Google respondem 503 e o resto da API fica perfeita: quem
        # for investigar comeca pelo app, pelo Google Cloud e pelo client id
        # do front — tres lugares — antes de suspeitar do `.env` do servidor.
        warnings.append(
            "GOOGLE_OAUTH_CLIENT_IDS esta vazia: POST /auth/google, "
            "/auth/google/complete-signup e /customers/me/social/google "
            "respondem 503 e NINGUEM consegue entrar nem conectar pelo Google. "
            "O resto da API funciona normalmente, entao o sintoma nao aponta "
            "para ca. Defina os client ids do Google Cloud separados por "
            "virgula, um por plataforma (web, Android, iOS) — o `aud` do "
            "id_token e o client id daquela plataforma."
        )

    if settings.RATE_LIMIT_ENABLED and not settings.REDIS_URL:
        warnings.append(
            "REDIS_URL nao definida: o rate limiting usa contador em memoria, "
            "valido apenas para 1 worker/container. Com mais de um processo o "
            "limite efetivo vira N vezes o configurado."
        )

    if not settings.REDIS_URL:
        warnings.append(
            "REDIS_URL nao definida: o cache de estimativa de entrega fica so "
            "em memoria e e perdido a cada deploy, aumentando o custo de "
            "chamadas ao Google Maps."
        )

    if not settings.REDIS_URL:
        # Aviso PROPRIO, e nao uma frase acrescentada aos dois acima, pelo
        # mesmo criterio que separa aqueles dois: cada consequencia da mesma
        # variavel ausente tem um dono, um sintoma e um custo diferentes, e
        # quem le o boot precisa achar o seu sem ler os outros.
        #
        # ESTE E O QUE MAIS SOME EM SILENCIO. Rate limit e estimativa de
        # entrega degradam para memoria e continuam CORRETOS num worker so; o
        # cache de embedding degrada para um `dict` que morre a cada deploy e
        # nunca viu duas pessoas — e o ganho inteiro dele e justamente o
        # acerto entre clientes. Sem Redis ele nao erra resposta nenhuma:
        # so volta a cobrar ~400 ms e uma chamada paga de embedding por
        # pergunta, indefinidamente, sem nada no log do `/chat` alem de um
        # miss que parece cache frio.
        warnings.append(
            "REDIS_URL nao definida: o cache de embedding do Rapi (/chat e "
            "/voice) fica so no dict do processo — morre a cada deploy e nao "
            "e compartilhado entre workers nem entre clientes, entao a mesma "
            "pergunta de duas pessoas paga dois embeddings (~400 ms e uma "
            "chamada cobrada cada). No log do /chat isso aparece como "
            "embedding_cache_origem=sem_redis."
        )

    if not settings.SUPABASE_SERVICE_ROLE_KEY:
        # Warning e nao erro: sem a chave so o upload de imagem do painel
        # para de funcionar (responde 503). Todo o resto da API, inclusive
        # a LEITURA das imagens ja existentes, continua igual — o bucket e
        # publico para leitura.
        warnings.append(
            "SUPABASE_SERVICE_ROLE_KEY nao definida: o upload de imagem do "
            "painel (POST /admin/products/{id}/image) responde 503. O "
            "restante do cardapio e as imagens ja enviadas nao sao afetados."
        )

    if settings.INTERNAL_API_KEY:
        warnings.append(
            "INTERNAL_API_KEY ainda definida no ambiente: desde a Fase 1 "
            "nenhuma rota a utiliza. Pode ser removida do .env."
        )

    warnings.extend(_avisos_do_whatsapp(settings))

    if settings.PAYMENT_PROVIDER == "sandbox" and not (settings.PAYMENT_WEBHOOK_SECRET or "").strip():
        # Warning e nao erro: hoje ha restaurante sem nenhuma forma de
        # pagamento online, e derrubar o boot deles por causa de uma
        # variavel que nao usam seria pior. Quem tem pagamento online
        # descobre no primeiro webhook, que responde 503.
        warnings.append(
            "PAYMENT_WEBHOOK_SECRET nao definida: o webhook do sandbox "
            "responde 503 e nenhum pedido online sai de 'aguardando "
            "pagamento'. Obrigatoria se a filial oferece pagamento online."
        )

    # Nao ha warning de boot equivalente para o segredo de webhook do
    # Mercado Pago: ele e por restaurante e vive no banco (ver
    # restaurant_payment_credentials.webhook_secret_encrypted), entao nao
    # da para saber no boot se "algum" restaurante esta sem cadastrar —
    # mesma razao pela qual nao ha warning de boot para access_token
    # ausente. Quem nao tiver cadastrado descobre no primeiro webhook, que
    # responde 503.

    if settings.is_production and settings.PAYMENT_PROVIDER == "sandbox":
        warnings.append(
            "PAYMENT_PROVIDER=sandbox em producao: as cobrancas sao criadas "
            "localmente e nenhum dinheiro e movimentado de verdade."
        )

    header = settings.RATE_LIMIT_CLIENT_IP_HEADER.strip()
    if not header:
        warnings.append(
            "RATE_LIMIT_CLIENT_IP_HEADER vazio: atras de um proxy o rate "
            "limiting vai agrupar todos os clientes no IP do proxy."
        )
    elif settings.is_production:
        # Nao da para conferir no boot se o proxy preenche o cabecalho — isso
        # so aparece na primeira requisicao. O aviso existe para que a
        # dependencia esteja escrita em algum lugar que alguem le no deploy:
        # com o cabecalho ausente, `client_ip` manda todo mundo para o balde
        # compartilhado e o login publico passa a estourar 429 no primeiro
        # minuto de movimento. Confira ANTES de subir (docs/operacao.md).
        warnings.append(
            f"RATE_LIMIT_CLIENT_IP_HEADER={header}: o proxy na frente PRECISA "
            "sobrescrever esse cabecalho em toda requisicao. Se ele nao vier, "
            "todos os clientes caem num balde unico e as rotas publicas "
            "comecam a responder 429."
        )

    return warnings


def _whatsapp_esta_em_uso(settings: Settings) -> bool:
    """Este ambiente usa WhatsApp?

    Ele NAO tem como olhar `whatsapp_channels` — `collect_configuration_warnings`
    recebe as configuracoes e nao o banco, e e o que a mantem chamavel no boot
    e num teste sem Postgres. O proxy sao as duas variaveis que so alguem
    montando a frente define:

    - `WHATSAPP_NOTIFICATIONS_ENABLED` ligada e a intencao declarada de mandar;
    - `WHATSAPP_TOKEN_ENCRYPTION_KEY` preenchida quer dizer que um canal foi
      (ou vai ser) cadastrado — sem ela o cadastro nem roda, entao ninguem a
      define por acaso.

    Nenhuma das duas, nenhum aviso: o ambiente simplesmente nao usa WhatsApp,
    e tres linhas de boot sobre uma frente desligada sao o tipo de ruido que
    se aprende a ignorar junto com o resto.
    """
    if settings.WHATSAPP_NOTIFICATIONS_ENABLED:
        return True
    return bool((settings.WHATSAPP_TOKEN_ENCRYPTION_KEY or "").strip())


def _avisos_do_whatsapp(settings: Settings) -> list[str]:
    """As tres variaveis do WhatsApp, cada uma com o SEU sintoma.

    Elas nao derrubam o boot de proposito — a frente e opcional e nenhum
    pedido depende dela —, e ate 05/09/2026 tambem nao avisavam nada. O
    resultado era o pior desenho possivel: **com canal cadastrado e chave
    faltando, o sintoma so aparecia no primeiro envio REAL**, como uma linha
    `failed` em `whatsapp_messages` que alguem precisa estar lendo. Nenhuma
    tela, nenhum log de boot, nenhum 500.

    Tres avisos e nao um, pelo mesmo criterio dos tres do `REDIS_URL`: cada
    uma tem um dono, um sintoma e um momento diferentes de morder, e juntar
    as tres numa frase faria quem procura a sua ler as outras duas.
    """
    if not _whatsapp_esta_em_uso(settings):
        return []

    avisos = []

    if not (settings.WHATSAPP_TOKEN_ENCRYPTION_KEY or "").strip():
        # A primeira porque e a mais total: sem ela o token do lojista nao
        # decifra, e NENHUM aviso sai — nem o cadastro de canal novo roda.
        avisos.append(
            "WHATSAPP_TOKEN_ENCRYPTION_KEY nao definida com os avisos "
            "LIGADOS: o access_token de cada canal nao pode ser decifrado, "
            "entao nenhum aviso de pedido sai e o cadastro de canal novo "
            "falha. Gere com "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`.'
        )

    if not (settings.WHATSAPP_APP_SECRET or "").strip():
        avisos.append(
            "WHATSAPP_APP_SECRET nao definida: POST /webhooks/whatsapp "
            "responde 503 para TODO restaurante. Os avisos ate saem, mas "
            "'entregue' e 'lido' nunca voltam para whatsapp_messages e a "
            "janela de 24h nunca abre. Nao existe modo 'aceita sem "
            "verificar'."
        )

    if not (settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN or "").strip():
        avisos.append(
            "WHATSAPP_WEBHOOK_VERIFY_TOKEN nao definida: o GET de "
            "verificacao responde 503 e o painel da Meta so diz que nao "
            "validou, sem dizer por que. Ela precisa estar no ambiente "
            "ANTES de clicar em 'Verificar e salvar' la."
        )

    return avisos

def validate_settings(settings: Settings) -> None:
    # A unica linha impura das checagens: ler o processo. O que decide fica
    # em `detect_worker_count`/`collect_runtime_warnings`, que recebem argv e
    # env de fora e por isso se testam sem mexer em global nenhum.
    for warning in collect_runtime_warnings(sys.argv, os.environ):
        logger.warning("[Startup] %s", warning)

    for warning in collect_configuration_warnings(settings):
        logger.warning("[Startup] %s", warning)

    errors = collect_configuration_errors(settings)
    if errors:
        joined = "\n".join(f"  - {error}" for error in errors)
        raise StartupConfigurationError(
            f"Configuracao invalida, a API nao vai subir:\n{joined}"
        )

    logger.info("[Startup] configuracao validada com sucesso")
