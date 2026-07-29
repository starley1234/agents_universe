"""Командная строка САПС.

Полный жизненный цикл доступен из терминала: систему ставят в КБ, где
регламентные операции запускают по расписанию, а не кликами.

    saps init                          создать схему в PostgreSQL
    saps check                         самопроверка окружения
    saps import <файл> [--promote]     импорт Word/Excel
    saps staging [--doc N]             что распозналось
    saps promote --doc N               перенести в production
    saps rules load [файл|--builtin]   справочник авиационных правил
    saps index                         пересчитать эмбеддинги
    saps agent editor|classifier|gap   запуск агента
    saps suggestions [--accept N]      очередь предложений
    saps requirements [--owner ...]    список требований
    saps show REQ-123                  карточка требования
    saps health [--node ...]           индикатор здоровья сертификации
    saps export docx|xlsx|requirements выгрузка
    saps plugin list|run <имя>         плагины
    saps tc login|pull|push            работа с Teamcenter
    saps serve                         веб-интерфейс и API

Возврат ненулевого кода при ошибке — обязателен: команды вызывают из
cron и CI, и молчаливый провал там незаметен.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agents import ClassifierAgent, EditorAgent, GapAgent
from .agents.classifier import index_clauses, index_requirements
from .config import Config, ConfigError
from .db.schema import MOC_CODES
from .db.store import Store, StoreError
from .export.reports import (compliance_docx, compliance_xlsx, export_path,
                             requirements_xlsx)
from .ingest.pipeline import import_file, promote, promote_all
from .ingest.sync import pull_item, push_batch
from .ingest.teamcenter import TeamcenterClient, TeamcenterError
from .ingest.word import ParseError
from .llm import build_embedder, build_llm
from .plugins import base as plugins
from .rules.loader import RulesError, list_builtin, load_builtin, load_ruleset


def _store(cfg: Config) -> Store:
    return Store(cfg.require_dsn(), schema=cfg.db_schema, dim=cfg.embedding_dim)


def _embedder(cfg: Config):
    return build_embedder(cfg.embedding_provider, cfg.embedding_model,
                          dim=cfg.embedding_dim, base_url=cfg.embedding_base_url,
                          api_key=cfg.embedding_api_key,
                          timeout=cfg.embedding_timeout)


def _llm(cfg: Config):
    kwargs: dict[str, Any] = {"retries": cfg.llm_retries}
    if cfg.llm_provider not in ("none", "stub"):
        kwargs.update(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key,
                      timeout=cfg.llm_timeout, temperature=cfg.llm_temperature)
    return build_llm(cfg.llm_provider, cfg.llm_model, **kwargs)


def _tc(cfg: Config) -> TeamcenterClient:
    cfg.require_tc()
    return TeamcenterClient(cfg.tc_url, user=cfg.tc_user,
                            password=cfg.tc_password, group=cfg.tc_group,
                            role=cfg.tc_role, locale=cfg.tc_locale,
                            timeout=cfg.tc_timeout)


# --- команды ---------------------------------------------------------------
def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        st.init_schema()
        print(f"Схема {cfg.db_schema!r} создана/обновлена.")
        if args.rules:
            loaded = load_builtin(st, embedder=_embedder(cfg))
            for item in loaded:
                print(f"  справочник {item['ruleset']}: {item['loaded']} пунктов")
        if args.indexes:
            st.build_vector_indexes()
            print("  векторные индексы построены")
    return 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    print(cfg.describe())
    ok = True
    try:
        with _store(cfg) as st:
            stats = st.stats()
            print(f"\n✓ PostgreSQL доступен: требований {stats['requirements']}, "
                  f"пунктов АП {stats['clauses']}, предложений "
                  f"{stats['suggestions_pending']}")
            if stats["clauses"] == 0:
                print("⚠ Справочник авиационных правил пуст — Классификатор "
                      "работать не сможет. Загрузите: saps rules load --builtin")
    except (StoreError, ConfigError) as exc:
        ok = False
        print(f"\n✗ База недоступна: {exc}")

    try:
        emb = _embedder(cfg)
        vec = emb.embed_one("проверка")
        print(f"✓ Эмбеддер {cfg.embedding_provider}: вектор {len(vec)}")
        if cfg.embedding_provider in ("hash", "", "offline"):
            print("  (офлайн-эмбеддер: сравнивает слова, а не смысл — для "
                  "боевого подбора пунктов АП настройте семантическую модель)")
    except Exception as exc:                                     # noqa: BLE001
        ok = False
        print(f"✗ Эмбеддер недоступен: {exc}")

    if cfg.llm_provider == "none":
        print("• LLM не настроена: работают детерминированные проверки, "
              "переформулировка требований недоступна")
    else:
        print(f"✓ LLM: {cfg.llm_provider}:{cfg.llm_model}")

    if cfg.tc_url:
        print(f"• Teamcenter: {cfg.tc_url} "
              f"({'запись разрешена' if cfg.tc_write_enabled else 'только чтение'})")
    else:
        print("• Teamcenter не настроен — доступен только импорт из файлов")

    print("\n" + ("Система готова." if ok else "Есть проблемы — см. выше."))
    return 0 if ok else 1


def cmd_import(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        try:
            result = import_file(st, args.path, actor=args.actor,
                                 allow_duplicate=args.force)
        except ParseError as exc:
            print(f"Ошибка разбора: {exc}", file=sys.stderr)
            return 2
        print(f"Документ #{result.document_id}: {result.name} ({result.kind})")
        s = result.summary
        print(f"  распознано записей: {result.staged} "
              f"(с номером: {s.get('with_id', 0)}, без номера: "
              f"{s.get('without_id', 0)})")
        if s.get("origins"):
            print(f"  источники: {s['origins']}")
        for w in result.warnings:
            print(f"  ⚠ {w}")
        if result.duplicate_of:
            return 1
        if args.promote:
            pr = promote_all(st, result.document_id, actor=args.actor,
                             default_owner=args.owner, default_node=args.node,
                             embedder=_embedder(cfg).embed_one)
            c = pr.to_dict()["counts"]
            print(f"  перенесено в базу: создано {c['created']}, "
                  f"обновлено {c['updated']}, пропущено {c['skipped']}")
            for skip in pr.skipped[:10]:
                print(f"    • {skip.get('external_id', skip['staging_id'])}: "
                      f"{skip['reason']}")
    return 0


def cmd_staging(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        if args.doc is None:
            docs = st.list_documents()
            if not docs:
                print("Документов нет. Импортируйте: saps import <файл>")
                return 0
            print(f"{'#':>4}  {'тип':<11} {'записей':>7}  имя")
            for d in docs:
                print(f"{d['id']:>4}  {d['kind']:<11} {d['records']:>7}  {d['name']}")
            return 0
        rows = st.staging_records(args.doc, status=args.status)
        if not rows:
            print("Записей нет.")
            return 0
        for r in rows:
            raw = r["raw"] or {}
            conf = raw.get("confidence", 0)
            print(f"[{r['id']:>5}] {r['external_id'] or '—':<14} "
                  f"conf={conf:<5} {r['status']:<10} "
                  f"{(r['raw_text'] or '')[:70]}")
            if raw.get("notes"):
                print(f"         замечания: {'; '.join(raw['notes'])}")
    return 0


def cmd_promote(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        if args.doc is not None:
            pr = promote_all(st, args.doc, actor=args.actor,
                             default_owner=args.owner, default_node=args.node,
                             on_conflict=args.on_conflict,
                             embedder=_embedder(cfg).embed_one)
        elif args.ids:
            pr = promote(st, args.ids, actor=args.actor,
                         default_owner=args.owner, default_node=args.node,
                         on_conflict=args.on_conflict,
                         embedder=_embedder(cfg).embed_one)
        else:
            print("Укажите --doc N или --ids 1 2 3", file=sys.stderr)
            return 2
        c = pr.to_dict()["counts"]
        print(f"Создано {c['created']}, обновлено {c['updated']}, "
              f"пропущено {c['skipped']}")
        for skip in pr.skipped:
            print(f"  • {skip.get('external_id', skip['staging_id'])}: "
                  f"{skip['reason']}")
    return 0


def cmd_rules(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        emb = _embedder(cfg)
        if args.action == "list":
            clauses = st.list_clauses(args.ruleset)
            if not clauses:
                print("Справочник пуст. Загрузите: saps rules load --builtin")
                print(f"Встроенные наборы: {', '.join(list_builtin()) or '—'}")
                return 0
            current = ""
            for c in clauses:
                if c["ruleset"] != current:
                    current = c["ruleset"]
                    print(f"\n{current}")
                print(f"  {c['clause']:<12} {c['title']}")
            return 0
        if args.action == "load":
            if args.builtin or not args.path:
                loaded = load_builtin(st, args.ruleset, embedder=emb)
            else:
                loaded = [load_ruleset(st, args.path, embedder=emb)]
            for item in loaded:
                print(f"{item['ruleset']}: загружено {item['loaded']} пунктов")
            return 0
    return 0


def cmd_index(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        emb = _embedder(cfg)
        n_clauses = index_clauses(st, emb)
        n_reqs = index_requirements(st, emb)
        print(f"Пересчитано эмбеддингов: пунктов АП {n_clauses}, "
              f"требований {n_reqs}")
        if args.build_indexes:
            st.build_vector_indexes()
            print("Векторные индексы построены")
    return 0


def cmd_agent(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        if args.name == "editor":
            report = EditorAgent(cfg, st, _llm(cfg)).run(
                owner=args.owner, node_code=args.node,
                suggest_rewrite=args.rewrite)
        elif args.name == "classifier":
            report = ClassifierAgent(cfg, st, _embedder(cfg), _llm(cfg)).run(
                owner=args.owner, node_code=args.node, ruleset=args.ruleset,
                use_llm=args.use_llm)
        elif args.name == "gap":
            report = GapAgent(cfg, st).run(owner=args.owner, node_code=args.node)
        else:
            print(f"Агент {args.name!r} неизвестен", file=sys.stderr)
            return 2

        print(report.summary())
        for err in report.errors:
            print(f"  ⚠ {err}")
        if args.verbose:
            for f in report.findings[:40]:
                print("   " + json.dumps(f, ensure_ascii=False)[:200])
            for s in report.skipped[:20]:
                print(f"   пропуск {s['external_id']}: {s['reason']}")
        if report.suggestions:
            print(f"\nСоздано предложений: {len(report.suggestions)}. "
                  "Посмотреть: saps suggestions")
    return 0


def cmd_suggestions(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        if args.accept:
            for sug_id in args.accept:
                result = st.decide_suggestion(sug_id, "accepted", args.actor)
                print(f"#{sug_id}: принято — {result}")
            return 0
        if args.reject:
            for sug_id in args.reject:
                st.decide_suggestion(sug_id, "rejected", args.actor)
                print(f"#{sug_id}: отклонено")
            return 0
        rows = st.list_suggestions(status=args.status, agent=args.agent)
        if not rows:
            print("Предложений нет.")
            return 0
        for s in rows:
            print(f"\n[{s['id']:>5}] {s['external_id']} — агент {s['agent']}, "
                  f"вид {s['kind']}"
                  + (f", уверенность {float(s['score']):.2f}"
                     if s["score"] is not None else ""))
            if s["rationale"]:
                print(f"   обоснование: {s['rationale'][:200]}")
            if s["kind"] == "text":
                print(f"   было:  {(s['text_before'] or '')[:120]}")
                print(f"   стало: {(s['text_after'] or '')[:120]}")
            else:
                payload = json.dumps(s["payload"], ensure_ascii=False)
                print(f"   предлагается: {payload}")
        print(f"\nВсего: {len(rows)}. Принять: saps suggestions --accept <id>")
    return 0


def cmd_requirements(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        rows = st.list_requirements(owner=args.owner, node_code=args.node,
                                    status=args.status, query=args.query,
                                    limit=args.limit)
        if not rows:
            print("Требований не найдено.")
            return 0
        print(f"{'ID':<14} {'статус':<11} {'кач-во':<7} {'узел':<16} формулировка")
        for r in rows:
            q = (f"{float(r['quality_score']):.2f}"
                 if r["quality_score"] is not None else "—")
            print(f"{r['external_id']:<14} {r['status']:<11} {q:<7} "
                  f"{(r.get('node_code') or ''):<16} {(r['text'] or '')[:60]}")
        print(f"\nВсего: {len(rows)}")
    return 0


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        req = st.get_requirement_by_external(args.external_id)
        if req is None:
            print(f"Требование {args.external_id!r} не найдено", file=sys.stderr)
            return 1
        req_id = int(req["id"])
        full = st.get_requirement(req_id)
        print(f"{full['external_id']} [{full['status']}] "
              f"узел: {full.get('node_code') or '—'}, "
              f"ответственный: {full['owner'] or '—'}")
        print(f"\n{full['text']}\n")
        if full["quality_score"] is not None:
            print(f"Качество формулировки: {float(full['quality_score']):.2f}")
            for issue in (full["quality"] or {}).get("issues", []):
                print(f"  • [{issue['severity']}] {issue['message']}")
        links = st.requirement_links(req_id)
        print("\nПункты авиационных правил:")
        if links:
            for l in links:
                mark = "✓" if l["confirmed"] else "?"
                print(f"  {mark} {l['ruleset']} {l['clause']} — {l['title']} "
                      f"({float(l['score'] or 0):.2f}, {l['source']})")
        else:
            print("  — связей нет")
        items = st.compliance_items(req_id)
        print("\nДоказательная документация:")
        if items:
            for c in items:
                print(f"  {c['moc']} [{c['status']}] {MOC_CODES.get(c['moc'], '')}")
                for ev in c["evidence"]:
                    print(f"      • {ev['title'] or ev['kind']} {ev['uri']}")
                if not c["evidence"]:
                    print("      ⚠ доказательств нет")
        else:
            print("  ⚠ метод подтверждения не назначен")
        revs = st.revisions(req_id)
        print(f"\nИстория: {len(revs)} ревизий")
        for r in revs[-5:]:
            print(f"  v{r['version']} {str(r['created_at'])[:16]} "
                  f"{r['actor'] or '—'}: {r['reason']}")
    return 0


def cmd_health(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        agent = GapAgent(cfg, st)
        if args.by_node:
            rows = agent.health_by_node()
            if not rows:
                print("Узлы изделия не заданы.")
                return 0
            print(f"{'узел':<20} {'треб.':>6} {'готовность':>12}  пробелы")
            for h in rows:
                gaps = h["gaps"]
                print(f"{h['node_code']:<20} {h['total']:>6} "
                      f"{h['health'] * 100:>10.0f}%  "
                      f"АП:{gaps['no_rule_link']} MoC:{gaps['no_moc']} "
                      f"док:{gaps['no_evidence']}")
            return 0
        info = agent.health(node_code=args.node, owner=args.owner)
        print(f"Требований: {info['total']}")
        print(f"Готовность: {info['health'] * 100:.0f}% — {info['status']}")
        print("\nСоставляющие:")
        for key, value in info["factors"].items():
            print(f"  {key:<12} {value * 100:>5.0f}% "
                  f"(вес {info['weights'][key]})")
        print("\nПробелы:")
        for key, value in info["gaps"].items():
            if value:
                print(f"  {key:<18} {value}")
        if info["blocking"]:
            print(f"\n⚠ Статус «соответствует» без доказательств: "
                  f"{', '.join(info['blocking'])}")
    return 0


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    workdir = Path(args.out).parent if args.out else Path(cfg.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    with _store(cfg) as st:
        if args.format == "docx":
            path = Path(args.out) if args.out else export_path(
                workdir, "Протокол_соответствия", "docx")
            compliance_docx(st, cfg, path, node_code=args.node, owner=args.owner)
        elif args.format == "xlsx":
            path = Path(args.out) if args.out else export_path(
                workdir, "Протокол_соответствия", "xlsx")
            compliance_xlsx(st, path, node_code=args.node, owner=args.owner)
        else:
            path = Path(args.out) if args.out else export_path(
                workdir, "Требования", "xlsx")
            requirements_xlsx(st, path, node_code=args.node, owner=args.owner)
        print(f"Сохранено: {path}")
    return 0


def cmd_plugin(cfg: Config, args: argparse.Namespace) -> int:
    with _store(cfg) as st:
        if args.action == "list":
            for item in plugins.describe_all(cfg, st):
                if "error" in item:
                    print(f"✗ {item['name']}: {item['error']}")
                else:
                    print(f"{item['name']:<14} {item['title']}")
            return 0
        try:
            plugin = plugins.create(args.name, cfg, st)
        except plugins.PluginError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2
        kwargs: dict[str, Any] = {}
        for pair in args.arg or []:
            if "=" not in pair:
                print(f"--arg ожидает key=value, получено {pair!r}", file=sys.stderr)
                return 2
            key, _, value = pair.partition("=")
            if value in ("true", "false"):
                kwargs[key] = value == "true"
            else:
                kwargs[key] = value
        report = plugin.run(**kwargs)
        print(report.summary())
        for err in report.errors:
            print(f"  ⚠ {err}")
        for f in report.findings[:20]:
            print("   " + json.dumps(f, ensure_ascii=False)[:300])
    return 0


def cmd_tc(cfg: Config, args: argparse.Namespace) -> int:
    try:
        client = _tc(cfg)
    except ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    try:
        if args.action == "login":
            cookie = client.login()
            print(f"Авторизация успешна. Кука сессии: "
                  f"{cookie.split('=')[0]}=***")
            return 0
        if args.action == "pull":
            with _store(cfg) as st:
                result = pull_item(st, client, args.item_id, actor=args.actor)
                print(f"Документ #{result.document_id}: получено записей "
                      f"{result.staged}")
                if args.promote:
                    pr = promote_all(st, result.document_id, actor=args.actor,
                                     embedder=_embedder(cfg).embed_one)
                    c = pr.to_dict()["counts"]
                    print(f"  создано {c['created']}, пропущено {c['skipped']}")
            return 0
        if args.action == "push":
            with _store(cfg) as st:
                if args.external_id:
                    req = st.get_requirement_by_external(args.external_id)
                    if req is None:
                        print(f"Требование {args.external_id!r} не найдено",
                              file=sys.stderr)
                        return 1
                    ids = [int(req["id"])]
                else:
                    ids = [int(r["id"]) for r in st.list_requirements(
                        status="approved", node_code=args.node, limit=1000)]
                result = push_batch(st, cfg, client, ids, actor=args.actor,
                                    dry_run=not args.apply)
                print(f"{'ЗАПИСАНО' if args.apply else 'ПРОВЕРКА (dry-run)'}: "
                      f"успешно {len(result['written'])}, "
                      f"заблокировано {len(result['blocked'])}, "
                      f"ошибок {len(result['failed'])}")
                for plan in result["blocked"][:20]:
                    print(f"  ✗ {plan['external_id']}: "
                          f"{'; '.join(plan['reasons'])}")
                for fail in result["failed"]:
                    print(f"  ! требование #{fail['requirement_id']}: "
                          f"{fail['error']}")
                if not args.apply and result["blocked"]:
                    return 1
            return 0
    except TeamcenterError as exc:
        print(f"Teamcenter: {exc}", file=sys.stderr)
        return 2
    finally:
        client.logout()
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .api.server import serve
    return serve(cfg)


# --- разбор аргументов -----------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="saps",
        description="САПС — Система агентской поддержки сертификации",
        epilog="Типовой путь: saps init --rules → saps import ТЗ.docx "
               "--promote → saps agent classifier → saps suggestions")
    p.add_argument("-c", "--config", help="путь к JSON-конфигу")
    p.add_argument("--actor", default="cli",
                   help="кто выполняет действие (пишется в журнал)")
    sub = p.add_subparsers(dest="command")

    i = sub.add_parser("init", help="создать схему БД")
    i.add_argument("--rules", action="store_true",
                   help="сразу загрузить встроенные справочники АП")
    i.add_argument("--indexes", action="store_true",
                   help="построить векторные индексы")
    i.set_defaults(func=cmd_init)

    sub.add_parser("check", help="самопроверка окружения").set_defaults(
        func=cmd_check)

    imp = sub.add_parser("import", help="импорт Word/Excel")
    imp.add_argument("path")
    imp.add_argument("--promote", action="store_true",
                     help="сразу перенести распознанное в production")
    imp.add_argument("--owner", default="", help="ответственный по умолчанию")
    imp.add_argument("--node", default="", help="узел изделия по умолчанию")
    imp.add_argument("--force", action="store_true",
                     help="импортировать, даже если файл уже загружали")
    imp.set_defaults(func=cmd_import)

    stg = sub.add_parser("staging", help="сырые записи импорта")
    stg.add_argument("--doc", type=int, help="номер документа")
    stg.add_argument("--status", default="", help="new|promoted|duplicate|skipped")
    stg.set_defaults(func=cmd_staging)

    pr = sub.add_parser("promote", help="перенести записи в production")
    pr.add_argument("--doc", type=int)
    pr.add_argument("--ids", type=int, nargs="*")
    pr.add_argument("--owner", default="")
    pr.add_argument("--node", default="")
    pr.add_argument("--on-conflict", default="skip", choices=["skip", "update"])
    pr.set_defaults(func=cmd_promote)

    rl = sub.add_parser("rules", help="справочник авиационных правил")
    rl.add_argument("action", choices=["list", "load"])
    rl.add_argument("path", nargs="?", default="")
    rl.add_argument("--builtin", action="store_true")
    rl.add_argument("--ruleset", default="")
    rl.set_defaults(func=cmd_rules)

    idx = sub.add_parser("index", help="пересчитать эмбеддинги")
    idx.add_argument("--build-indexes", action="store_true")
    idx.set_defaults(func=cmd_index)

    ag = sub.add_parser("agent", help="запуск агента")
    ag.add_argument("name", choices=["editor", "classifier", "gap"])
    ag.add_argument("--owner", default="")
    ag.add_argument("--node", default="")
    ag.add_argument("--ruleset", default="")
    ag.add_argument("--rewrite", action="store_true",
                    help="редактор: предлагать переформулировку через LLM")
    ag.add_argument("--use-llm", action="store_true",
                    help="классификатор: уточнять выбор моделью")
    ag.add_argument("-v", "--verbose", action="store_true")
    ag.set_defaults(func=cmd_agent)

    sg = sub.add_parser("suggestions", help="предложения агентов")
    sg.add_argument("--status", default="pending")
    sg.add_argument("--agent", default="")
    sg.add_argument("--accept", type=int, nargs="*")
    sg.add_argument("--reject", type=int, nargs="*")
    sg.set_defaults(func=cmd_suggestions)

    rq = sub.add_parser("requirements", help="список требований")
    rq.add_argument("--owner", default="")
    rq.add_argument("--node", default="")
    rq.add_argument("--status", default="")
    rq.add_argument("--query", default="")
    rq.add_argument("--limit", type=int, default=100)
    rq.set_defaults(func=cmd_requirements)

    sh = sub.add_parser("show", help="карточка требования")
    sh.add_argument("external_id")
    sh.set_defaults(func=cmd_show)

    hl = sub.add_parser("health", help="индикатор здоровья сертификации")
    hl.add_argument("--node", default="")
    hl.add_argument("--owner", default="")
    hl.add_argument("--by-node", action="store_true")
    hl.set_defaults(func=cmd_health)

    ex = sub.add_parser("export", help="выгрузка")
    ex.add_argument("format", choices=["docx", "xlsx", "requirements"])
    ex.add_argument("--node", default="")
    ex.add_argument("--owner", default="")
    ex.add_argument("--out", default="")
    ex.set_defaults(func=cmd_export)

    pl = sub.add_parser("plugin", help="плагины")
    pl.add_argument("action", choices=["list", "run"])
    pl.add_argument("name", nargs="?", default="")
    pl.add_argument("--arg", action="append", metavar="KEY=VALUE")
    pl.set_defaults(func=cmd_plugin)

    tc = sub.add_parser("tc", help="работа с Teamcenter")
    tc.add_argument("action", choices=["login", "pull", "push"])
    tc.add_argument("item_id", nargs="?", default="")
    tc.add_argument("--external-id", default="",
                    help="push: конкретное требование")
    tc.add_argument("--node", default="")
    tc.add_argument("--promote", action="store_true")
    tc.add_argument("--apply", action="store_true",
                    help="push: реально записать (по умолчанию только проверка)")
    tc.set_defaults(func=cmd_tc)

    sub.add_parser("serve", help="веб-интерфейс и API").set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    try:
        return int(args.func(cfg, args))
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        return 130
    except (ConfigError, StoreError, RulesError, ParseError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
