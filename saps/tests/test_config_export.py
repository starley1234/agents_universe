"""Тесты конфигурации, выгрузок и плагинов.

Конфиг — граница доверия: он решает, писать ли в промышленный PDM и
слушать ли сеть. Выгрузки — то, что уходит регулятору и на совещания,
поэтому проверяется не только формат файла, но и ЧЕСТНОСТЬ содержимого:
протокол не должен печатать «соответствует» без доказательств.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, check_raises, make_config, make_store,  # noqa: E402
                     section, skip_section, summary)
from saps.config import Config, ConfigError, mask_dsn              # noqa: E402


def clean_env() -> None:
    for key in list(os.environ):
        if key.startswith("SAPS_"):
            del os.environ[key]


def main() -> int:
    clean_env()

    section("Конфигурация: умолчания")
    cfg = Config()
    check("PostgreSQL обязателен: DSN пуст", cfg.db_dsn == "")
    check_raises("без DSN — понятный отказ", ConfigError, cfg.require_dsn)
    check("запись в Teamcenter выключена", cfg.tc_write_enabled is False,
          "обратная запись меняет промышленный PDM — только осознанно")
    check("LLM по умолчанию не настроена", cfg.llm_provider == "none")
    check("температура 0 для воспроизводимости", cfg.llm_temperature == 0.0)
    check("эмбеддер офлайновый", cfg.embedding_provider == "hash")
    check("слушаем только localhost", cfg.host == "127.0.0.1")

    section("Окружение перебивает умолчания")
    os.environ["SAPS_DB_DSN"] = "postgresql://u:p@h/db"
    os.environ["SAPS_TC_WRITE"] = "true"
    os.environ["SAPS_TC_URL"] = "http://tc:8080/tc"
    os.environ["SAPS_QUALITY_MIN"] = "0.85"
    os.environ["SAPS_EMBEDDING_DIM"] = "256"
    cfg = Config.load()
    check("DSN прочитан", cfg.db_dsn == "postgresql://u:p@h/db")
    check("булево прочитано", cfg.tc_write_enabled is True)
    check("дробное прочитано", cfg.quality_min_score == 0.85)
    check("целое прочитано", cfg.embedding_dim == 256)
    clean_env()

    os.environ["SAPS_EMBEDDING_DIM"] = "не число"
    check_raises("нечисловое значение отвергается", ConfigError, Config.load)
    clean_env()

    section("Секреты только из окружения")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cfg.json"
        path.write_text(json.dumps({
            "db_schema": "custom",
            "tc_password": "ПАРОЛЬ-ИЗ-ФАЙЛА",
            "llm_api_key": "sk-ИЗ-ФАЙЛА",
            "api_token": "ТОКЕН-ИЗ-ФАЙЛА",
        }), encoding="utf-8")
        cfg = Config.load(path)
        check("обычное поле из файла применилось", cfg.db_schema == "custom")
        check("пароль Teamcenter из файла ПРОИГНОРИРОВАН", cfg.tc_password == "")
        check("ключ LLM из файла ПРОИГНОРИРОВАН", cfg.llm_api_key == "")
        check("токен API из файла ПРОИГНОРИРОВАН", cfg.api_token == "")

        bad = Path(tmp) / "bad.json"
        bad.write_text("{сломано", encoding="utf-8")
        check_raises("битый JSON", ConfigError, Config.load, bad)
        unknown = Path(tmp) / "u.json"
        unknown.write_text(json.dumps({"нет_поля": 1}), encoding="utf-8")
        check_raises("неизвестный параметр", ConfigError, Config.load, unknown)
        check_raises("указанный файл обязан существовать", ConfigError,
                     Config.load, Path(tmp) / "нет.json")

    os.environ["SAPS_TC_PASSWORD"] = "секрет"
    cfg = Config.load()
    check("пароль из окружения прочитан", cfg.tc_password == "секрет")
    check("to_dict маскирует пароль", cfg.to_dict()["tc_password"] == "***")
    clean_env()

    section("Маскирование строки подключения")
    check("пароль в DSN скрыт",
          mask_dsn("postgresql://saps:secret@host:5432/db")
          == "postgresql://saps:***@host:5432/db")
    check("хост остаётся виден",
          "host:5432/db" in mask_dsn("postgresql://saps:secret@host:5432/db"),
          "инженер должен видеть, к какой базе подключён")
    check("DSN без пароля не ломается",
          mask_dsn("postgresql:///db") == "postgresql:///db")
    check("пустой DSN", mask_dsn("") == "")

    section("Валидация")
    check_raises("порог качества вне [0..1]", ConfigError,
                 Config(quality_min_score=1.5).validate)
    check_raises("top_k меньше 1", ConfigError,
                 Config(classify_top_k=0).validate)
    check_raises("слишком малая размерность", ConfigError,
                 Config(embedding_dim=2).validate)
    check_raises("сеть без токена запрещена", ConfigError,
                 Config(host="0.0.0.0").validate)
    Config(host="0.0.0.0", api_token="t").validate()
    check("сеть с токеном разрешена", True)
    check_raises("запись в TC без адреса", ConfigError,
                 Config(tc_write_enabled=True).validate)
    check_raises("Teamcenter без пароля", ConfigError,
                 Config(tc_url="http://x").require_tc)

    section("Порог классификатора зависит от шкалы эмбеддера")
    hash_cfg = Config(embedding_provider="hash")
    sem_cfg = Config(embedding_provider="openai")
    check("для hash порог ниже",
          hash_cfg.effective_classify_min() < sem_cfg.effective_classify_min(),
          "у мешка слов косинус физически не достигает 0.8")
    check("для семантической модели — основной порог",
          sem_cfg.effective_classify_min() == sem_cfg.classify_min_score)

    section("describe() — сводка для инженера")
    text = Config(db_dsn="postgresql://u:p@h/db", tc_url="http://tc").describe()
    check("виден режим Teamcenter (только чтение по умолчанию)",
          "[чтение]" in text and "запись разрешена" not in text, text)
    check("пароль не светится", "p@h" not in text or "***" in text)

    section("Запись и чтение Office-форматов")
    from saps.export.writers import write_docx, write_xlsx
    from saps.ingest.excel import read_workbook
    from saps.ingest.word import read_blocks
    tmpdir = Path(tempfile.mkdtemp(prefix="saps_exp_"))

    docx = write_docx(tmpdir / "a.docx", [
        {"type": "heading", "text": "Заголовок", "level": 1},
        {"type": "paragraph", "text": "Текст с «кавычками» и <тегами> & амперсандом"},
        {"type": "table", "header": ["A", "B"], "rows": [["1", "2"]]},
    ], title="Документ")
    blocks = read_blocks(docx)
    check("docx читается обратно", len(blocks) >= 3)
    check("спецсимволы сохранены",
          any("амперсандом" in b.text for b in blocks))
    check("таблица на месте", any(b.kind == "table" for b in blocks))
    check("многострочный текст не ломает файл",
          len(read_blocks(write_docx(tmpdir / "m.docx", [
              {"type": "paragraph", "text": "строка1\nстрока2"}]))) >= 1)

    xlsx = write_xlsx(tmpdir / "a.xlsx", {
        "Лист один": {"header": ["Кол1", "Кол2"],
                      "rows": [["значение", 42], ["кириллица", 3.14]]},
        "Второй": {"header": ["X"], "rows": [["y"]]}})
    wb = read_workbook(xlsx)
    check("оба листа записаны", set(wb) == {"Лист один", "Второй"}, str(set(wb)))
    check("числа сохранены как числа", "42" in str(wb["Лист один"][1]))
    check("кириллица сохранена", "кириллица" in str(wb["Лист один"][2]))
    check("пустая книга не падает",
          isinstance(read_workbook(write_xlsx(tmpdir / "e.xlsx", {})), dict))

    if harness.server() is None:
        skip_section("Отчёты и плагины", harness.SKIP_REASON)
        return summary("Конфигурация и выгрузки")

    from saps.export.reports import (collect_compliance, compliance_docx,
                                     compliance_xlsx, requirements_xlsx)
    from saps.plugins import base as plugins

    section("Протокол соответствия: честность содержимого")
    st = make_store(dim=64)
    cfg = make_config(embedding_dim=64, workdir=str(tmpdir))

    r1 = st.create_requirement("REQ-1", "Полностью закрытое требование",
                               node_code="УЗЕЛ-1", status="approved")
    clause = st.upsert_clause("АП-25", "25.1309", title="Оборудование")
    link = st.link_requirement_clause(r1, clause, score=0.9)
    st.confirm_link(link, "Иванов")
    item = st.add_compliance_item(r1, "MC2")
    st.add_evidence(item, title="Расчёт 12-345")
    st.set_compliance_status(item, "compliant")

    r2 = st.create_requirement("REQ-2", "Требование без покрытия",
                               node_code="УЗЕЛ-1")
    r3 = st.create_requirement("REQ-3", "Статус есть, доказательств нет",
                               node_code="УЗЕЛ-1")
    item3 = st.add_compliance_item(r3, "MC4")
    st.set_compliance_status(item3, "compliant")

    data = collect_compliance(st)
    rows = {r["external_id"]: r for r in data["rows"]}
    check("полностью закрытое -> соответствует",
          rows["REQ-1"]["verdict"] == "соответствует")
    check("без покрытия -> не подтверждено",
          rows["REQ-2"]["verdict"] == "не подтверждено")
    check("статус без доказательств -> НЕ соответствует статусу",
          rows["REQ-3"]["verdict"] == "не подтверждено",
          "нельзя печатать «соответствует» без доказательного документа")
    check("пробелы перечислены", rows["REQ-2"]["gaps"])
    check("подсчёт пробелов", data["gaps_total"] >= 3)
    check("неподтверждённые связи не идут в графу «Пункт АП»",
          rows["REQ-2"]["clauses"] == "—")

    st.link_requirement_clause(r2, clause, score=0.7)   # НЕ подтверждена
    data = collect_compliance(st)
    rows = {r["external_id"]: r for r in data["rows"]}
    check("неподтверждённая связь по-прежнему не в протоколе",
          rows["REQ-2"]["clauses"] == "—",
          "в отчёт для регулятора идёт только подтверждённое человеком")
    check("но она видна отдельной графой",
          "25.1309" in rows["REQ-2"]["unconfirmed_clauses"])

    section("Файлы протокола")
    docx_path = compliance_docx(st, cfg, tmpdir / "protocol.docx")
    check("Word-протокол создан", docx_path.exists())
    blocks = read_blocks(docx_path)
    # Текст ищем И в абзацах, И в ячейках таблиц: расшифровка кодов MoC
    # и матрица — это таблицы, а не абзацы.
    text_all = " ".join(b.text for b in blocks if b.text)
    table_text = " ".join(cell for b in blocks if b.kind == "table"
                          for row in b.rows for cell in row)
    check("есть предупреждение о незакрытых позициях",
          "НЕ ЯВЛЯЕТСЯ доказательством" in text_all,
          "документ с дырами обязан честно об этом говорить")
    check("есть матрица соответствия",
          any(b.kind == "table" for b in blocks))
    check("расшифровка кодов MoC приложена",
          "MC2" in table_text and "MC9" in table_text)
    check("в матрице виден вердикт по требованию",
          "не подтверждено" in table_text or "соответствует" in table_text)
    check("раздел пробелов заполнен",
          "не назначен метод подтверждения" in table_text)

    xlsx_path = compliance_xlsx(st, tmpdir / "protocol.xlsx")
    wb = read_workbook(xlsx_path)
    check("три листа в Excel-протоколе",
          set(wb) == {"Матрица", "Пробелы", "Сводка"}, str(set(wb)))
    check("матрица заполнена", len(wb["Матрица"]) == 4)
    check("лист пробелов заполнен", len(wb["Пробелы"]) > 1)

    reqs_path = requirements_xlsx(st, tmpdir / "reqs.xlsx")
    check("срез требований выгружен", read_workbook(reqs_path)["Требования"])

    full = collect_compliance(st, node_code="УЗЕЛ-1")
    check("фильтр по узлу работает", full["total"] == 3)
    check("фильтр по несуществующему узлу",
          collect_compliance(st, node_code="НЕТ")["total"] == 0)

    section("Плагины")
    names = plugins.available()
    check("встроенные плагины зарегистрированы",
          {"code_review", "report"} <= set(names), str(names))
    described = plugins.describe_all(cfg, st)
    check("описания получены", all("name" in d for d in described))
    check_raises("неизвестный плагин", plugins.PluginError,
                 plugins.create, "нет_такого", cfg, st)

    rep = plugins.create("report", cfg, st).run(fmt="xlsx")
    check("плагин отчёта отработал", rep.processed == 1, str(rep.to_dict()))
    check("файл создан",
          Path(rep.findings[0]["files"][0]).exists())
    bad_fmt = plugins.create("report", cfg, st).run(fmt="pdf")
    check("неизвестный формат — понятная ошибка", bool(bad_fmt.errors))

    section("Плагин ревью кода (DO-178C)")
    src = tmpdir / "src"
    src.mkdir(exist_ok=True)
    (src / "control.c").write_text(
        "/* Реализация [REQ-1] */\n"
        "#include <stdlib.h>\n"
        "void loop(void){\n"
        "  char *p = malloc(100);\n"
        "  goto end;\n"
        "end:\n"
        "  free(p);\n"
        "}\n", encoding="utf-8")
    (src / "util.py").write_text(
        "def run():\n"
        "    try:\n"
        "        eval('2+2')\n"
        "    except:\n"
        "        pass\n", encoding="utf-8")

    cr = plugins.create("code_review", cfg, st).run(path=str(src))
    check("файлы просмотрены", cr.processed == 2, str(cr.processed))
    codes = {f.get("code") for f in cr.findings if "code" in f}
    check("динамическая память найдена", "dynamic_memory" in codes, str(codes))
    check("goto найден", "goto" in codes)
    check("eval найден", "eval_exec" in codes)
    check("bare except найден", "bare_except" in codes)
    trace = next(f for f in cr.findings if f.get("kind") == "traceability")
    check("прослеживаемость: REQ-1 найден в коде",
          "REQ-1" in trace["traced_requirements"])
    check("требования без кода перечислены",
          "REQ-2" in trace["requirements_without_code"])
    check("файл без ссылок помечен",
          "util.py" in trace["files_without_traces"])

    (src / "ghost.c").write_text("/* [REQ-999] нет такого */\nint x;\n",
                                 encoding="utf-8")
    cr2 = plugins.create("code_review", cfg, st).run(path=str(src))
    check("ссылка на несуществующее требование замечена",
          any("REQ-999" in e for e in cr2.errors), str(cr2.errors))
    missing = plugins.create("code_review", cfg, st).run()
    check("без пути — понятная ошибка", bool(missing.errors))

    st.close()
    harness.cleanup()
    return summary("Конфигурация и выгрузки")


if __name__ == "__main__":
    raise SystemExit(main())
