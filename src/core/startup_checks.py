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
    """
    warnings: list[str] = []

    workers = detect_worker_count(argv, env)
    if workers > 1:
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
            "REDIS_URL NAO resolve este caso, ao contrario do rate limit e do "
            "cache de entrega: o historico do chat nao tem caminho de Redis. "
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

    if settings.ADMIN_AUTH_SECRET.strip() == settings.CUSTOMER_AUTH_SECRET.strip():
        # A variavel ser obrigatoria (config.py) resolve o caso de estar
        # ausente; este erro cobre o outro jeito de chegar ao mesmo lugar,
        # que e copiar o valor do cliente para preencher o campo. Token
        # forjado de um publico passaria a valer no outro.
        errors.append(
            "ADMIN_AUTH_SECRET e CUSTOMER_AUTH_SECRET tem o MESMO valor: os "
            "tokens de lojista e de cliente ficam assinados com a mesma "
            "chave, e comprometer um segredo compromete os dois publicos. "
            "Gere um valor proprio com "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
        )

    if settings.MERCADOPAGO_ENVIRONMENT not in ("test", "production"):
        errors.append(
            f"MERCADOPAGO_ENVIRONMENT='{settings.MERCADOPAGO_ENVIRONMENT}' invalido: "
            "so 'test' ou 'production' selecionam qual credencial cadastrada "
            "em restaurant_payment_credentials e usada."
        )

    return errors


def collect_configuration_warnings(settings: Settings) -> list[str]:
    warnings: list[str] = []

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
