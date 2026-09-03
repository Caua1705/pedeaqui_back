"""O `nonce`: o que fecha injeção de `id_token` no fluxo do navegador.

O front usa o Google Identity Services no navegador, e não o fluxo nativo. Aí
o `nonce` deixa de ser defesa em profundidade e passa a ser **a** defesa: sem
ele, um `id_token` legítimo emitido para o nosso client id — capturado de um
log, de uma extensão, de qualquer lugar por onde ele passe — é aceito por este
backend como se fosse a pessoa entrando agora.

## Por que o servidor emite o nonce, e não o cliente

Um nonce escolhido pelo navegador não prova nada: o servidor não sabe o que
esperar, e "confere se bate com o que veio junto" é comparar um valor consigo
mesmo. Quem tem que escolher é quem confere.

## Por que ele NÃO mora no Redis nem numa tabela

`REDIS_URL` é opcional neste projeto, e sem ela o backend cai num `dict` de
processo — com dois workers, metade dos logins falharia a conferência
(armadilha 20). Controle de segurança que quebra quando se escala não é
controle. Uma tabela custaria migração, retenção e um passo de exclusão de
conta para guardar dez minutos de estado anônimo.

O que sobra, e é o mesmo desenho dos tickets do "entrar com Google": um token
assinado por nós, guardado pelo cliente. Ele carrega o **hash** do nonce — não
o nonce — para que um `nonce_token` vazado num log de proxy não entregue junto
o valor que permitiria forjar o par.

## Falha FECHADA: sem `nonce_token`, sem login

`id_token` sem a claim `nonce` também é recusado. A alternativa — "confere só
quando vem" — seria inútil: o Google só põe o `nonce` quando o cliente o passa,
então quem estivesse atacando simplesmente obteria um token sem ele.
"""

import unittest
from datetime import timedelta

from fastapi import HTTPException

from src.services import google_signin_tickets as tickets
from src.utils.security import create_signed_token, decode_signed_token


class TestOParEmitido(unittest.TestCase):
    def test_o_nonce_e_o_token_saem_juntos(self) -> None:
        nonce, nonce_token = tickets.create_nonce()

        self.assertTrue(nonce)
        self.assertTrue(nonce_token)
        self.assertNotIn(nonce, nonce_token)

    def test_cada_chamada_sorteia_um_nonce_novo(self) -> None:
        """Nonce repetido é nonce que não serve: o token capturado de uma
        sessão passaria a valer na seguinte."""
        primeiro, _ = tickets.create_nonce()
        segundo, _ = tickets.create_nonce()

        self.assertNotEqual(primeiro, segundo)

    def test_o_token_carrega_o_HASH_e_nao_o_nonce(self) -> None:
        """`nonce_token` vazado num log não pode entregar o valor que permite
        forjar o par."""
        nonce, nonce_token = tickets.create_nonce()

        payload = decode_signed_token(nonce_token, tickets.PURPOSE_NONCE)
        self.assertNotEqual(payload["sub"], nonce)
        self.assertEqual(payload["sub"], tickets.hash_nonce(nonce))

    def test_o_nonce_token_nao_vale_como_sessao(self) -> None:
        _, nonce_token = tickets.create_nonce()

        with self.assertRaises(Exception):
            decode_signed_token(nonce_token, "customer_access")


class TestAConferencia(unittest.TestCase):
    def test_o_par_certo_passa(self) -> None:
        nonce, nonce_token = tickets.create_nonce()

        tickets.ensure_nonce_matches(nonce_token, nonce)

    def test_o_nonce_de_outra_sessao_e_recusado(self) -> None:
        """O ataque inteiro em uma linha: o `id_token` da vítima chega com o
        nonce DELA, e o atacante só tem o `nonce_token` da sessão dele."""
        _, meu_token = tickets.create_nonce()
        nonce_da_vitima, _ = tickets.create_nonce()

        with self.assertRaises(HTTPException) as erro:
            tickets.ensure_nonce_matches(meu_token, nonce_da_vitima)

        self.assertEqual(erro.exception.status_code, 401)

    def test_id_token_sem_nonce_e_recusado(self) -> None:
        """Falha FECHADA. O Google só põe a claim quando o cliente a passa —
        "confere só quando vem" seria pedir para o atacante não passar."""
        _, nonce_token = tickets.create_nonce()

        with self.assertRaises(HTTPException) as erro:
            tickets.ensure_nonce_matches(nonce_token, None)

        self.assertEqual(erro.exception.status_code, 401)

    def test_nonce_token_vencido_e_recusado(self) -> None:
        nonce = "um-nonce-qualquer"
        vencido = create_signed_token(
            subject=tickets.hash_nonce(nonce),
            purpose=tickets.PURPOSE_NONCE,
            expires_delta=timedelta(minutes=-1),
        )

        with self.assertRaises(HTTPException) as erro:
            tickets.ensure_nonce_matches(vencido, nonce)

        self.assertEqual(erro.exception.status_code, 400)

    def test_ticket_de_outro_proposito_nao_serve_de_nonce_token(self) -> None:
        """`purpose` separa usos que compartilham a chave (armadilha 32)."""
        de_cadastro = create_signed_token(
            subject="qualquer",
            purpose=tickets.PURPOSE_SIGNUP,
            expires_delta=timedelta(minutes=5),
            extra={"email": "x@y.z", "name": "X"},
        )

        with self.assertRaises(HTTPException) as erro:
            tickets.ensure_nonce_matches(de_cadastro, "qualquer")

        self.assertEqual(erro.exception.status_code, 400)


class TestOHashDoNonce(unittest.TestCase):
    def test_e_estavel(self) -> None:
        self.assertEqual(tickets.hash_nonce("abc"), tickets.hash_nonce("abc"))

    def test_nonces_diferentes_dao_hashes_diferentes(self) -> None:
        self.assertNotEqual(tickets.hash_nonce("abc"), tickets.hash_nonce("abd"))


if __name__ == "__main__":
    unittest.main()
