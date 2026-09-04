"""A terceira varredura de container — a que fala com a Meta.

`tests/test_varreduras_de_dinheiro.py` fechou duas das três varreduras que
rodam em laço num container: as do `estorno`. Esta é a terceira, no
`whatsapp-reenvio`, e ela estava sem teste nenhum — o script não era importado
por arquivo nenhum do repositório.

Ela repete o contrato de código de saída das outras duas (o próprio docstring
dela diz isso com todas as letras), e o laço aqui é o mais apertado dos três:

    while true; do
      if python scripts/reenvia_avisos_de_whatsapp.py; then sleep 120
      else echo "..."; sleep 30
      fi
    done

**30 segundos.** Sair diferente de 0 porque um aviso não saiu põe a varredura
girando meio minuto a meio minuto em cima da mesma fila — e o cenário que ela
existe para atender é justamente *a Meta fora do ar por dez minutos*. Ela não
pode ser também o que transforma isso em tempestade de requisição.

E ela tem duas coisas que as de dinheiro não têm:

- **a chave que cala tudo.** `WHATSAPP_NOTIFICATIONS_ENABLED` desligado tem que
  calar o reenvio, não só o aviso ao vivo. Duas chaves fariam "desliguei o
  WhatsApp" continuar mandando mensagem pela porta dos fundos — e quem recebe é
  o cliente, num canal cuja qualidade cai por denúncia;
- **o `except` largo de um aviso só.** O inesperado no aviso nº 3 não pode
  custar os outros 47 da fila. Com o laço de 30s, a alternativa é a mesma
  exceção derrubando a mesma passada para sempre.

Colaborador é dublê solto (sessão, repositório, notifier); **a mensagem é o
model de verdade**, `WhatsAppMessage(...)` transiente — CLAUDE.md.
"""

import uuid

import pytest

from scripts import reenvia_avisos_de_whatsapp
from src.models.whatsapp_model import WhatsAppMessage
from src.services.whatsapp_notification_service import (
    REENVIO_DESISTIU,
    REENVIO_ENVIADO,
)


class SessaoFalsa:
    def __init__(self):
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def rollback(self):
        self.rollbacks += 1


class RepositorioFalso:
    def __init__(self, mensagens, registro):
        self._mensagens = mensagens
        self._registro = registro

    def __call__(self, _db):
        return self

    def list_due_for_retry(self, now, limit):
        self._registro.append((now, limit))
        return self._mensagens


def aviso(**sobrescritas):
    """`WhatsAppMessage` transiente — sem sessão e sem banco.

    Não é `SimpleNamespace`: um objeto de atributos livres responderia
    `mensagem.kind` porque o teste escreveu, e continuaria respondendo no dia
    em que a coluna mudasse de nome.
    """
    campos = {
        "id": uuid.uuid4(),
        "order_id": uuid.uuid4(),
        "channel_id": uuid.uuid4(),
        "kind": "pedido_aceito",
        "status": "failed",
        "attempts": 2,
        "error_code": "131047",
    }
    campos.update(sobrescritas)
    return WhatsAppMessage(**campos)


@pytest.fixture
def montar(monkeypatch):
    """Liga os dublês e devolve a lista de avisos que o notifier recebeu."""

    def ligar(mensagens, desfecho=REENVIO_ENVIADO, explode=False, ligado=True):
        registro = []
        enviados = []
        sessao = SessaoFalsa()

        class NotifierFalso:
            def __init__(self, db):
                self.db = db

            def retry(self, mensagem):
                enviados.append(mensagem)
                if explode:
                    raise RuntimeError("a Meta devolveu algo que ninguem previu")
                return desfecho

        monkeypatch.setattr(
            reenvia_avisos_de_whatsapp.settings,
            "WHATSAPP_NOTIFICATIONS_ENABLED",
            ligado,
        )
        monkeypatch.setattr(
            reenvia_avisos_de_whatsapp, "SessionLocal", lambda: sessao
        )
        monkeypatch.setattr(
            reenvia_avisos_de_whatsapp,
            "WhatsAppMessageRepository",
            RepositorioFalso(mensagens, registro),
        )
        monkeypatch.setattr(
            reenvia_avisos_de_whatsapp, "WhatsAppOrderNotifier", NotifierFalso
        )
        return {"registro": registro, "enviados": enviados, "sessao": sessao}

    return ligar


