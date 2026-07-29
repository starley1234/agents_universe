"""Тесты системы. Запуск: python3 tests/test_all.py

Философия та же, что в инженерной части: тест обязан УМЕТЬ ПАДАТЬ.
Поэтому рядом с проверками «работает» стоят негативные проверки —
подсовываем заведомо плохой ввод и требуем, чтобы система его отвергла.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.build import build_agent                      # noqa: E402
from agent.config import Config                          # noqa: E402
from agent.core import Agent                             # noqa: E402
from agent.llm.base import BaseLLM, LLMReply, ToolCall   # noqa: E402
from agent.tools.base import ToolError, ToolRegistry, Workspace  # noqa: E402
from agent.tools import files as files_tools             # noqa: E402
from agent.tools import shell as shell_tools             # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


# --------------------------------------------------------------- заглушка
class ScriptedLLM(BaseLLM):
    """Модель по сценарию: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, script: list[LLMReply]) -> None:
        super().__init__("scripted")
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        return self.script.pop(0) if self.script else LLMReply(text="конец")


# =========================================================== workspace
def test_workspace() -> None:
    section("Изоляция рабочей папки")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        p = ws.resolve("sub/file.txt")
        check("обычный путь разрешается", str(p).startswith(str(ws.root)))

        # НЕГАТИВНЫЕ: выход наружу обязан быть отвергнут
        for bad in ["../secret", "../../etc/passwd", "/etc/passwd",
                    "sub/../../out", "./../../x"]:
            try:
                ws.resolve(bad)
                check(f"отклонён выход {bad!r}", False, "путь пропущен!")
            except ToolError:
                check(f"отклонён выход {bad!r}", True)

        # симлинк наружу тоже должен отсекаться
        outside = Path(td).parent / "agent_outside_probe"
        outside.mkdir(exist_ok=True)
        link = Path(td) / "link"
        try:
            link.symlink_to(outside)
            try:
                ws.resolve("link/x")
                check("отклонён симлинк наружу", False, "прошёл!")
            except ToolError:
                check("отклонён симлинк наружу", True)
        except OSError:
            print("  skip симлинки недоступны")
        finally:
            outside.rmdir()


# =========================================================== files
def test_files() -> None:
    section("Файловые инструменты")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        tools = {t.name: t for t in files_tools.build(ws)}

        tools["write_file"].fn(path="a.txt", content="раз\nдва\nтри\n")
        check("файл создан", (ws.root / "a.txt").exists())
        check("чтение возвращает содержимое",
              "два" in tools["read_file"].fn(path="a.txt"))
        check("чтение диапазона",
              "два" in tools["read_file"].fn(path="a.txt", start=2, end=2))

        tools["edit_file"].fn(path="a.txt", old_text="два", new_text="ДВА")
        check("правка применена", "ДВА" in (ws.root / "a.txt").read_text())

        # НЕГАТИВНЫЕ
        try:
            tools["edit_file"].fn(path="a.txt", old_text="нетто", new_text="x")
            check("отказ при отсутствии фрагмента", False)
        except ToolError:
            check("отказ при отсутствии фрагмента", True)

        tools["write_file"].fn(path="b.txt", content="дубль\nдубль\n")
        try:
            tools["edit_file"].fn(path="b.txt", old_text="дубль", new_text="x")
            check("отказ при неоднозначной замене", False, "заменил вслепую!")
        except ToolError:
            check("отказ при неоднозначной замене", True)

        try:
            tools["read_file"].fn(path="нет-такого.txt")
            check("отказ при чтении несуществующего", False)
        except ToolError:
            check("отказ при чтении несуществующего", True)

        check("поиск находит", "a.txt" in tools["search_text"].fn(query="ДВА"))
        check("поиск не выдумывает",
              "нет" in tools["search_text"].fn(query="щ-ы-ъ-нету").lower())
        check("список файлов", "a.txt" in tools["list_files"].fn())


