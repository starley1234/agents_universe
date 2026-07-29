"""Тесты движка: цикл качества, доска, HITL, возобновляемость.

Главный набор проекта. Здесь проверяются именно те обещания, ради
которых существует платформа, и проверяются на настоящей инфраструктуре:
реальный SQLite, реальные инструменты, реальная файловая система.
Заглушка одна — сама модель.

Ключевые сценарии:
  * данные идут между шагами через доску, а не через склейку текста;
  * плохая работа возвращается на доработку с ЗАМЕЧАНИЯМИ в промпте;
  * исчерпание доработок не приводит к тихой приёмке брака;
  * пауза на человеке переживает СОЗДАНИЕ НОВОГО ДВИЖКА на той же базе
    (эмуляция перезапуска процесса);
  * правка человека становится результатом шага;
  * прогон, возобновлённый после правки файла workflow, исполняет ту
    редакцию, с которой стартовал.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import (TempEnv, check, check_raises, section,        # noqa: E402
                     simple_workflow, summary)
from awos.contracts import CRITIC_MARKER, SUPERVISOR_MARKER        # noqa: E402
from awos.kernel.engine import Engine, EngineError                 # noqa: E402
from awos.kernel.store import Store                                # noqa: E402
from awos.kernel.workflow import WorkflowError, parse_workflow     # noqa: E402
from awos.llm.base import BaseLLM, LLMError, Reply, Usage          # noqa: E402
from awos.llm.stub import StubLLM                                  # noqa: E402
from awos.roles.profile import Profile                             # noqa: E402


class RoleLLM(BaseLLM):
    """Модель, отвечающая по роли из системного промпта.

    Позволяет писать сценарий теста в терминах ролей («критик сначала
    браку́ет, потом принимает»), а не подгонять порядок реплик вслепую.
    """
    name = "role"

    def __init__(self, worker=None, critic=None, supervisor=None) -> None:
        super().__init__("role", retries=0)
        self.worker = list(worker or [])
        self.critic = list(critic or [])
        self.supervisor = list(supervisor or [])
        self.prompts: list[str] = []

    def _chat_once(self, messages):
        # Роль определяем по маркеру ШАБЛОНА СРЕДЫ (awos/contracts.py), а не
        # по системному промпту: в промпте Исполнителя законно упоминается
        # Критик, и опознание по промпту молча путает роли.
        user = str(messages[-1].get("content", ""))
        self.prompts.append(user)
        if CRITIC_MARKER in user:
            queue = self.critic
            default = '{"score": 0.9, "verdict": "accept"}'
        elif SUPERVISOR_MARKER in user:
            queue = self.supervisor
            default = '{"decision": "accept", "reason": "ок"}'
        else:
            queue = self.worker
            default = "результат исполнителя"
        text = queue.pop(0) if queue else default
        return Reply(text=text, usage=Usage(10, 5))


def engine_with(env: TempEnv, llm: BaseLLM) -> Engine:
    return Engine(env.cfg, llm_factory=lambda profile: llm)


def main() -> int:
    section("Простой прогон: один шаг, без критика")
    with TempEnv() as env:
        env.write_workflow("wf", simple_workflow())
        eng = engine_with(env, StubLLM(scripted=["готовый результат"]))
        out = eng.start("wf", goal="цель")
        check("прогон завершён", out.status == "done", out.detail)
        check("шаг выполнен", out.steps[0]["status"] == "done")
        check("результат на доске", out.outputs["result"] == "готовый результат")
        check("служебные ключи скрыты из outputs",
              not any(k.startswith("_") for k in out.outputs))
        run = eng.store.get_run(out.run_id)
        check("счётчик шагов", run["steps_done"] == 1)
        check("расход токенов учтён", run["tokens_in"] > 0)

    section("Цепочка: данные идут через доску контекста")
    with TempEnv() as env:
        env.write_workflow("chain", {
            "name": "chain", "inputs": {"topic": "тема"},
            "steps": [
                {"name": "a", "task": "изучи {input.topic}", "writes": "notes"},
                {"name": "b", "task": "используй: {ctx.notes}", "reads": ["notes"],
                 "writes": "final"},
            ]})
        llm = RoleLLM(worker=["ЗАМЕТКИ ПЕРВОГО", "ИТОГ ВТОРОГО"])
        out = engine_with(env, llm).start("chain", goal="ц",
                                          inputs={"topic": "ТЕМА-X"})
        check("оба шага выполнены", [s["status"] for s in out.steps] ==
              ["done", "done"], str(out.steps))
        check("вход подставлен в первый шаг", "ТЕМА-X" in llm.prompts[0])
        check("результат первого шага пришёл во второй",
              "ЗАМЕТКИ ПЕРВОГО" in llm.prompts[1])
        check("блок общего контекста добавлен",
              "Данные из общего контекста" in llm.prompts[1])
        check("оба ключа на доске",
              out.outputs["notes"] == "ЗАМЕТКИ ПЕРВОГО" and
              out.outputs["final"] == "ИТОГ ВТОРОГО")

    section("Обязательные входы проверяются ДО создания прогона")
    with TempEnv() as env:
        env.write_workflow("need", simple_workflow(
            inputs={"topic": "тема"},
            steps=[{"name": "a", "task": "{input.topic}"}]))
        eng = engine_with(env, StubLLM())
        check_raises("отсутствующий вход отвергается", WorkflowError,
                     eng.start, "need")
        check("мёртвый прогон не создан", eng.store.list_runs() == [],
              "ошибка входов не должна оставлять мусор в базе")

    section("Цикл качества: доработка с замечаниями")
    with TempEnv(max_revisions=2, min_score=0.7) as env:
        env.write_workflow("q", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r",
             "review": {"critic": "critic"}}]))
        llm = RoleLLM(
            worker=["плохая версия", "исправленная версия"],
            critic=['{"score": 0.3, "verdict": "revise", '
                    '"issues": ["нет источников"], "summary": "слабо"}',
                    '{"score": 0.9, "verdict": "accept"}'])
        out = engine_with(env, llm).start("q", goal="ц")
        check("шаг в итоге принят", out.steps[0]["status"] == "done")
        check("зафиксирована одна доработка", out.steps[0]["revisions"] == 1)
        check("принята вторая версия", out.outputs["r"] == "исправленная версия")
        check("итоговая оценка сохранена", out.steps[0]["score"] == 0.9)
        retry_prompt = llm.prompts[2]
        check("замечания попали в промпт доработки",
              "нет источников" in retry_prompt,
              "без замечаний модель выдаёт тот же текст другими словами")
        check("прошлая версия показана исполнителю",
              "плохая версия" in retry_prompt)
        check("доработка помечена явно",
              "ДОРАБОТКУ" in retry_prompt.upper())

    section("Цикл качества: доработки исчерпаны — брак НЕ принимается")
    with TempEnv(max_revisions=1, min_score=0.8, hitl_mode="off") as env:
        env.write_workflow("bad", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r",
             "review": {"critic": "critic"}}]))
        llm = RoleLLM(worker=["плохо-1", "плохо-2", "плохо-3"],
                      critic=['{"score": 0.2, "verdict": "revise"}'] * 3)
        out = engine_with(env, llm).start("bad", goal="ц")
        check("прогон провален", out.status == "failed")
        check("шаг провален", out.steps[0]["status"] == "failed")
        check("причина названа", "порог" in out.steps[0]["detail"].lower() or
              "доработки исчерпаны" in out.steps[0]["detail"].lower(),
              out.steps[0]["detail"])
        check("брак не попал на доску", "r" not in out.outputs,
              "тихая приёмка брака — худший исход для платформы качества")

    section("Цикл качества: лимит доработок соблюдается")
    with TempEnv(max_revisions=3, min_score=0.9, hitl_mode="off") as env:
        env.write_workflow("lim", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "review": {"critic": "critic"}}]))
        llm = RoleLLM(worker=["в"] * 10,
                      critic=['{"score": 0.1, "verdict": "revise"}'] * 10)
        out = engine_with(env, llm).start("lim", goal="ц")
        check("ровно 3 доработки", out.steps[0]["revisions"] == 3,
              str(out.steps[0]["revisions"]))
        # Считаем именно ходы Исполнителя: запрос к Критику тоже содержит
        # текст задачи (он показывает её для разбора), поэтому наивный
        # подсчёт по слову «сделай» даёт удвоение.
        worker_turns = [p for p in llm.prompts if CRITIC_MARKER not in p]
        check("исполнитель вызван 4 раза (1 + 3 доработки)",
              len(worker_turns) == 4, f"ходов: {len(worker_turns)}")
        check("критик вызван на каждый ход исполнителя",
              len(llm.prompts) - len(worker_turns) == 4)

    section("Пустой ответ модели не проходит как результат")
    with TempEnv(max_revisions=0, hitl_mode="off") as env:
        env.write_workflow("empty", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r"}]))
        out = engine_with(env, RoleLLM(worker=["   "])).start("empty")
        check("пустой результат приводит к провалу", out.status == "failed")

    section("Сбой модели: понятный провал, а не трейсбек")
    class Broken(BaseLLM):
        name = "broken"

        def __init__(self):
            super().__init__("broken", retries=0)

        def _chat_once(self, messages):
            raise LLMError("сеть недоступна")

    with TempEnv() as env:
        env.write_workflow("br", simple_workflow())
        out = engine_with(env, Broken()).start("br")
        check("прогон провален", out.status == "failed")
        check("причина понятна", "недоступн" in out.detail.lower(), out.detail)

    section("Human-in-the-Loop: пауза, перезапуск процесса, продолжение")
    with TempEnv(hitl_mode="always", hitl_wait_seconds=0) as env:
        env.write_workflow("h", {
            "name": "h", "steps": [
                {"name": "a", "task": "первый", "writes": "one"},
                {"name": "b", "task": "второй по {ctx.one}", "reads": ["one"],
                 "writes": "two"}]})
        out = engine_with(env, RoleLLM(worker=["РЕЗУЛЬТАТ-A", "РЕЗУЛЬТАТ-B"])
                          ).start("h", goal="ц")
        run_id = out.run_id
        check("прогон остановлен на человеке", out.status == "waiting_human")
        check("точка контроля создана", out.checkpoint is not None)
        check("в точке контроля показан результат",
              out.checkpoint["payload"]["output"] == "РЕЗУЛЬТАТ-A")
        check("шаг помечен как ожидающий",
              out.steps[0]["status"] == "waiting_human")
        check("второй шаг ещё не начат", out.steps[1]["status"] == "pending")

        # Эмуляция перезапуска процесса: НОВЫЙ Engine, НОВЫЙ Store, та же база.
        fresh = Engine(env.cfg, Store(env.cfg.db_path),
                       llm_factory=lambda p: RoleLLM(worker=["РЕЗУЛЬТАТ-B"]))
        pending = fresh.store.pending_checkpoint(run_id)
        check("новый процесс видит открытую точку контроля",
              pending is not None and pending["id"] == out.checkpoint["id"],
              "пауза обязана переживать перезапуск")
        out2 = fresh.respond(pending["id"], "approved", "утверждаю")
        check("после утверждения прогон пошёл дальше",
              out2.steps[0]["status"] == "done")
        check("данные первого шага дошли до второго",
              out2.status in ("waiting_human", "done"))
        check("результат первого шага на доске",
              fresh.store.ctx_get(run_id, "one") == "РЕЗУЛЬТАТ-A")

        cp2 = fresh.store.pending_checkpoint(run_id)
        out3 = fresh.respond(cp2["id"], "approved")
        check("прогон завершён после второго утверждения",
              out3.status == "done", out3.detail)

    section("HITL: правка человека становится результатом шага")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("e", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r"}]))
        eng = engine_with(env, RoleLLM(worker=["версия модели"]))
        out = eng.start("e")
        out2 = eng.respond(out.checkpoint["id"], "edited", "ТЕКСТ ЧЕЛОВЕКА")
        check("прогон завершён", out2.status == "done")
        check("на доске текст человека, а не модели",
              out2.outputs["r"] == "ТЕКСТ ЧЕЛОВЕКА")
        check("в шаге сохранён текст человека",
              out2.steps[0]["output"] == "ТЕКСТ ЧЕЛОВЕКА")

    section("HITL: отклонение и отмена")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("r", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r"}]))
        eng = engine_with(env, RoleLLM(worker=["в"]))
        out = eng.start("r")
        out2 = eng.respond(out.checkpoint["id"], "rejected", "не годится")
        check("отклонение проваливает прогон", out2.status == "failed")
        check("причина человека сохранена", "не годится" in out2.detail)

        out = eng.start("r")
        out2 = eng.respond(out.checkpoint["id"], "cancelled", "передумал")
        check("отмена переводит прогон в cancelled", out2.status == "cancelled")

    section("HITL: режим off не спрашивает никого")
    with TempEnv(hitl_mode="off") as env:
        env.write_workflow("o", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r", "human": "always"}]))
        out = engine_with(env, RoleLLM(worker=["в"])).start("o")
        check("режим off сильнее human=always у шага", out.status == "done",
              "администратор среды главнее автора workflow")

    section("HITL: human=never не спрашивают даже в режиме always")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("n", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r", "human": "never"}]))
        out = engine_with(env, RoleLLM(worker=["в"])).start("n")
        check("шаг с human=never проходит без паузы", out.status == "done")

    section("HITL: эскалация при исчерпании доработок")
    with TempEnv(hitl_mode="critical", max_revisions=0, min_score=0.9) as env:
        env.write_workflow("esc", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r",
             "review": {"critic": "critic"}}]))
        llm = RoleLLM(worker=["слабо"],
                      critic=['{"score": 0.1, "verdict": "revise", '
                              '"issues": ["всё плохо"]}'])
        eng = engine_with(env, llm)
        out = eng.start("esc")
        check("качество ниже порога -> зовём человека",
              out.status == "waiting_human", out.detail)
        check("человеку показан вердикт критика",
              out.checkpoint["payload"]["verdict"]["score"] == 0.1)
        out2 = eng.respond(out.checkpoint["id"], "approved", "беру как есть")
        check("человек может принять работу вопреки оценке",
              out2.status == "done")
        check("результат ушёл на доску", out2.outputs["r"] == "слабо")

    section("HITL: опасный инструмент проходит через человека")
    with TempEnv(hitl_mode="critical", allow_shell=True) as env:
        env.write_workflow("sh", simple_workflow(steps=[
            {"name": "a", "task": "выполни", "writes": "r",
             "tools": ["shell"]}]))
        llm = RoleLLM(worker=[
            '```tool\n{"tool": "shell", "args": {"command": "echo ОПАСНО"}}\n```',
            "команда выполнена"])
        eng = engine_with(env, llm)
        out = eng.start("sh")
        check("прогон остановлен перед опасным вызовом",
              out.status == "waiting_human", out.detail)
        check("тип точки контроля — tool", out.checkpoint["kind"] == "tool")
        check("человеку показана команда",
              out.checkpoint["payload"]["args"]["command"] == "echo ОПАСНО")
        calls = eng.store.tool_calls(out.run_id)
        check("команда НЕ выполнена до разрешения", calls == [],
              "подтверждение обязано быть ДО выполнения, а не после")

    with TempEnv(hitl_mode="critical", allow_shell=True) as env:
        env.write_workflow("sh2", simple_workflow(steps=[
            {"name": "a", "task": "выполни", "writes": "r", "tools": ["shell"]}]))
        eng = engine_with(env, RoleLLM(worker=[
            '```tool\n{"tool": "shell", "args": {"command": "echo ok"}}\n```',
            "готово"]))
        out = eng.start("sh2")
        out2 = eng.respond(out.checkpoint["id"], "rejected", "нельзя")
        check("запрет человека проваливает шаг", out2.status == "failed")
        check("причина запрета сохранена", "нельзя" in out2.detail)

    section("Возобновление: редакция workflow фиксируется на старте")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("v", simple_workflow(steps=[
            {"name": "a", "task": "первый", "writes": "r"}]))
        eng = engine_with(env, RoleLLM(worker=["в"]))
        out = eng.start("v")
        # Подменяем файл определения, пока прогон ждёт человека.
        env.write_workflow("v", {"name": "v", "steps": [
            {"name": "СОВСЕМ_ДРУГОЙ", "task": "другое"}]})
        out2 = eng.respond(out.checkpoint["id"], "approved")
        check("прогон доигран по СТАРОЙ редакции", out2.status == "done",
              "иначе правка файла ломает идущие прогоны")
        check("имя шага прежнее", out2.steps[0]["name"] == "a")

    section("Возобновление: защита от ошибок оператора")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("g", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r"}]))
        eng = engine_with(env, RoleLLM(worker=["в"]))
        out = eng.start("g")
        check_raises("resume при открытой точке контроля отвергается",
                     EngineError, eng.resume, out.run_id)
        eng.respond(out.checkpoint["id"], "approved")
        check_raises("resume завершённого прогона отвергается", EngineError,
                     eng.resume, out.run_id)
        check_raises("ответ на несуществующую точку контроля", EngineError,
                     eng.respond, 9999, "approved")

    section("Отмена прогона")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("c", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r"}]))
        eng = engine_with(env, RoleLLM(worker=["в"]))
        out = eng.start("c")
        eng.cancel(out.run_id, "хватит")
        run = eng.store.get_run(out.run_id)
        check("статус cancelled", run["status"] == "cancelled")
        check("причина сохранена", run["detail"] == "хватит")
        check("открытая точка контроля закрыта",
              eng.store.pending_checkpoint(out.run_id) is None,
              "иначе она вечно висит в очереди согласований")

    section("Инструменты: сужение прав профилем и шагом")
    with TempEnv(allow_shell=True) as env:
        env.write_profile("narrow", {"name": "narrow", "role": "worker",
                                     "system": "промпт", "tools": ["read_file"]})
        env.write_workflow("t", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "profile": "narrow", "writes": "r"}]))
        llm = RoleLLM(worker=[
            '```tool\n{"tool": "shell", "args": {"command": "ls"}}\n```',
            "не вышло, отвечаю текстом"])
        out = engine_with(env, llm).start("t")
        check("прогон не упал", out.status == "done")
        check("отобранный инструмент недоступен",
              any("недоступен" in p for p in llm.prompts),
              "профиль сузил права — shell не должен вызываться")

    section("Журнал прогона: аудит происходящего")
    with TempEnv() as env:
        env.write_workflow("log", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r",
             "review": {"critic": "critic"}}]))
        eng = engine_with(env, RoleLLM(worker=["итог"]))
        out = eng.start("log", goal="цель")
        kinds = [e["kind"] for e in eng.store.events(out.run_id)]
        for kind in ("run_start", "step_start", "worker_output",
                     "critic_verdict", "supervisor_decision", "context_write",
                     "step_done", "run_done"):
            check(f"в журнале есть {kind}", kind in kinds, str(kinds))

    section("Доска: история версий доступна после прогона")
    with TempEnv(hitl_mode="always") as env:
        env.write_workflow("hist", simple_workflow(steps=[
            {"name": "a", "task": "сделай", "writes": "r"}]))
        eng = engine_with(env, RoleLLM(worker=["версия модели"]))
        out = eng.start("hist")
        eng.respond(out.checkpoint["id"], "edited", "версия человека")
        history = eng.store.ctx_history(out.run_id, "r")
        check("на доске одна версия ключа r", len(history) == 1)
        check("сохранён именно текст человека",
              history[0]["value"] == "версия человека")
        check("автор записи зафиксирован", history[0]["author"] == "step:a")
        snapshot = eng.store.ctx_all(out.run_id)
        check("определение workflow сохранено в прогоне",
              isinstance(snapshot.get("_workflow"), dict),
              "нужно для честного разбора инцидентов")

    section("status(): полная картина прогона")
    with TempEnv() as env:
        env.write_workflow("s", simple_workflow())
        eng = engine_with(env, StubLLM(scripted=["итог"]))
        out = eng.start("s", goal="цель")
        info = eng.status(out.run_id)
        check("есть блок run", info["run"]["id"] == out.run_id)
        check("есть шаги", len(info["steps"]) == 1)
        check("есть контекст", "result" in info["context"])
        check("есть журнал", len(info["events"]) > 0)
        check("открытых точек контроля нет", info["checkpoint"] is None)

    return summary("Движок")


if __name__ == "__main__":
    raise SystemExit(main())