class TestAChaveQueCalaTudo:
    """A mesma chave que cala o aviso ao vivo tem que calar o reenvio."""

    def test_desligado_nao_manda_nada(self, montar, monkeypatch):
        ligado = montar([aviso()], ligado=False)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        assert reenvia_avisos_de_whatsapp.main() == 0
        assert ligado["enviados"] == []

    def test_desligado_nem_abre_o_banco(self, montar, monkeypatch):
        """A guarda vem ANTES do `with SessionLocal()`. Não é otimização: com
        o WhatsApp desligado, este container não tem nada a fazer no banco, e
        um `SELECT` a cada 2 min seria trabalho por nada, para sempre."""
        ligado = montar([aviso()], ligado=False)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        reenvia_avisos_de_whatsapp.main()

        assert ligado["registro"] == []

    def test_ligado_manda(self, montar, monkeypatch):
        """O par positivo: sem ele, os dois acima passariam com um script que
        não manda nada em situação nenhuma."""
        mensagem = aviso()
        ligado = montar([mensagem], ligado=True)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        reenvia_avisos_de_whatsapp.main()

        assert ligado["enviados"] == [mensagem]


class TestOCodigoDeSaida:
    @pytest.mark.parametrize("desfecho", [REENVIO_ENVIADO, REENVIO_DESISTIU])
    def test_sai_com_zero_em_qualquer_desfecho(self, montar, monkeypatch, desfecho):
        montar([aviso()], desfecho=desfecho)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        assert reenvia_avisos_de_whatsapp.main() == 0

    def test_erro_inesperado_num_aviso_ainda_sai_com_zero(self, montar, monkeypatch):
        """Com o laço de 30s, sair diferente de 0 aqui é a mesma exceção
        derrubando a mesma passada duas vezes por minuto, sem fim."""
        montar([aviso()], explode=True)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        assert reenvia_avisos_de_whatsapp.main() == 0

    def test_fila_vazia_sai_com_zero(self, montar, monkeypatch):
        montar([])
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        assert reenvia_avisos_de_whatsapp.main() == 0


class TestUmAvisoNaoCustaOsOutros:
    def test_a_excecao_de_um_nao_para_a_fila(self, montar, monkeypatch):
        """O `except` largo do `_retry_one`. Sem ele, o aviso nº 1 que
        estourasse levaria os outros 49 da passada junto — e a linha deles
        continuaria com `next_attempt_at` no passado, então o sintoma seria
        uma fila que não anda."""
        tres = [aviso(), aviso(), aviso()]
        ligado = montar(tres, explode=True)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        reenvia_avisos_de_whatsapp.main()

        assert ligado["enviados"] == tres

    def test_o_inesperado_desfaz_a_transacao_daquele_aviso(self, montar, monkeypatch):
        """Sem o rollback, a sessão segue suja para os 49 seguintes — e o
        próximo `commit` gravaria o que a exceção deixou pela metade."""
        ligado = montar([aviso()], explode=True)
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        reenvia_avisos_de_whatsapp.main()

        assert ligado["sessao"].rollbacks == 1


class TestODryRunNaoManda:
    def test_dry_run_nao_chama_o_notifier(self, montar, monkeypatch):
        """Aqui o dry-run que envia não gasta dinheiro: manda mensagem para o
        celular de um cliente. Num canal cuja qualidade cai por denúncia, é
        pior que a fatura."""
        ligado = montar([aviso()])
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py", "--dry-run"])

        assert reenvia_avisos_de_whatsapp.main() == 0
        assert ligado["enviados"] == []


class TestOQueChegaAoRepositorio:
    def test_o_teto_do_lote_chega_na_consulta(self, montar, monkeypatch):
        """Cada linha da fila custa uma requisição à Meta. Sem o teto, uma
        passada segura a transação aberta durante dezenas de chamadas HTTP."""
        ligado = montar([])
        monkeypatch.setattr("sys.argv", ["reenvia_avisos_de_whatsapp.py"])

        reenvia_avisos_de_whatsapp.main()

        (_now, limit) = ligado["registro"][0]
        assert limit == reenvia_avisos_de_whatsapp.BATCH_LIMIT