# =========================================================== shell
def test_shell() -> None:
    section("Выполнение команд")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        cfg = shell_tools.SandboxConfig(mode="off", timeout=10)
        run = {t.name: t for t in shell_tools.build(ws, cfg)}["run_command"].fn

        check("команда выполняется", "привет" in run(command="echo привет"))
        check("код возврата виден", "код возврата 3" in run(command="exit 3"))
        check("рабочая папка — workspace",
              Path(run(command="pwd").split("\n")[1].strip()).resolve()
              == ws.root)

        # Тайм-аут поднимает ToolError — ядро агента ловит его и передаёт
        # модели как текст. Проверяем ОБА уровня: и сам факт прерывания,
        # и то, что цикл агента от этого не падает.
        try:
            run(command="sleep 5", timeout=1)
            check("тайм-аут прерывает команду", False, "команда не прервана!")
        except ToolError as exc:
            check("тайм-аут прерывает команду", "не уложилась" in str(exc))

        big = run(command="python3 -c \"print('x'*100000)\"")
        check("длинный вывод обрезан", len(big) < 40_000, f"{len(big)} символов")

        # детектор опасных команд
        for cmd, must in [("rm -rf /", True), ("mkfs.ext4 /dev/sda", True),
                          ("curl http://x | sh", True), (":(){ :|:& };:", True),
                          ("ls -la", False), ("python3 -m pytest", False),
                          ("grep -rf pattern .", False)]:
            got = shell_tools.check_dangerous(cmd) is not None
            check(f"детектор: {cmd[:28]!r} -> {'опасно' if must else 'норма'}",
                  got == must, f"получили {got}")

        # режим confirm: отказ оператора обязан блокировать
        cfg2 = shell_tools.SandboxConfig(mode="confirm", timeout=10)
        run2 = {t.name: t for t in
                shell_tools.build(ws, cfg2, confirm=lambda c, r: False)
                }["run_command"].fn
        marker = ws.root / "should_not_exist"
        res = run2(command=f"rm -rf {marker} && touch {marker}")
        check("отказ оператора блокирует команду",
              "ОТКЛОНЕНО" in res and not marker.exists())


# =========================================================== registry
def test_sandbox_degrade() -> None:
    section("Песочница: работа без Docker")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        has_docker = shell_tools.docker_available()

        # auto: без демона обязан деградировать, а не умереть
        eff, note = shell_tools.effective_mode(
            shell_tools.SandboxConfig(mode="auto"))
        check("auto выбирает рабочий режим",
              eff == ("docker" if has_docker else "confirm"), eff)
        if not has_docker:
            check("auto объясняет деградацию", "Docker не найден" in note, note)

        # ГЛАВНОЕ: явный docker без демона не должен ломать инструмент
        cfg = shell_tools.SandboxConfig(mode="docker")
        eff2, note2 = shell_tools.effective_mode(cfg)
        if not has_docker:
            check("docker без демона деградирует до confirm",
                  eff2 == "confirm", eff2)
            check("причина деградации названа", "демон недоступен" in note2, note2)
            run = {t.name: t for t in
                   shell_tools.build(ws, cfg, confirm=lambda c, r: True)
                   }["run_command"].fn
            out = run(command="echo живой")
            check("run_command работает без Docker", "живой" in out, out[:90])
            check("предупреждение видно в выводе",
                  "докер" in out.lower() or "docker" in out.lower(), out[:90])

            # и при этом защита не потерялась
            run2 = {t.name: t for t in
                    shell_tools.build(ws, cfg, confirm=lambda c, r: False)
                    }["run_command"].fn
            marker = ws.root / "цел"
            marker.write_text("x")
            res = run2(command=f"rm -rf {marker}")
            check("опасная команда всё ещё требует подтверждения",
                  "ОТКЛОНЕНО" in res, res[:90])
            check("файл не удалён", marker.exists())

        # off не спрашивает подтверждений
        run3 = {t.name: t for t in
                shell_tools.build(ws, shell_tools.SandboxConfig(mode="off"),
                                  confirm=lambda c, r: False)
                }["run_command"].fn
        check("режим off выполняет без вопросов",
              "ОТКЛОНЕНО" not in run3(command="echo z"))


