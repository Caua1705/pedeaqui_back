"""Uma tela com o estado da producao. Rode isto ANTES de qualquer outra coisa.

    docker exec pedeaqui-api python scripts/estado_da_producao.py

Sem argumento nenhum, de proposito. Ele responde as quatro perguntas que
precedem toda investigacao neste repositorio, e a ordem delas nao e
alfabetica — e a ordem em que errar custa mais caro:

    1. QUE CODIGO ESTA NO AR?      git_sha da imagem, e o alvo do alembic
    2. EM QUE REVISAO ESTA O BANCO? o que foi aplicado contra o que existe
    3. O REDIS RESPONDE?            e despejou chave?
    4. O MERCADO PAGO ESTA PRONTO?  credencial e segredo de webhook, por loja

A primeira e a primeira porque em 24/08/2026 tres commits ficaram sem push e
a producao seguiu rodando o codigo anterior; descobrir isso custou uma
bateria de medicao. "Estou olhando o codigo que eu acho que estou olhando?" e
a pergunta que precede todas as outras.

SO LEITURA, pela mesma regra de `check_restaurant.py`: nenhum INSERT, UPDATE
ou DDL. Este script nao conserta nada e nao deve passar a consertar — as
correcoes tem donos diferentes (uma e deploy, uma e migracao, duas sao
script no servidor) e escolher por conta propria seria escrever em producao
sem ninguem pedir.

**Ele NAO e `check_restaurant.py`, e os dois nao se fundem.** Aquele responde
"este restaurante esta pronto para o primeiro pedido?" e recebe um slug; este
responde "a plataforma esta de pe e coerente?" e nao recebe nada. Juntar os
dois faria um script que precisa de argumento para responder uma pergunta que
nao tem argumento.

E ele e LOCAL ao processo que o roda. Rodado no host, `git_sha` e o do `.env`
do host, nao o do container — por isso a linha de uso ali em cima comeca com
`docker exec`.

Sai com 1 quando ha ERRO, para servir de porta num script de deploy.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.config import GIT_SHA_NAO_CARIMBADO, settings
from src.db.session import SessionLocal
from src.services.payment_credential_service import PaymentCredentialService
from src.utils.crypto import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
)


OK = "OK"
ATENCAO = "ATENCAO"
ERRO = "ERRO"

# Sem acento e sem travessao em NADA que va para o console: no Windows do
# balcao ele abre na codepage do sistema (850/1252 no Brasil) e um caractere
# de fora dela sai como "?" ou derruba o proprio print (armadilha 29). Este
# script roda no container Linux quase sempre, mas "quase sempre" nao e uma
# regra que da para lembrar as duas da manha.


@dataclass
class Conferencia:
    titulo: str
    situacao: str
    linhas: list[str] = field(default_factory=list)
    o_que_fazer: str = ""


# ---------------------------------------------------------------------------
# 1. Que codigo esta no ar
# ---------------------------------------------------------------------------


def conferir_a_imagem() -> Conferencia:
    """O git_sha carimbado na imagem, e o alvo do alembic.

    Os dois na mesma conferencia porque sao a mesma pergunta em dois tempos:
    "que codigo" e "contra que schema ele foi mandado rodar".

    `ALEMBIC_TARGET` diferente de `head` e estado TEMPORARIO (a migracao em
    duas etapas), e o jeito de ele virar permanente e alguem esquecer a
    variavel no `.env`. Dali em diante toda revisao nova deixa de ser
    aplicada e o deploy passa verde — ver `docker-entrypoint.sh`.
    """
    linhas = [f"  app_env={settings.APP_ENV}"]
    alvo = os.environ.get("ALEMBIC_TARGET", "").strip()

    if settings.GIT_SHA == GIT_SHA_NAO_CARIMBADO:
        linhas.append("  git_sha=nao-carimbado - nao da para saber que codigo e este")
        return Conferencia(
            "Imagem no ar", ERRO, linhas,
            "Deploy que carimba: GIT_SHA=$(git rev-parse --short HEAD) "
            "docker compose up -d --build",
        )

    linhas.insert(0, f"  git_sha={settings.GIT_SHA}")
    if alvo and alvo != "head":
        linhas.append(f"  ALEMBIC_TARGET={alvo} - o banco NAO vai para head neste deploy")
        return Conferencia(
            "Imagem no ar", ATENCAO, linhas,
            "Se a janela de conferencia da migracao em duas etapas terminou, "
            "TIRE ALEMBIC_TARGET do .env. Ver docs/deploy-hash-do-tracking-token.md.",
        )

    return Conferencia("Imagem no ar", OK, linhas)


# ---------------------------------------------------------------------------
# 2. Em que revisao esta o banco
# ---------------------------------------------------------------------------


def _config_do_alembic() -> Config:
    """O `alembic.ini`, com `script_location` em caminho ABSOLUTO.

    O do arquivo e `alembic`, relativo — e o Alembic o resolve contra o
    diretorio de TRABALHO, nao contra o do proprio `.ini` (conferido). No
    container isso funciona por acidente, porque `WORKDIR` e `/app`; rodado
    de qualquer outro lugar, `ScriptDirectory` morre com "Path doesn't
    exist: alembic", que nao diz nada sobre cwd.

    Isto NAO torna o script inteiro independente de cwd — `settings` le
    `.env` pelo caminho relativo, entao rodar de fora da raiz derruba o
    import antes de chegar aqui, e isso vale para todo script do repositorio.
    O que a linha faz e nao ACRESCENTAR uma segunda dependencia de cwd, cujo
    erro ("Path doesn't exist: alembic") nao diria nada sobre cwd.
    """
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT_DIR / "alembic"))
    return config


def conferir_a_revisao_do_banco(db: Session) -> Conferencia:
    """O que `alembic_version` diz contra o que existe em `alembic/versions`.

    A comparacao e com o head DESTA imagem, e e por isso que ela vale: as
    duas metades saem do mesmo lugar, entao "banco atras do codigo" aqui
    significa que a migracao nao rodou — nao que o repositorio andou.

    Banco a FRENTE do codigo tambem e achado, e o mais perigoso dos dois: e
    o rollback de imagem sem `alembic downgrade`, que na revisao do cardapio
    por filial faz `/menu` responder 200 com o cardapio das duas lojas
    misturado (armadilha 36).
    """
    script = ScriptDirectory.from_config(_config_do_alembic())
    heads = script.get_heads()
    aplicadas = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()

    linhas = [
        f"  codigo em: {', '.join(sorted(heads)) or '(nenhuma revisao)'}",
        f"  banco em:  {', '.join(sorted(aplicadas)) or '(vazio)'}",
    ]

    if not aplicadas:
        return Conferencia(
            "Revisao do banco", ERRO, linhas,
            "Banco que ja tem o schema mas nunca passou pelo Alembic precisa de "
            "`alembic stamp 20260726_0001` UMA vez, antes do primeiro up.",
        )

    if set(aplicadas) == set(heads):
        return Conferencia("Revisao do banco", OK, linhas)

    conhecidas = {revisao.revision for revisao in script.walk_revisions()}
    desconhecidas = [revisao for revisao in aplicadas if revisao not in conhecidas]
    if desconhecidas:
        linhas.append(
            f"  o banco esta em {', '.join(desconhecidas)}, que NAO existe nesta imagem"
        )
        return Conferencia(
            "Revisao do banco", ERRO, linhas,
            "O banco esta A FRENTE do codigo: alguem voltou a imagem sem "
            "`alembic downgrade`. O codigo antigo consulta o schema novo e "
            "responde 200 com dado errado.",
        )

    linhas.append("  o banco esta ATRAS: ha revisao nesta imagem que nao foi aplicada")
    return Conferencia(
        "Revisao do banco", ERRO, linhas,
        "Veja a saida do alembic no boot: `docker logs pedeaqui-api | head -40`. "
        "O entrypoint migra antes de servir, entao API de pe com banco atras "
        "significa que ela subiu de uma imagem anterior.",
    )


# ---------------------------------------------------------------------------
# 3. O Redis
# ---------------------------------------------------------------------------


def conferir_o_redis(cliente=None) -> Conferencia:
    """Responde? E despejou chave?

    `cliente` injetavel pelo mesmo motivo de
    `cleanup_idempotency_keys.conferir_despejo_do_redis`: e o unico jeito de
    um teste exercitar "despejou 40 chaves" sem um Redis de verdade sob
    pressao de memoria. O dublê aqui e um COLABORADOR (biblioteca de
    terceiro), nao um schema nosso — a regra de nunca dublar schema/model do
    CLAUDE.md nao alcanca este caso.

    `evicted_keys` e o numero que importa, e o esperado dele e ZERO, para
    sempre. Contador de rate limit despejado nao da erro: o `slowapi` le a
    chave ausente como zero, o cliente ganha orcamento novo, e o limite deixa
    de valer em silencio (armadilha 41).

    Aqui e uma FOTO, e a varredura diaria de `cleanup_idempotency_keys.py`
    continua sendo quem vigia — este script existe para a pergunta caber na
    mesma tela das outras tres, nao para substituir aquela.
    """
    if cliente is None and not settings.REDIS_URL:
        return Conferencia(
            "Redis", ATENCAO,
            ["  REDIS_URL nao definida"],
            "Sem Redis: rate limit e cache de entrega em memoria do processo, e o "
            "cache de embedding do Rapi morre a cada deploy. Ver o servico `redis` "
            "do docker-compose.yml.",
        )

    if cliente is None:
        from src.ai.services.chat_cache import cliente_redis

        cliente = cliente_redis()

    if cliente is None:
        return Conferencia(
            "Redis", ERRO, ["  REDIS_URL definida, e o cliente nao subiu"],
            "Confira o valor de REDIS_URL no .env (a senha entra na URL).",
        )

    try:
        cliente.ping()
        info = cliente.info()
    except Exception as erro:  # noqa: BLE001
        return Conferencia(
            "Redis", ERRO, [f"  nao respondeu: {type(erro).__name__}"],
            "docker compose ps redis; docker logs pedeaqui-redis",
        )

    despejadas = _numero(info, "evicted_keys")
    usada_mb = _numero(info, "used_memory") / (1024 * 1024)
    teto_mb = _numero(info, "maxmemory") / (1024 * 1024)
    linhas = [f"  responde | used_memory={usada_mb:.1f} MB de {teto_mb:.0f} MB"]

    if despejadas == 0:
        linhas.append("  evicted_keys=0")
        return Conferencia("Redis", OK, linhas)

    linhas.append(f"  evicted_keys={despejadas} - o esperado e ZERO, para sempre")
    return Conferencia(
        "Redis", ERRO, linhas,
        "Chave despejada pode ser contador de rate limit, e ai o limite deixou "
        "de valer sem nenhum erro. Ver a armadilha 41 da skill: o conserto e "
        "outra INSTANCIA de Redis, nao outro banco logico.",
    )


def _numero(info: dict, chave: str) -> int:
    valor = info.get(chave, info.get(chave.encode(), 0))
    try:
        return int(valor)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 4. As credenciais do Mercado Pago
# ---------------------------------------------------------------------------


def conferir_o_mercado_pago(db: Session) -> Conferencia:
    """Credencial e segredo de webhook de quem OFERECE pagamento online.

    A conferencia DECIFRA a credencial, e nao so olha se a coluna esta
    preenchida. E a unica forma de saber que a
    `PAYMENT_CREDENTIALS_ENCRYPTION_KEY` deste `.env` e a mesma que cifrou o
    que esta no banco — uma chave trocada deixa a linha inteira ilegivel, e o
    sintoma seria 503 em toda cobranca, muito depois.

    O valor decifrado nunca sai daqui: o que vai para a tela e "decifrou" ou
    "nao decifrou".

    Quem NAO oferece pagamento online nao aparece: sem cobranca no gateway
    nao ha webhook para chegar, e listar essas lojas encheria a tela de
    linhas que nao pedem nada.
    """
    ambiente = settings.MERCADOPAGO_ENVIRONMENT
    titulo = f"Mercado Pago (ambiente {ambiente})"

    if settings.PAYMENT_PROVIDER != "mercadopago":
        return Conferencia(
            titulo, ATENCAO if settings.is_production else OK,
            [f"  PAYMENT_PROVIDER={settings.PAYMENT_PROVIDER}: nenhuma cobranca "
             "de verdade e criada"],
            "Em producao isto significa que o dinheiro nao esta se movendo."
            if settings.is_production else "",
        )

    lojas = _restaurantes_com_pagamento_online(db)
    if not lojas:
        return Conferencia(titulo, OK, ["  nenhum restaurante oferece pagamento online"])

    servico = PaymentCredentialService(db)
    linhas, situacao = [], OK
    for loja in lojas:
        situacao = _pior(situacao, _conferir_uma_loja(servico, loja, linhas))

    return Conferencia(
        titulo, situacao, linhas,
        "python scripts/register_restaurant_payment_credential.py "
        f"--restaurant-slug <slug> --environment {ambiente}",
    )


def _conferir_uma_loja(servico: PaymentCredentialService, loja: dict, linhas: list[str]) -> str:
    """A situacao de UM restaurante, escrevendo o diagnostico dele em `linhas`."""
    nome = loja["slug"]
    try:
        credencial = servico.get_active_credential(loja["id"])
    except CredentialEncryptionNotConfiguredError:
        linhas.append(f"  {nome}: PAYMENT_CREDENTIALS_ENCRYPTION_KEY ausente ou invalida")
        return ERRO
    except CredentialDecryptionError:
        linhas.append(f"  {nome}: a credencial NAO DECIFRA com a chave deste .env")
        return ERRO

    if credencial is None:
        linhas.append(f"  {nome}: sem credencial cadastrada - toda cobranca responde 503")
        return ERRO

    if credencial.webhook_secret is None:
        linhas.append(
            f"  {nome}: credencial ok, SEM segredo de webhook - o cliente paga e o "
            "pedido nunca sai de 'aguardando'"
        )
        return ERRO

    linhas.append(f"  {nome}: credencial e segredo de webhook decifram")
    return OK


def _restaurantes_com_pagamento_online(db: Session) -> list[dict]:
    """Os que tem ao menos uma filial ativa oferecendo pagamento pelo gateway.

    A condicao e a mesma de `check_restaurant.py`, e ela repete a regra em
    SQL de proposito: e o preco de a conferencia rodar sem subir o app. Se
    ela mudar la, muda aqui.
    """
    linhas = db.execute(text(
        """
        SELECT DISTINCT r.id, r.slug
          FROM restaurants r
          JOIN branches b ON b.restaurant_id = r.id
          JOIN branch_payment_methods bpm ON bpm.branch_id = b.id
         WHERE r.is_active IS TRUE
           AND b.is_active IS TRUE
           AND bpm.enabled IS TRUE
           AND (bpm.payment_flow = 'online' OR bpm.requires_gateway IS TRUE)
         ORDER BY r.slug
        """
    )).mappings().all()
    return [dict(linha) for linha in linhas]


# ---------------------------------------------------------------------------
# Saida
# ---------------------------------------------------------------------------


def _pior(atual: str, novo: str) -> str:
    """A situacao acumulada. So piora — uma loja em ordem nao apaga o erro da outra."""
    ordem = (OK, ATENCAO, ERRO)
    return atual if ordem.index(atual) >= ordem.index(novo) else novo


def imprimir(conferencias: list[Conferencia]) -> None:
    print()
    print("=" * 76)
    print(f"Estado da producao  ({settings.APP_NAME})")
    print("=" * 76)

    for conferencia in conferencias:
        print()
        print(f"[{conferencia.situacao:<7}] {conferencia.titulo}")
        for linha in conferencia.linhas:
            print(linha)
        if conferencia.situacao != OK and conferencia.o_que_fazer:
            print(f"  -> {conferencia.o_que_fazer}")

    print()
    print("-" * 76)
    print(f"  {_veredito(conferencias)}")
    print("-" * 76)
    print()


def _veredito(conferencias: list[Conferencia]) -> str:
    erros = [c.titulo for c in conferencias if c.situacao == ERRO]
    if erros:
        return f"HA PROBLEMA EM: {', '.join(erros)}."

    atencoes = [c.titulo for c in conferencias if c.situacao == ATENCAO]
    if atencoes:
        return f"Nenhum erro. Confira: {', '.join(atencoes)}."

    return "Tudo no lugar: imagem carimbada, banco em head, Redis limpo, gateway pronto."


def main() -> int:
    """O banco e conferido primeiro porque quase tudo depende dele.

    Nao dar para conectar nao vira traceback: vira uma conferencia com ERRO,
    e as outras rodam do mesmo jeito. Quem esta com a operacao parada precisa
    da tela inteira de uma vez — descobrir o Redis fora do ar num segundo
    comando, depois de consertar o banco, e a pior ordem possivel.
    """
    conferencias = [conferir_a_imagem()]

    db = None
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
    except Exception as erro:  # noqa: BLE001
        if db is not None:
            db.close()
            db = None
        conferencias.append(Conferencia(
            "Revisao do banco", ERRO,
            [f"  nao consegui falar com o Postgres: {type(erro).__name__}"],
            "Confira DATABASE_URL no .env (precisa do driver: postgresql+psycopg://...).",
        ))

    try:
        if db is not None:
            conferencias.append(conferir_a_revisao_do_banco(db))
        conferencias.append(conferir_o_redis())
        if db is not None:
            conferencias.append(conferir_o_mercado_pago(db))
    finally:
        if db is not None:
            db.close()

    imprimir(conferencias)
    return 1 if any(c.situacao == ERRO for c in conferencias) else 0


if __name__ == "__main__":
    raise SystemExit(main())
