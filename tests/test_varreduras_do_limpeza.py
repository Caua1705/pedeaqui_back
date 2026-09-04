"""As duas varreduras do container `limpeza` — a quarta e a quinta da família.

`test_varreduras_de_dinheiro.py` fechou as do `estorno`,
`test_varredura_de_reenvio_de_whatsapp.py` fechou a do `whatsapp-reenvio`.
Faltavam estas duas, e o buraco era o mesmo: os testes que existem importam
`_expirar`, `_vencidos` e `conferir_despejo_do_redis` — **nunca o `main`, nunca
o `--dry-run`, nunca o código de saída**.

    while true; do
      if python scripts/cleanup_idempotency_keys.py && python scripts/expire_cashback.py; then
        sleep 86400
      else
        sleep 300
      fi
    done

O `&&` e o código de saída valem aqui pelo mesmo motivo das outras três, com
um agravante próprio da cadência: **este laço dorme 24h no caminho feliz e 5
min depois de uma falha.** Um `return 1` indevido não deixa o container
lento — ele transforma um trabalho DIÁRIO num laço de cinco minutos que
executa `DELETE` sobre sete tabelas, para sempre.

## O `--dry-run` daqui é o de maior aposta dos cinco

O `--dry-run` do estorno que paga custa uma tarifa de gateway. O do reenvio
que manda custa a reputação do número. **Este apaga dado pessoal em texto
puro, e o apagar É o mecanismo de exclusão** — `ai_feedback.user_message` e o
`comment` de `order_reviews` de pedido de convidado não pendem de `customers`
e não são alcançados por conta nenhuma (armadilha 38). Não há de onde
recuperar.

O que separa contar de apagar são dois blocos irmãos no mesmo `if`: sete
`select(count)` de um lado, sete `delete_*`/`clear_*` de repositório do outro.
Nada obrigava os dois a continuarem sendo blocos diferentes.

Colaborador é dublê escrito à mão (sessão, repositórios, service). Não há
model nem schema dublado aqui — o que estas varreduras manipulam são
CONTAGENS.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from scripts import cleanup_idempotency_keys, expire_cashback


COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


class SessaoFalsa:
    """O `with SessionLocal() as db:` das duas varreduras."""

    def __init__(self, contagem: int = 0):
        self._contagem = contagem
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def scalar(self, _stmt):
        """Todo `select(func.count())` do caminho de dry-run passa por aqui."""
        return self._contagem

    def commit(self):
        self.commits += 1


class RepositorioQueContaApagos:
    """Um dublê para os sete repositórios de expurgo, de uma vez.

    Cada um tem um nome de método diferente (`delete_expired`,
    `delete_codes_created_before`, `clear_comments_created_before`...), e o que
    este teste precisa saber é só uma coisa: **algum deles foi chamado?**
    """

    def __init__(self, registro: list, nome: str):
        self._registro = registro
        self._nome = nome

    def __call__(self, _db):
        return self

    def __getattr__(self, metodo: str):
        def apagar(*args, **kwargs):
            self._registro.append(f"{self._nome}.{metodo}")
            return 0

        return apagar


REPOSITORIOS_DE_EXPURGO = (
    "IdempotencyRepository",
    "DeliveryEstimateRepository",
    "CustomerRepository",
    "AIFeedbackRepository",
    "OrderReviewRepository",
    "AdminErrorReportRepository",
    "WhatsAppContactWindowRepository",
)


@pytest.fixture
def limpeza(monkeypatch):
    """Liga os dublês do `cleanup_idempotency_keys` e devolve o que foi apagado."""

    def ligar(contagem: int = 0):
        apagados: list[str] = []
        sessao = SessaoFalsa(contagem)
        for nome in REPOSITORIOS_DE_EXPURGO:
            monkeypatch.setattr(
                cleanup_idempotency_keys, nome, RepositorioQueContaApagos(apagados, nome)
            )
        monkeypatch.setattr(cleanup_idempotency_keys, "SessionLocal", lambda: sessao)
        # O diagnóstico do Redis abre conexão própria; ele tem teste dele.
        monkeypatch.setattr(
            cleanup_idempotency_keys, "conferir_despejo_do_redis", lambda *_a, **_k: 0
        )
        return {"apagados": apagados, "sessao": sessao}

    return ligar


class TestOExpurgoNaoAconteceEmDryRun:
    """A aposta mais alta dos cinco `--dry-run`: aqui não há de onde voltar."""

    def test_dry_run_nao_chama_repositorio_de_expurgo_nenhum(self, limpeza, monkeypatch):
        ligado = limpeza(contagem=7)
        monkeypatch.setattr("sys.argv", ["cleanup_idempotency_keys.py", "--dry-run"])

        assert cleanup_idempotency_keys.main() == 0
        assert ligado["apagados"] == []

    def test_dry_run_nao_commita(self, limpeza, monkeypatch):
        """Sem escrita não há o que commitar, e um `commit` no caminho de
        contagem é o sinal de que os dois blocos se encostaram."""
        ligado = limpeza(contagem=7)
        monkeypatch.setattr("sys.argv", ["cleanup_idempotency_keys.py", "--dry-run"])

        cleanup_idempotency_keys.main()

        assert ligado["sessao"].commits == 0

    def test_sem_dry_run_ele_apaga_os_SETE(self, limpeza, monkeypatch):
        """O par positivo, e ele conta até sete de propósito: um expurgo que
        saia da lista deixa de acontecer, e o sintoma é dado pessoal que fica —
        exatamente o que não tem sintoma."""
        ligado = limpeza()
        monkeypatch.setattr("sys.argv", ["cleanup_idempotency_keys.py"])

        cleanup_idempotency_keys.main()

        assert len(ligado["apagados"]) == len(REPOSITORIOS_DE_EXPURGO)
        assert {chamada.split(".")[0] for chamada in ligado["apagados"]} == set(
            REPOSITORIOS_DE_EXPURGO
        )

    def test_os_sete_expurgos_cabem_num_commit_so(self, limpeza, monkeypatch):
        """São sete tabelas e uma transação. Commitar entre elas deixaria a
        retenção pela metade quando a quarta falhasse — e a metade que ficou
        seria dado pessoal que já devia ter saído."""
        ligado = limpeza()
        monkeypatch.setattr("sys.argv", ["cleanup_idempotency_keys.py"])

        cleanup_idempotency_keys.main()

        assert ligado["sessao"].commits == 1


class TestOCashbackNaoVenceEmDryRun:
    @pytest.fixture
    def cashback(self, monkeypatch):
        def ligar(regras, vencidos=(), dry_run=False):
            expirados: list = []
            sessao = SessaoFalsa()

            class RegrasFalsas:
                def __init__(self, _db):
                    pass

                def list_enabled_restaurant_rules(self):
                    return regras

            class ServiceFalso:
                def __init__(self, _db):
                    pass

                def expire_balance(self, customer_id, restaurant_id, _momento):
                    expirados.append((customer_id, restaurant_id))
                    return Decimal("10.00")

            monkeypatch.setattr(expire_cashback, "SessionLocal", lambda: sessao)
            monkeypatch.setattr(expire_cashback, "CashbackRuleRepository", RegrasFalsas)
            monkeypatch.setattr(expire_cashback, "CashbackService", ServiceFalso)
            monkeypatch.setattr(
                expire_cashback, "_vencidos", lambda *_a, **_k: list(vencidos)
            )
            monkeypatch.setattr(
                "sys.argv",
                ["expire_cashback.py"] + (["--dry-run"] if dry_run else []),
            )
            return {"expirados": expirados, "sessao": sessao}

        return ligar

    def test_dry_run_nao_expira_saldo_nenhum(self, cashback):
        """A linha negativa de vencimento apaga dinheiro de cliente. O
        `--dry-run` é o que se roda para OLHAR antes."""
        regra = _regra()
        ligado = cashback([regra], vencidos=[("cliente-1", Decimal("10"), _momento())], dry_run=True)

        assert expire_cashback.main() == 0
        assert ligado["expirados"] == []

    def test_sem_dry_run_ele_expira(self, cashback):
        regra = _regra()
        ligado = cashback([regra], vencidos=[("cliente-1", Decimal("10"), _momento())])

        expire_cashback.main()

        assert ligado["expirados"] == [("cliente-1", regra.restaurant_id)]

    def test_sem_campanha_ligada_sai_cedo_e_nao_toca_em_nada(self, cashback):
        """A trava que segura tudo hoje é um DADO, não o código:
        `cashback_rules.enabled` nasce falso em todo restaurante (armadilha
        26). Com a lista vazia esta varredura é um no-op — e é assim que ela
        roda em produção hoje."""
        ligado = cashback([])

        assert expire_cashback.main() == 0
        assert ligado["expirados"] == []
        assert ligado["sessao"].commits == 0


class TestOCodigoDeSaidaEOEncadeamento:
    """As duas metades da mesma garantia, como no `estorno`."""

    def test_a_limpeza_sai_com_zero(self, limpeza, monkeypatch):
        limpeza()
        monkeypatch.setattr("sys.argv", ["cleanup_idempotency_keys.py"])

        assert cleanup_idempotency_keys.main() == 0

    def test_o_compose_continua_encadeando_as_duas_com_e_comercial(self):
        """Com o `&&`, um vermelho da limpeza impede o vencimento de cashback
        de rodar. E os dois só saem diferente de 0 quando o BANCO não
        respondeu, que é o que torna o `&&` seguro — ele significa "banco fora
        do ar, tenta os dois em 5 min", e nunca "a primeira falhou, então a
        segunda não roda"."""
        texto = COMPOSE.read_text(encoding="utf-8")

        assert (
            "python scripts/cleanup_idempotency_keys.py "
            "&& python scripts/expire_cashback.py" in texto
        )

    def test_o_laco_do_limpeza_dorme_um_dia_no_caminho_feliz(self):
        """O agravante desta cadência: com um `return 1` indevido, o container
        troca `sleep 86400` por `sleep 300` — um trabalho diário vira um laço
        de cinco minutos executando DELETE sobre sete tabelas."""
        texto = COMPOSE.read_text(encoding="utf-8")

        assert "sleep 86400" in texto


def _regra():
    class RegraFalsa:
        restaurant_id = "restaurante-1"

    return RegraFalsa()


def _momento():
    from datetime import datetime, timezone

    return datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
