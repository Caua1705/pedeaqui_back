"""A trava por falhas de codigo do entregador.

Ate esta frente, os seis digitos eram segurados so pelo rate limit por IP —
e o balde do rate limit e o IP, nao o entregador: quem tem o link troca de
rede e o limite recomeca. O contador aqui e do CADASTRO, e trocar de IP nao
o contorna.

O que estes testes protegem, e cada um e um jeito de a trava virar enfeite ou
virar armadilha:

1. **Ela fecha.** Cinco erros dentro da janela travam, e a partir dai NEM O
   CODIGO CERTO abre — uma trava que deixasse a tentativa certa passar seria
   exatamente a resposta que a forca bruta procura.
2. **Ela nao prende o motoboy legitimo.** Falha isolada nao acumula (a
   contagem recomeca fora da janela), o acerto zera tudo, e a trava vence
   sozinha.
3. **A gravacao acontece.** A falha e contada na DEPENDENCIA, e a requisicao
   termina em 401: sem commit proprio, `get_db` fecharia a sessao sem gravar
   e o contador nunca sairia de zero.
4. **O acerto nao escreve a toa.** O par viaja em toda requisicao; um UPDATE
   por requisicao para nao mudar nada seria o custo escondido da trava.
5. **A saida existe fora do app.** Regenerar o acesso pelo painel destrava na
   hora, e o painel ve a trava enquanto ela vale — o motoboy travado nao tem
   como pedir socorro pela tela dele.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.services.admin_courier_service import AdminCourierService
from src.services.courier_delivery_service import (
    ACCESS_LOCK_WINDOW,
    MAX_ACCESS_ATTEMPTS,
    CourierDeliveryService,
)
from src.utils.security import hash_courier_access_code, hash_courier_link_token
from tests import fabricas


LINK = "link-do-ze-com-256-bits-de-mentira"
CODIGO = "123456"
ERRADO = "000000"


class FakeDb:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class FakeCourierRepository:
    def __init__(self, couriers):
        self.couriers = list(couriers)

    def get_by_link_hash(self, link_hash):
        for courier in self.couriers:
            if courier.access_link_hash == link_hash and courier.deleted_at is None:
                return courier
        return None

    def get_by_id_and_restaurant(self, courier_id, restaurant_id):
        for courier in self.couriers:
            if (
                courier.id == courier_id
                and courier.restaurant_id == restaurant_id
                and courier.deleted_at is None
            ):
                return courier
        return None


class _Base(unittest.TestCase):
    def setUp(self):
        self.agora = datetime(2026, 9, 4, 19, 0, tzinfo=timezone.utc)
        self.restaurant_id = uuid.uuid4()
        self.ze = fabricas.entregador(
            restaurant_id=self.restaurant_id,
            name="Zé",
            access_link_hash=hash_courier_link_token(LINK),
            access_code_hash=hash_courier_access_code(CODIGO, LINK),
        )
        self.db = FakeDb()
        self.service = CourierDeliveryService(self.db)
        self.service.courier_repository = FakeCourierRepository([self.ze])
        self.service.clock = lambda: self.agora

    def _errar(self, vezes: int) -> HTTPException:
        """Erra o codigo `vezes` vezes e devolve a ULTIMA excecao."""
        ultima = None
        for _ in range(vezes):
            with self.assertRaises(HTTPException) as raised:
                self.service.authenticate(LINK, ERRADO)
            ultima = raised.exception
        return ultima

    def _adiantar(self, delta: timedelta) -> None:
        self.agora = self.agora + delta


class TestElaFecha(_Base):
    def test_quatro_erros_nao_travam(self):
        self._errar(MAX_ACCESS_ATTEMPTS - 1)

        self.assertEqual(self.ze.access_failed_attempts, MAX_ACCESS_ATTEMPTS - 1)
        self.assertIsNone(self.ze.access_blocked_until)
        self.assertIs(self.service.authenticate(LINK, CODIGO), self.ze)

    def test_ate_a_quarta_a_resposta_e_401(self):
        erro = self._errar(MAX_ACCESS_ATTEMPTS - 1)

        self.assertEqual(erro.status_code, 401)

    def test_a_quinta_falha_trava_e_ja_responde_429(self):
        erro = self._errar(MAX_ACCESS_ATTEMPTS)

        self.assertEqual(erro.status_code, 429)
        self.assertEqual(self.ze.access_blocked_until, self.agora + ACCESS_LOCK_WINDOW)

    def test_a_frase_do_429_diz_o_prazo_e_a_outra_saida(self):
        erro = self._errar(MAX_ACCESS_ATTEMPTS)

        self.assertIn("15 min", erro.detail)
        self.assertIn("acesso novo ao restaurante", erro.detail)

    def test_travado_o_codigo_CERTO_tambem_e_recusado(self):
        """O ponto inteiro da trava.

        Se a tentativa certa passasse durante o bloqueio, a trava nao
        atrapalharia a forca bruta em nada — ela so atrasaria as tentativas
        erradas, que sao as que o atacante ja sabe estarem erradas.
        """
        self._errar(MAX_ACCESS_ATTEMPTS)

        with self.assertRaises(HTTPException) as raised:
            self.service.authenticate(LINK, CODIGO)

        self.assertEqual(raised.exception.status_code, 429)

    def test_travado_a_tentativa_errada_nao_reinicia_o_prazo(self):
        """Insistir durante a trava nao a estende: o `_ensure_is_not_blocked`
        recusa ANTES de conferir o codigo, entao a falha nem e contada."""
        self._errar(MAX_ACCESS_ATTEMPTS)
        fim = self.ze.access_blocked_until
        contador = self.ze.access_failed_attempts

        self._adiantar(timedelta(minutes=5))
        self._errar(3)

        self.assertEqual(self.ze.access_blocked_until, fim)
        self.assertEqual(self.ze.access_failed_attempts, contador)


class TestElaNaoPrendeOMotoboy(_Base):
    def test_falha_fora_da_janela_recomeca_a_contagem(self):
        """Dois erros na segunda e tres na sexta NAO somam cinco.

        Sem a janela, a trava puniria digitacao de semanas diferentes — que e
        o "bloquear cedo demais" que deixa o entregador de fora no meio do
        turno.
        """
        self._errar(MAX_ACCESS_ATTEMPTS - 1)

        self._adiantar(ACCESS_LOCK_WINDOW + timedelta(minutes=1))
        erro = self._errar(1)

        self.assertEqual(erro.status_code, 401)
        self.assertEqual(self.ze.access_failed_attempts, 1)
        self.assertIsNone(self.ze.access_blocked_until)

    def test_a_trava_vence_sozinha(self):
        self._errar(MAX_ACCESS_ATTEMPTS)

        self._adiantar(ACCESS_LOCK_WINDOW)

        self.assertIs(self.service.authenticate(LINK, CODIGO), self.ze)

    def test_depois_de_vencer_a_trava_a_contagem_recomeca_do_zero(self):
        """Quem destravou e errou de novo tem cinco tentativas outra vez, e
        nao uma. Sem isso, a segunda trava viria no primeiro erro e a terceira
        tambem — escalada de fato, escrita sem querer."""
        self._errar(MAX_ACCESS_ATTEMPTS)

        self._adiantar(ACCESS_LOCK_WINDOW)
        erro = self._errar(1)

        self.assertEqual(erro.status_code, 401)
        self.assertEqual(self.ze.access_failed_attempts, 1)

    def test_o_acerto_zera_o_contador(self):
        self._errar(MAX_ACCESS_ATTEMPTS - 2)

        self.service.authenticate(LINK, CODIGO)

        self.assertEqual(self.ze.access_failed_attempts, 0)
        self.assertIsNone(self.ze.access_failed_at)
        self.assertIsNone(self.ze.access_blocked_until)

    def test_codigo_AUSENTE_nao_conta_contra_a_trava(self):
        """Nao e chute: e app mal montado, ou a requisicao que sai antes de a
        pessoa digitar. Contar isso travaria o motoboy por um bug do app."""
        for _ in range(MAX_ACCESS_ATTEMPTS + 3):
            with self.assertRaises(HTTPException) as raised:
                self.service.authenticate(LINK, None)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(self.ze.access_failed_attempts, 0)
        self.assertIsNone(self.ze.access_blocked_until)

    def test_link_desconhecido_nao_encosta_no_contador(self):
        """Quem nao tem o link para no 404, e por isso a trava nao e uma
        porta para travar o motoboy dos outros: e preciso ter o link."""
        with self.assertRaises(HTTPException) as raised:
            self.service.authenticate("outro-link", ERRADO)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.ze.access_failed_attempts, 0)


class TestAGravacao(_Base):
    def test_a_falha_e_gravada_apesar_do_401(self):
        """A contagem roda na DEPENDENCIA e a requisicao termina em erro. Sem
        commit proprio, `get_db` fecha a sessao sem gravar e o contador nunca
        sai de zero — a trava inteira vira enfeite."""
        self._errar(1)

        self.assertEqual(self.db.commits, 1)

    def test_o_acerto_sem_nada_a_zerar_nao_escreve(self):
        """O par viaja em TODA requisicao e o app recarrega a lista a cada
        parada: um UPDATE por requisicao, para nao mudar nada, seria o custo
        escondido desta frente."""
        for _ in range(5):
            self.service.authenticate(LINK, CODIGO)

        self.assertEqual(self.db.commits, 0)

    def test_o_acerto_COM_o_que_zerar_escreve_uma_vez_so(self):
        self._errar(1)
        commits_antes = self.db.commits

        self.service.authenticate(LINK, CODIGO)
        self.service.authenticate(LINK, CODIGO)

        self.assertEqual(self.db.commits, commits_antes + 1)


class TestASaidaPeloPainel(unittest.TestCase):
    """O motoboy travado nao tem como pedir socorro pelo app: a tela dele
    responde 429 e mais nada. Quem destrava e a loja, pelo telefone."""

    def setUp(self):
        self.agora = datetime(2026, 9, 4, 19, 0, tzinfo=timezone.utc)
        self.restaurant_id = uuid.uuid4()
        self.ze = fabricas.entregador(
            restaurant_id=self.restaurant_id,
            name="Zé",
            access_link_hash=hash_courier_link_token(LINK),
            access_code_hash=hash_courier_access_code(CODIGO, LINK),
            access_failed_attempts=MAX_ACCESS_ATTEMPTS,
            access_failed_at=datetime(2026, 9, 4, 18, 55, tzinfo=timezone.utc),
            access_blocked_until=datetime(2026, 9, 4, 19, 10, tzinfo=timezone.utc),
        )
        self.db = FakeDb()
        self.service = AdminCourierService(self.db)
        self.service.courier_repository = FakeCourierRepository([self.ze])
        self.service.clock = lambda: self.agora
        admin = fabricas.usuario_do_painel(restaurant_id=self.restaurant_id, role="owner")
        self.owner = AdminScope(
            admin_user=admin, restaurant_id=self.restaurant_id, branch_id=None
        )

    def test_regenerar_o_acesso_destrava_na_hora(self):
        self.service.generate_access(self.owner, self.ze.id)

        self.assertEqual(self.ze.access_failed_attempts, 0)
        self.assertIsNone(self.ze.access_failed_at)
        self.assertIsNone(self.ze.access_blocked_until)

    def test_o_painel_ve_a_trava_enquanto_ela_vale(self):
        resposta = self.service.get_courier(self.owner, self.ze.id)

        self.assertEqual(resposta.access_blocked_until, self.ze.access_blocked_until)

    def test_o_painel_nao_publica_trava_ja_vencida(self):
        """A coluna guarda o fim da ultima trava mesmo depois de ele passar.
        Publicar o instante cru faria o painel escrever "travado ate 19h10"
        as 20h — o dono ligaria para o motoboy que ja consegue entrar."""
        self.agora = datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)

        resposta = self.service.get_courier(self.owner, self.ze.id)

        self.assertIsNone(resposta.access_blocked_until)

    def test_entregador_sem_trava_sai_com_o_campo_nulo(self):
        self.ze.access_blocked_until = None

        resposta = self.service.get_courier(self.owner, self.ze.id)

        self.assertIsNone(resposta.access_blocked_until)


if __name__ == "__main__":
    unittest.main()