def test_sandbox_all_modes() -> None:
    section("run_command работает при ЛЮБОМ режиме песочницы")
    # Регрессия: при mode=docker без демона инструмент был мёртв.
    # Проверяем все режимы сквозь полный цикл агента.
    from agent.build import build_agent
    from agent.config import Config
    from agent.llm.base import BaseLLM as _B

    class Calls(_B):
        def __init__(self):
            super().__init__("t")
            self.n = 0

        def _chat_once(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return LLMReply(tool_calls=[ToolCall(
                    "1", "run_command",
                    {"command": 'python3 -c "print(6*7)"'})])
            last = [m for m in messages if m.get("role") == "tool"][-1]["content"]
            return LLMReply(text=last)

    for mode in ("auto", "docker", "confirm", "off"):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(provider="ollama", model="m", workspace=td,
                         skills=["files", "shell"])
            cfg.sandbox.mode = mode
            agent = build_agent(cfg, confirm=lambda c, r: True)
            agent.llm = Calls()
            res = agent.run("посчитай")
            check(f"режим {mode}: команда выполнена", "42" in res.answer,
                  res.answer[:100])

    # опечатка в конфиге не должна убивать инструмент
    eff, note = shell_tools.effective_mode(
        shell_tools.SandboxConfig(mode="dokcer"))
    check("опечатка в режиме -> безопасный confirm", eff == "confirm", eff)
    check("опечатка объяснена", "неизвестный режим" in note, note)


def test_reasoning_models() -> None:
    section("Reasoning-модели и «украшенные» пути")
    # Реальный случай: unsloth/qwen3.5-9b кладёт всё в reasoning_content,
    # а content оставляет пустым. Агент вставал на втором шаге.
    from agent.llm.openai_like import OpenAILike

    data = {"choices": [{"message": {
        "role": "assistant", "content": "",
        "reasoning_content": "Рассуждение модели о задаче",
        "tool_calls": []}}]}
    r = OpenAILike._parse(data)
    check("reasoning_content подхвачен", "Рассуждение" in r.text, repr(r.text))

    data2 = {"choices": [{"message": {
        "role": "assistant", "content": "обычный ответ",
        "reasoning_content": "черновик"}}]}
    check("обычный content в приоритете",
          OpenAILike._parse(data2).text == "обычный ответ")

    # markdown-обёртка вокруг пути — тоже из лога
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        (ws.root / "main.py").write_text("x = 1", encoding="utf-8")
        for raw in ("[main.py](http://main.py)", "`main.py`", " main.py ",
                    "<main.py>", "file://main.py"):
            got = ws.resolve(raw)
            check(f"путь {raw[:24]!r} распознан", got.exists(), str(got))
        # защита обязана остаться
        for evil in ("../../etc/passwd", "[x](y)/../../etc/passwd"):
            try:
                ws.resolve(evil)
                check(f"выход {evil[:20]!r} отклонён", False, "пропущен!")
            except ToolError:
                check(f"выход {evil[:20]!r} отклонён", True)

    # РЕАЛЬНЫЙ СЛУЧАЙ из лога qwen3.5-9b: content пуст, весь текст в
    # reasoning_content — модель РАССУЖДАЕТ («Покажу возможности: 1…»),
    # но не действует. Агент принимал намерение за результат и вставал.
    data3 = {"choices": [{"message": {
        "role": "assistant", "content": "",
        "reasoning_content": "Покажу возможности: 1. Запущу скрипт",
        "tool_calls": []}}]}
    r3 = OpenAILike._parse(data3)
    check("ход из reasoning помечен флагом", r3.from_reasoning, str(r3))
    check("обычный ответ флагом не помечен",
          not OpenAILike._parse(data2).from_reasoning)

    from agent.llm.base import BaseLLM as _B0

    class Reasoner(_B0):
        """Повторяет лог: сначала рассуждение, после подталкивания — дело."""

        def __init__(self):
            super().__init__("qwen3.5-9b")
            self.n = 0

        def _chat_once(self, messages, tools=None):
            self.n += 1
            if self.n == 1:
                return LLMReply(text="Покажу возможности: 1. Запущу скрипт",
                                from_reasoning=True)
            return LLMReply(text="Готово: скрипт запущен.")

    rs = Reasoner()
    res_r = Agent(rs, ToolRegistry(), max_steps=6).run("покажи вау")
    check("рассуждение не принято за результат",
          res_r.answer == "Готово: скрипт запущен.", res_r.answer[:60])
    check("модель подтолкнули к действию", rs.n == 2, str(rs.n))

    class Stubborn(_B0):
        def _chat_once(self, messages, tools=None):
            return LLMReply(text="всё рассуждаю", from_reasoning=True)

    res_s = Agent(Stubborn("t"), ToolRegistry(), max_steps=10).run("x")
    check("упрямая reasoning-модель не зацикливает",
          len(res_s.steps) <= 5, str(len(res_s.steps)))
    check("её рассуждение всё же отдано как ответ",
          "рассужда" in res_s.answer, res_s.answer[:50])

    # пустой ответ не должен завершать задачу
    from agent.llm.base import BaseLLM as _B

    class Empty(_B):
        def __init__(self):
            super().__init__("t")
            self.n = 0

        def _chat_once(self, messages, tools=None):
            self.n += 1
            if self.n <= 2:
                return LLMReply(text="")      # молчит дважды
            return LLMReply(text="теперь отвечаю")

    llm = Empty()
    res = Agent(llm, ToolRegistry(), max_steps=6).run("задача")
    check("пустой ответ не считается выполнением",
          res.answer == "теперь отвечаю", res.answer)
    check("модель получила побуждение ответить", llm.n == 3, str(llm.n))

    # но бесконечно уговаривать тоже нельзя
    class AlwaysEmpty(_B):
        def _chat_once(self, messages, tools=None):
            return LLMReply(text="")

    res2 = Agent(AlwaysEmpty("t"), ToolRegistry(), max_steps=8).run("x")
    check("вечно пустой ответ не зацикливает",
          len(res2.steps) <= 4, str(len(res2.steps)))


def test_registry() -> None:
    section("Реестр инструментов")
    reg = ToolRegistry()
    with tempfile.TemporaryDirectory() as td:
        reg.extend(files_tools.build(Workspace(td)))
    check("инструменты зарегистрированы", len(reg) == 5, str(len(reg)))
    check("схемы валидны для API",
          all({"name", "description", "parameters"} <= set(s)
              for s in reg.schemas()))
    try:
        reg.get("нет_такого")
        check("отказ на неизвестный инструмент", False)
    except ToolError:
        check("отказ на неизвестный инструмент", True)


# =========================================================== core
def test_core() -> None:
    section("Цикл агента")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        reg = ToolRegistry()
        reg.extend(files_tools.build(ws))

        # сценарий: записать файл, затем ответить текстом
        llm = ScriptedLLM([
            LLMReply(tool_calls=[ToolCall("1", "write_file",
                                          {"path": "r.txt", "content": "готово"})]),
            LLMReply(text="Файл создан."),
        ])
        res = Agent(llm, reg, max_steps=5).run("создай файл")
        check("инструмент вызван", (ws.root / "r.txt").exists())
        check("ответ получен", res.answer == "Файл создан.")
        check("причина остановки — done", res.stopped_by == "done")
        check("шаги посчитаны", len(res.steps) == 2, str(len(res.steps)))
        check("вызовы посчитаны", res.tool_calls == 1)

        # ошибка инструмента НЕ роняет агента
        llm2 = ScriptedLLM([
            LLMReply(tool_calls=[ToolCall("1", "read_file", {"path": "нет.txt"})]),
            LLMReply(text="Файла нет, сообщаю."),
        ])
        res2 = Agent(llm2, reg, max_steps=5).run("прочитай")
        check("ошибка инструмента не роняет цикл", res2.stopped_by == "done")
        fed = [m for m in res2.messages if m.get("role") == "tool"]
        check("текст ошибки ушёл модели",
              fed and "ОШИБКА" in fed[0]["content"])

        # неизвестный инструмент
        llm3 = ScriptedLLM([
            LLMReply(tool_calls=[ToolCall("1", "нет_такого", {})]),
            LLMReply(text="понял"),
        ])
        res3 = Agent(llm3, reg, max_steps=5).run("x")
        check("неизвестный инструмент обработан", res3.stopped_by == "done")

        # неверные аргументы
        llm4 = ScriptedLLM([
            LLMReply(tool_calls=[ToolCall("1", "write_file", {"путь": "x"})]),
            LLMReply(text="исправлюсь"),
        ])
        res4 = Agent(llm4, reg, max_steps=5).run("x")
        fed4 = [m for m in res4.messages if m.get("role") == "tool"]
        check("неверные аргументы -> понятная ошибка",
              fed4 and "ОШИБКА" in fed4[0]["content"])

        # лимит шагов: агент обязан остановиться и признать это
        loop = ScriptedLLM([
            LLMReply(tool_calls=[ToolCall(str(i), "list_files", {})])
            for i in range(20)
        ])
        res5 = Agent(loop, reg, max_steps=3).run("бесконечно")
        check("лимит шагов срабатывает", res5.stopped_by == "max_steps")
        check("лимит честно объявлен", "предел" in res5.answer.lower())

        # обрезка истории не выбрасывает системный промпт и задачу
        agent = Agent(ScriptedLLM([]), reg, max_history_chars=500)
        long_hist = [{"role": "system", "content": "СИСТЕМА"},
                     {"role": "user", "content": "ЗАДАЧА"}]
        long_hist += [{"role": "tool", "tool_call_id": str(i),
                       "content": "x" * 400} for i in range(20)]
        trimmed = agent._trim(long_hist)
        check("системный промпт сохранён", trimmed[0]["content"] == "СИСТЕМА")
        check("задача сохранена", trimmed[1]["content"] == "ЗАДАЧА")
        check("история реально урезана", len(trimmed) < len(long_hist),
              f"{len(trimmed)} из {len(long_hist)}")


# =========================================================== config
def test_economy() -> None:
    section("Экономия токенов")
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        reg = ToolRegistry()
        reg.extend(files_tools.build(ws))
        (ws.root / "big.txt").write_text("данные\n" * 3000, encoding="utf-8")

        class Rec(BaseLLM):
            def __init__(self, script):
                super().__init__("t")
                self.script = list(script)
                self.sizes = []

            def chat(self, messages, tools=None):
                self.sizes.append(sum(len(json.dumps(m, ensure_ascii=False))
                                      for m in messages))
                return self.script.pop(0) if self.script else LLMReply(text="всё")

        script = [LLMReply(tool_calls=[ToolCall(str(i), "read_file",
                                                {"path": "big.txt"})])
                  for i in range(6)]

        a = Rec(list(script))
        Agent(a, reg, max_steps=7, tool_result_limit=0,
              keep_last_results=0).run("x")
        b = Rec(list(script))
        Agent(b, reg, max_steps=7, tool_result_limit=4000,
              keep_last_results=3).run("x")
        check("экономия реально уменьшает контекст",
              b.sizes[-1] < a.sizes[-1] * 0.6,
              f"{a.sizes[-1]} -> {b.sizes[-1]}")

        # обрезка не должна терять начало и конец
        ag = Agent(Rec([]), reg, tool_result_limit=100)
        clipped = ag._clip("НАЧАЛО" + "x" * 5000 + "КОНЕЦ")
        check("обрезка сохраняет начало", clipped.startswith("НАЧАЛО"))
        check("обрезка сохраняет конец", clipped.endswith("КОНЕЦ"))
        check("обрезка укладывается в лимит", len(clipped) < 300, str(len(clipped)))
        check("короткий текст не трогается", ag._clip("мало") == "мало")

        # свёртка не должна затрагивать свежие результаты
        ag2 = Agent(Rec([]), reg, keep_last_results=2)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        msgs += [{"role": "tool", "tool_call_id": str(i), "content": "Q" * 900}
                 for i in range(5)]
        comp = ag2._compact(msgs)
        tools_left = [m for m in comp if m["role"] == "tool"]
        check("свежие результаты целы",
              all(len(m["content"]) == 900 for m in tools_left[-2:]))
        check("старые результаты свёрнуты",
              all("свёрнуто" in m["content"] for m in tools_left[:-2]))
        check("число сообщений не изменилось", len(comp) == len(msgs))


def test_profiles() -> None:
    section("Профили ролей")
    names = Config.list_profiles()
    check("профили найдены", len(names) >= 4, str(names))
    for want in ("coder", "cad", "research", "marketing"):
        check(f"есть профиль {want}", want in names)

    cad = Config.load(None, profile="cad")
    check("cad подключает навык cad", "cad" in cad.skills, str(cad.skills))
    check("cad задаёт промпт", bool(cad.system_prompt))
    mk = Config.load(None, profile="marketing")
    check("marketing без shell", "shell" not in mk.skills, str(mk.skills))
    check("профиль записан в конфиг", mk.profile == "marketing")

    try:
        Config.load(None, profile="нет_такого")
        check("отказ на неизвестный профиль", False)
    except FileNotFoundError:
        check("отказ на неизвестный профиль", True)

    # профиль применим и собирается в реального агента
    with tempfile.TemporaryDirectory() as td:
        cfg = Config.load(None, profile="cad", provider="ollama",
                          model="m", workspace=td)
        agent = build_agent(cfg)
        check("агент из профиля собирается",
              "scad_collision" in agent.tools.names())


def test_config() -> None:
    section("Конфигурация")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.json"
        p.write_text(json.dumps({
            "provider": "ollama", "model": "qwen2.5-coder",
            "workspace": td, "max_steps": 7,
            "skills": ["files"], "sandbox": {"mode": "off", "timeout": 30},
        }), encoding="utf-8")
        cfg = Config.load(str(p))
        check("провайдер из файла", cfg.provider == "ollama")
        check("лимит шагов из файла", cfg.max_steps == 7)
        check("песочница из файла", cfg.sandbox.mode == "off")
        check("base_url подставлен", bool(cfg.base_url))

        cfg2 = Config.load(str(p), model="другая", sandbox_mode="confirm")
        check("CLI переопределяет модель", cfg2.model == "другая")
        check("CLI переопределяет песочницу", cfg2.sandbox.mode == "confirm")
        check("ключ маскируется в выводе",
              Config.load(str(p)).to_dict().get("api_key") in (None, "***"))

        try:
            Config.load(str(Path(td) / "нет.json"))
            check("отказ на отсутствующий конфиг", False)
        except FileNotFoundError:
            check("отказ на отсутствующий конфиг", True)


# =========================================================== build
def test_build() -> None:
    section("Сборка агента")
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                     skills=["files", "shell", "cad"])
        agent = build_agent(cfg)
        names = agent.tools.names()
        check("файловые навыки на месте", "read_file" in names)
        check("shell на месте", "run_command" in names)
        check("cad на месте", "scad_collision" in names)
        check("инструментов достаточно", len(names) >= 11, str(len(names)))

        cfg.skills = ["files", "выдуманный"]
        try:
            build_agent(cfg)
            check("отказ на неизвестный навык", False)
        except ValueError as exc:
            check("отказ на неизвестный навык", "выдуманный" in str(exc))


