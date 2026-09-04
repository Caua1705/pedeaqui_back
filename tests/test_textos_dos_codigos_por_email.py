"""Cada propósito de código tem a SUA frase, e o e-mail nomeia a ação.

Um código de seis dígitos serve a quatro pedidos diferentes neste sistema —
entrar, ligar a conta do Google, redefinir a senha, excluir a conta — e o
e-mail é o único lugar onde a pessoa vê **o que ela está confirmando**. A tela
que pede o código é a que ela já está olhando, e quem chegou nela já está
convencido; se o e-mail diz só "seu código de verificação", não sobra nada que
possa contradizê-la.

O caso concreto: quem pediu um código para ENTRAR e cai numa tela pedindo
confirmação clica em confirmar por hábito. Foi assim até 04/09/2026 com a
confirmação do "entrar com Google" — mesma frase do cadastro, para um código
que faz outra coisa.

**O que se dubla aqui é o TRANSPORTE, e não o `EmailService`.** O objeto sob
teste é o de verdade; o que é substituído é o `_send_email`, que é onde a
biblioteca externa começa. Dublar o `EmailService` inteiro testaria o dublê
(armadilha 42).
"""

import unittest
from unittest.mock import patch

from src.services.email_service import EmailService


CODIGO = "123456"
DESTINO = "cliente@exemplo.com"


# Por método: a palavra que TEM que aparecer no assunto, e a que TEM que
# aparecer no corpo. São o verbo da ação, e não uma palavra qualquer do texto:
# é o verbo que a pessoa lê antes de digitar.
#
# Acrescentar um método `send_*_code` sem pôr a linha dele aqui deixa
# `test_todo_proposito_de_codigo_tem_a_sua_frase` vermelho — que é o ponto
# deste arquivo, e mais importante que os textos de hoje.
PROPOSITOS = {
    "send_email_verification_code": ("entrar", "ENTRAR"),
    "send_google_link_code": ("ligar sua conta do Google", "LIGAR"),
    "send_password_reset_code": ("redefinir sua senha", "senha"),
    "send_account_deletion_code": ("excluir sua conta", "EXCLUIR"),
}


def _enviar(nome_do_metodo: str) -> dict:
    """Chama o método de verdade e devolve o que ele mandaria para o Resend."""
    servico = EmailService()
    with patch.object(EmailService, "_send_email") as transporte:
        getattr(servico, nome_do_metodo)(DESTINO, CODIGO)

    transporte.assert_called_once()
    return transporte.call_args.kwargs


class TodoPropositoTemASuaFrase(unittest.TestCase):
    def test_todo_proposito_de_codigo_tem_a_sua_frase(self):
        """Método `send_*_code` novo sem linha em `PROPOSITOS` é vermelho.

        É a metade que sobrevive à próxima rodada. Os textos de hoje podem ser
        reescritos; o que não pode voltar a acontecer é um quinto propósito
        nascer reaproveitando a frase de outro — que é exatamente como o do
        Google nasceu.
        """
        na_classe = {
            nome
            for nome in dir(EmailService)
            if nome.startswith("send_") and nome.endswith("_code")
        }

        self.assertEqual(
            na_classe,
            set(PROPOSITOS),
            "Há método de código sem frase declarada (ou o contrário). Todo "
            "propósito novo precisa do SEU texto: o e-mail é o único lugar "
            "onde a pessoa vê o que está confirmando.",
        )

    def test_o_assunto_e_o_corpo_nomeiam_a_acao(self):
        for metodo, (no_assunto, no_corpo) in PROPOSITOS.items():
            with self.subTest(metodo=metodo):
                enviado = _enviar(metodo)

                self.assertIn(no_assunto.lower(), enviado["subject"].lower())
                self.assertIn(no_corpo, enviado["body"])

    def test_o_codigo_e_o_destino_vao_no_e_mail(self):
        """A trava boba que impede um texto novo de esquecer o código.

        Sem ela, um `body` reescrito sem o `{code}` passaria nos dois testes
        acima — nomeando a ação lindamente, e sem o número.
        """
        for metodo in PROPOSITOS:
            with self.subTest(metodo=metodo):
                enviado = _enviar(metodo)

                self.assertIn(CODIGO, enviado["body"])
                self.assertEqual(enviado["to_email"], DESTINO)

    def test_os_quatro_assuntos_sao_DIFERENTES_entre_si(self):
        """Dois propósitos com o mesmo assunto é o defeito original, de volta.

        A caixa de entrada mostra o assunto antes de qualquer coisa, e é por
        ele que a pessoa decide qual e-mail abrir. Dois iguais fazem os dois
        pedidos parecerem o mesmo pedido — mesmo com corpos diferentes.
        """
        assuntos = [_enviar(metodo)["subject"] for metodo in PROPOSITOS]

        self.assertEqual(len(set(assuntos)), len(PROPOSITOS), assuntos)

    def test_nenhum_texto_promete_verificacao_generica(self):
        """"Código de verificação" é a frase que não diz nada, e era a de dois.

        Ela descreve o mecanismo (conferir seis dígitos) em vez do efeito, e
        serve igualmente bem para entrar e para apagar a conta — que é
        precisamente o problema.
        """
        for metodo in PROPOSITOS:
            with self.subTest(metodo=metodo):
                enviado = _enviar(metodo)
                texto = f"{enviado['subject']} {enviado['body']}".lower()

                self.assertNotIn("código de verificação", texto)


if __name__ == "__main__":
    unittest.main()
