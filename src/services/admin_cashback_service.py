"""Ler e gravar a configuracao de cashback pelo painel.

## A heranca e por LINHA, e o contrato precisa deixar isso visivel

Em `branches` (revisao 20260818_0025) `NULL` numa coluna significa "herda o
valor do restaurante", e a resolucao e campo a campo. **No cashback nao.** A
filial tem a regra inteira (`cashback_rules.branch_id` preenchido) ou nao tem
nenhuma, e nesse caso vale a linha de `branch_id IS NULL`. O motivo esta no
model: o percentual por dia mora numa tabela filha, e "coluna nula" nao
existe numa tabela filha.

E por isso que a leitura devolve `AdminCashbackRuleView` — com `source` — e
nao a linha crua. Sem `source`, a filial que herda e a que tem regra propria
respondem exatamente a mesma coisa, e o painel nao tem como avisar que salvar
ali **cria uma sobrescrita** em vez de editar a regra da rede.

## Nao existe DELETE da regra do restaurante, e a falta e deliberada

Apagar a linha da rede parece "desligar o cashback de tudo", e nao e: a
filial que tem sobrescrita propria com `enabled = true` **continua
creditando**, porque `resolve_cashback_terms` nem olha a linha do restaurante
quando a da filial existe. O lojista teria desligado a campanha na tela e
continuaria pagando cashback numa loja.

Desligar e `enabled: false` no `PUT`, que vale para toda filial que herda — e
a loja que quiser sair sozinha ganha sobrescrita propria desligada, que e o
recurso descrito no model.

## `PUT` e nao `PATCH`

Um `PATCH` sobre uma filial que ainda nao tem regra propria teria que
responder "patch sobre o que?": sobre os valores herdados, criando uma
sobrescrita inteira a partir de um campo so. O `PUT` obriga quem escreve a
mandar a regra completa, e ai o que ele cria e o que ele viu.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.cashback_rule_model import CashbackRule, CashbackRuleWeekday
from src.repositories.cashback_rule_repository import CashbackRuleRepository
from src.schemas.admin_cashback_schema import (
    AdminCashbackRuleResponse,
    AdminCashbackRuleView,
    AdminCashbackRuleWrite,
    CashbackWeekdayResponse,
)
from src.services.branch_scope import BranchScope


class AdminCashbackService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = CashbackRuleRepository(db)
        self.branch_scope = BranchScope(db)

    def get_restaurant_rule(self, scope: AdminScope) -> AdminCashbackRuleView:
        """A regra padrao da rede, a que toda filial sem sobrescrita herda."""
        rule = self.repository.get_rule(scope.restaurant_id, None)
        if rule is None:
            return AdminCashbackRuleView(source="none")
        return AdminCashbackRuleView(source="restaurant", rule=self._response(rule))

    def replace_restaurant_rule(
        self,
        scope: AdminScope,
        payload: AdminCashbackRuleWrite,
    ) -> AdminCashbackRuleResponse:
        rule = self._upsert(scope.restaurant_id, None, payload)
        return self._response(rule)

    def get_branch_rule(self, scope: AdminScope, branch_id: uuid.UUID) -> AdminCashbackRuleView:
        """A regra que VALE nesta filial, dizendo de onde ela veio.

        A mesma resolucao de `resolve_cashback_terms`, e de proposito: uma
        segunda implementacao da heranca faria a tela do painel discordar do
        que o checkout aplica, sem erro em lugar nenhum.
        """
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        da_filial, do_restaurante = self.repository.get_rules_for_branch(
            scope.restaurant_id, branch_id
        )
        if da_filial is not None:
            return AdminCashbackRuleView(source="branch", rule=self._response(da_filial))
        if do_restaurante is not None:
            return AdminCashbackRuleView(source="restaurant", rule=self._response(do_restaurante))
        return AdminCashbackRuleView(source="none")

    def replace_branch_rule(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: AdminCashbackRuleWrite,
    ) -> AdminCashbackRuleResponse:
        """Cria ou substitui a sobrescrita desta filial.

        A partir daqui a filial **para de herdar**: mudar a regra da rede nao
        a alcanca mais, ate alguem apagar a sobrescrita.
        """
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        rule = self._upsert(scope.restaurant_id, branch_id, payload)
        return self._response(rule)

    def delete_branch_rule(self, scope: AdminScope, branch_id: uuid.UUID) -> None:
        """Apaga a sobrescrita. A filial volta a herdar a regra da rede.

        404 quando nao ha sobrescrita, e nao 204: os dois estados sao
        diferentes para quem esta na tela — "voltou a herdar agora" e "ja
        herdava" — e um 204 mudo faria o painel mostrar sucesso para um botao
        que nao deveria estar la.
        """
        self.branch_scope.get(scope=scope, branch_id=branch_id)
        rule = self.repository.get_rule(scope.restaurant_id, branch_id)
        if rule is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Esta filial não tem regra própria de cashback",
            )
        self._commit(lambda: self.repository.delete(rule))

    def _upsert(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        payload: AdminCashbackRuleWrite,
    ) -> CashbackRule:
        rule = self.repository.get_rule(restaurant_id, branch_id)
        if rule is None:
            rule = CashbackRule(restaurant_id=restaurant_id, branch_id=branch_id)
        self._commit(lambda: self._escrever(rule, payload))
        return rule

    def _escrever(self, rule: CashbackRule, payload: AdminCashbackRuleWrite) -> None:
        """Grava a regra inteira, dias da semana inclusive.

        A lista nova SUBSTITUI a antiga: o `delete-orphan` do relacionamento
        apaga as linhas que sairam do corpo. Dia que sai volta a valer
        `default_percent`, e nunca zero — e a inversao deliberada em relacao
        ao `PUT` de horarios (armadilha 3), explicada no model.
        """
        rule.enabled = payload.enabled
        rule.default_percent = payload.default_percent
        rule.min_redeem_balance = payload.min_redeem_balance
        rule.expiry_days = payload.expiry_days
        rule.weekdays = [
            CashbackRuleWeekday(weekday=dia.weekday, percent=dia.percent)
            for dia in payload.weekdays
        ]
        self.repository.add(rule)

    @staticmethod
    def _response(rule: CashbackRule) -> AdminCashbackRuleResponse:
        """A linha, com os dias em ordem.

        O `sorted` nao e cosmetico: `cashback_rule_weekdays` nao tem coluna
        de ordem, entao sem ele a mesma regra sai com os dias embaralhados a
        cada requisicao e a tela de configuracao troca de ordem sozinha. E a
        armadilha 14 no mesmo formato — la eram os adicionais da comanda.
        """
        return AdminCashbackRuleResponse(
            id=rule.id,
            restaurant_id=rule.restaurant_id,
            branch_id=rule.branch_id,
            enabled=rule.enabled,
            default_percent=rule.default_percent,
            min_redeem_balance=rule.min_redeem_balance,
            expiry_days=rule.expiry_days,
            weekdays=[
                CashbackWeekdayResponse(weekday=dia.weekday, percent=dia.percent)
                for dia in sorted(rule.weekdays, key=lambda dia: dia.weekday)
            ],
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    def _commit(self, before_commit=None) -> None:
        """Mesmo arranjo de `AdminSettingsService._commit`.

        O que precede o commit entra no mesmo `try` — aqui e o delete dos
        dias antigos junto do insert dos novos — para que uma falha no meio
        nao deixe a sessao suja.
        """
        try:
            if before_commit is not None:
                before_commit()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