# =========================================================== cad
def test_cad() -> None:
    section("CAD-навыки (без OpenSCAD проверяется разбор STL)")
    from agent.skills.cad_openscad import _load_tris, _mesh_stats

    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)

        # Замкнутый тетраэдр: 4 грани, каждое ребро ровно в двух.
        tetra = """solid t
facet normal 0 0 0
 outer loop
  vertex 0 0 0
  vertex 1 0 0
  vertex 0 1 0
 endloop
endfacet
facet normal 0 0 0
 outer loop
  vertex 0 0 0
  vertex 0 1 0
  vertex 0 0 1
 endloop
endfacet
facet normal 0 0 0
 outer loop
  vertex 0 0 0
  vertex 0 0 1
  vertex 1 0 0
 endloop
endfacet
facet normal 0 0 0
 outer loop
  vertex 1 0 0
  vertex 0 0 1
  vertex 0 1 0
 endloop
endfacet
endsolid t
"""
        good = ws.root / "good.stl"
        good.write_text(tetra, encoding="utf-8")
        st = _mesh_stats(_load_tris(good))
        check("замкнутое тело: дыр нет", st["bad_edges"] == 0, str(st))
        check("замкнутое тело: одна компонента", st["components"] == 1)
        check("объём посчитан", abs(st["volume"] - 1 / 6) < 1e-6,
              f"{st['volume']}")

        # НЕГАТИВНЫЙ: убираем одну грань — обязаны увидеть дыру
        holed = ws.root / "holed.stl"
        holed.write_text("\n".join(tetra.splitlines()[:-7]) + "\nendsolid t\n",
                         encoding="utf-8")
        st2 = _mesh_stats(_load_tris(holed))
        check("дырявое тело обнаружено", st2["bad_edges"] > 0,
              "проверка не поймала дыру!")

        # НЕГАТИВНЫЙ: два разнесённых тетраэдра — две компоненты
        shifted = tetra.replace("vertex 0 0 0", "vertex 50 50 50") \
                       .replace("vertex 1 0 0", "vertex 51 50 50") \
                       .replace("vertex 0 1 0", "vertex 50 51 50") \
                       .replace("vertex 0 0 1", "vertex 50 50 51")
        two = ws.root / "two.stl"
        two.write_text(tetra + shifted, encoding="utf-8")
        st3 = _mesh_stats(_load_tris(two))
        check("распавшаяся деталь обнаружена", st3["components"] == 2,
              f"компонент: {st3['components']}")


def main() -> int:
    print("=" * 60)
    print("ТЕСТЫ АГЕНТНОЙ СИСТЕМЫ")
    print("=" * 60)
    test_workspace()
    test_files()
    test_shell()
    test_sandbox_degrade()
    test_sandbox_all_modes()
    test_reasoning_models()
    test_registry()
    test_core()
    test_config()
    test_economy()
    test_profiles()
    test_build()
    test_cad()
    print("\n" + "=" * 60)
    print(f"пройдено: {PASS} · провалено: {FAIL}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
