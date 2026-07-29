"""Тесты коннектора Teamcenter: настоящий HTTP на фейковом SOA-сервере.

Поднимается локальный ThreadingHTTPServer, который отвечает так же, как
Teamcenter в примере заказчика: JSON-логин с Set-Cookie и XML-ответ
getItemAndRelatedObjects с полезной нагрузкой в CDATA. Это позволяет
проверить протокол целиком — формирование запроса, извлечение куки, её
передачу в Cookie, разбор ответа — не имея доступа к промышленному TC.

Отдельно и придирчиво проверяются ЗАЩИТЫ ЗАПИСИ: система не должна
писать в промышленный PDM ничего, что не прошло через человека.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness                                                    # noqa: E402
from harness import (check, check_raises, make_config, make_store,  # noqa: E402
                     section, skip_section, summary)
from saps.ingest.teamcenter import (TeamcenterClient, TeamcenterError,  # noqa: E402
                                    parse_item_response, tc_to_requirements)

ITEM_XML = """<?xml version="1.0" encoding="utf-8"?>
<ResponseEnvelope xmlns="http://teamcenter.com/Schemas/Soa/2006-09/ClientContext">
  <body>
    <bodystring><![CDATA[
    <GetItemAndRelatedObjectsResponse>
      <Item uid="AAAA1111" type="Item" item_id="АСДБ.04.32.8734.00.99"
            object_name="Блок управления">
        <props name="object_desc" value="Блок управления гидросистемой"/>
      </Item>
      <ItemRevision uid="BBBB2222" type="Requirement" item_id="REQ-501"
                    item_revision_id="A" object_name="Отказобезопасность">
        <props name="object_desc" value="Система должна сохранять работоспособность при единичном отказе"/>
        <props name="owning_user" value="ivanov"/>
      </ItemRevision>
      <ItemRevision uid="CCCC3333" type="Requirement" item_id="REQ-502"
                    object_name="Наработка">
        <props name="object_desc" value="Наработка на отказ не менее 10000 ч"/>
        <props name="owning_user" value="petrov"/>
      </ItemRevision>
    </GetItemAndRelatedObjectsResponse>
    ]]></bodystring>
  </body>
</ResponseEnvelope>"""

FAULT_XML = """<?xml version="1.0" encoding="utf-8"?>
<Envelope><Fault><faultstring>Object not found: НЕТ-ТАКОГО</faultstring></Fault></Envelope>"""


class FakeTC(BaseHTTPRequestHandler):
    """Имитация Teamcenter по схеме из PHP-примера заказчика."""

    cookie_name = "ASP.NET_SessionId"
    requests: list[dict] = []
    fail_login = False

    def log_message(self, *a):
        pass

    def do_POST(self):                                           # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        type(self).requests.append({
            "path": self.path, "body": body,
            "cookie": self.headers.get("Cookie", ""),
            "content_type": self.headers.get("Content-Type", ""),
        })

        if "Session/login" in self.path:
            if type(self).fail_login:
                # Реальный Teamcenter при неверном пароле отвечает 200 с
                # телом-ошибкой и БЕЗ куки — проверяем именно этот случай.
                self._send(200, b'{"error":"invalid credentials"}',
                           "application/json")
                return
            out = json.dumps({"serverInfo": {"Version": "13.2"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "TcCookieOther=zzz; Path=/")
            self.send_header("Set-Cookie",
                             f"{type(self).cookie_name}=sess123; Path=/; HttpOnly")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return

        if "getItemAndRelatedObjects" in self.path:
            if "НЕТ-ТАКОГО" in body:
                self._send(200, FAULT_XML.encode("utf-8"), "application/xml")
                return
            self._send(200, ITEM_XML.encode("utf-8"), "application/xml")
            return

        if "setProperties" in self.path:
            self._send(200, b"<Response><ok/></Response>", "application/xml")
            return

        self._send(404, b"not found", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    section("Разбор ответа Teamcenter (без сети)")
    obj = parse_item_response(ITEM_XML)
    check("корневой объект разобран", obj.item_id == "АСДБ.04.32.8734.00.99")
    check("uid извлечён", obj.uid == "AAAA1111")
    check("имя объекта", obj.name == "Блок управления")
    check("свойство из props", "гидросистемой" in obj.text())
    check("связанные объекты найдены", len(obj.related) == 2)
    check("тип связанного объекта",
          obj.related[0].type == "Requirement")
    check("ревизия разобрана", obj.related[0].revision == "A")
    check("владелец из owning_user", obj.related[0].owner() == "ivanov")

    check_raises("ошибка Teamcenter превращается в исключение",
                 TeamcenterError, parse_item_response, FAULT_XML)
    check_raises("пустой ответ", TeamcenterError, parse_item_response, "")
    check_raises("не XML", TeamcenterError, parse_item_response, "просто текст")
    check_raises("XML без объектов", TeamcenterError, parse_item_response,
                 "<Envelope><body/></Envelope>")

    section("Преобразование в требования")
    reqs = tc_to_requirements(obj)
    ids = {r.external_id for r in reqs}
    check("требования извлечены", {"REQ-501", "REQ-502"} <= ids, str(ids))
    r = next(x for x in reqs if x.external_id == "REQ-501")
    check("текст требования", "единичном отказе" in r.text)
    check("владелец перенесён", r.owner == "ivanov")
    check("uid сохранён в атрибутах", r.attributes["tc_uid"] == "BBBB2222")
    check("тип сохранён", r.attributes["tc_type"] == "Requirement")
    check("происхождение помечено", r.origin == "teamcenter")
    check("раздел указывает на изделие", "Teamcenter" in r.section_path)

    section("Протокол: логин и запрос (реальный HTTP)")
    FakeTC.requests = []
    FakeTC.fail_login = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTC)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/tc"

    client = TeamcenterClient(base, user="user", password="pass")
    cookie = client.login()
    check("кука сессии получена", cookie.startswith("ASP.NET_SessionId="),
          cookie)
    check("имя куки запомнено", client.session_name == "ASP.NET_SessionId")
    login_req = FakeTC.requests[0]
    check("логин ушёл на JsonRestServices",
          "JsonRestServices/Core-2011-06-Session/login" in login_req["path"])
    payload = json.loads(login_req["body"])
    check("структура тела логина как в примере заказчика",
          set(payload) == {"header", "body"}
          and "credentials" in payload["body"])
    check("поле descrimator присутствует (орфография API)",
          "descrimator" in payload["body"]["credentials"])
    check("пароль передан", payload["body"]["credentials"]["password"] == "pass")

    obj2 = client.fetch_item("АСДБ.04.32.8734.00.99")
    item_req = FakeTC.requests[1]
    check("запрос данных ушёл на RestServices",
          "RestServices/Core-2008-06-DataManagement/getItemAndRelatedObjects"
          in item_req["path"])
    check("кука передана в заголовке Cookie",
          "ASP.NET_SessionId=sess123" in item_req["cookie"], item_req["cookie"])
    check("тело — XML с CDATA",
          "<![CDATA[" in item_req["body"] and "GetItemAndRelatedObjectsInput"
          in item_req["body"])
    check("item_id подставлен",
          "АСДБ.04.32.8734.00.99" in item_req["body"])
    check("объект получен", obj2.item_id == "АСДБ.04.32.8734.00.99")

    section("Экранирование и ошибки")
    client.fetch_item('X&Y<Z>"')
    escaped = FakeTC.requests[-1]["body"]
    check("спецсимволы экранированы в XML",
          "&amp;" in escaped and "&lt;" in escaped and "X&Y" not in escaped)
    check_raises("объект не найден -> понятная ошибка", TeamcenterError,
                 client.fetch_item, "НЕТ-ТАКОГО")

    FakeTC.fail_login = True
    bad_client = TeamcenterClient(base, user="user", password="wrong")
    check_raises("отказ авторизации (200 без куки)", TeamcenterError,
                 bad_client.login)
    FakeTC.fail_login = False

    check_raises("пустой адрес отвергается", TeamcenterError,
                 TeamcenterClient, "")
    check_raises("логин без пользователя", TeamcenterError,
                 TeamcenterClient(base).login)
    dead = TeamcenterClient("http://127.0.0.1:1/tc", user="u", password="p",
                            timeout=1)
    check_raises("недоступный сервер -> понятная ошибка", TeamcenterError,
                 dead.login)

    section("Внешняя сессия (кука из чужого процесса)")
    external = TeamcenterClient(base)
    external.set_session("ASP.NET_SessionId=extern999")
    external.fetch_item("АСДБ.04.32.8734.00.99")
    check("переданная кука используется",
          "extern999" in FakeTC.requests[-1]["cookie"])

    if harness.server() is None:
        httpd.shutdown()
        skip_section("Синхронизация с базой", harness.SKIP_REASON)
        return summary("Teamcenter")

    from saps.ingest.pipeline import promote_all
    from saps.ingest.sync import SyncError, plan_push, pull_item, push_batch

    section("Импорт из Teamcenter в базу")
    st = make_store(dim=64)
    cfg = make_config(embedding_dim=64, tc_url=base, tc_user="u",
                      tc_password="p")
    result = pull_item(st, client, "АСДБ.04.32.8734.00.99", actor="engineer")
    check("документ создан", result.document_id > 0)
    check("записи попали в staging", result.staged == 2, str(result.staged))
    check("тип источника — teamcenter", result.kind == "teamcenter")
    pr = promote_all(st, result.document_id, actor="engineer")
    check("требования созданы", len(pr.created) == 2)
    req = st.get_requirement_by_external("REQ-501")
    check("требование в базе", req is not None)
    check("атрибуты Teamcenter сохранены",
          req["attributes"].get("tc_uid") == "BBBB2222", str(req["attributes"]))

    section("Защита записи в промышленный PDM")
    req_id = int(req["id"])
    plan = plan_push(st, cfg, req_id)
    check("запись запрещена: выключена в конфиге", plan.allowed is False)
    check("причина названа",
          any("выключена" in r for r in plan.reasons), str(plan.reasons))

    cfg.tc_write_enabled = True
    plan = plan_push(st, cfg, req_id)
    check("запись запрещена: статус не approved", plan.allowed is False)
    check("причина про статус",
          any("approved" in r for r in plan.reasons), str(plan.reasons))

    st.update_requirement(req_id, status="approved", actor="Иванов")
    st.update_requirement(req_id, text="Уточнённая формулировка требования",
                          actor="Иванов")
    sug = st.add_suggestion(req_id, "editor", text_after="ещё вариант")
    plan = plan_push(st, cfg, req_id)
    check("запись запрещена: висят предложения", plan.allowed is False)
    check("причина про предложения",
          any("предложения" in r for r in plan.reasons), str(plan.reasons))

    st.decide_suggestion(sug, "rejected", "Иванов")
    plan = plan_push(st, cfg, req_id)
    check("после решения по предложениям запись разрешена",
          plan.allowed is True, str(plan.reasons))
    check("в план попал текст требования",
          "Уточнённая" in plan.properties["object_desc"])
    check("в плане указан uid", plan.tc_uid == "BBBB2222")

    section("Запись: сначала dry-run, потом реальная отправка")
    before = len(FakeTC.requests)
    dry = push_batch(st, cfg, client, [req_id], actor="Иванов", dry_run=True)
    check("dry-run ничего не отправил",
          len(FakeTC.requests) == before and dry["written"] == [],
          "проверка не должна трогать промышленную систему")

    real = push_batch(st, cfg, client, [req_id], actor="Иванов", dry_run=False)
    check("запись выполнена", real["written"] == [req_id], str(real))
    sent = FakeTC.requests[-1]
    check("вызван setProperties", "setProperties" in sent["path"])
    check("передан uid объекта", "BBBB2222" in sent["body"])
    check("передан новый текст", "Уточнённая" in sent["body"])
    check("отметка синхронизации проставлена",
          st.get_requirement(req_id)["tc_synced_at"] is not None)
    audit = [a for a in st.audit(object_type="requirement", object_id=req_id)
             if a["action"] == "tc_write"]
    check("запись в журнале до отправки", len(audit) == 1)
    check("в журнале сохранены записанные свойства",
          "object_desc" in audit[0]["data"].get("properties", {}))

    section("Требование без tc_uid не пишется")
    local = st.create_requirement("REQ-LOCAL", "Локальное требование",
                                  status="approved")
    plan = plan_push(st, cfg, local)
    check("нет tc_uid — запись запрещена", plan.allowed is False)
    check("причина про tc_uid",
          any("tc_uid" in r for r in plan.reasons), str(plan.reasons))
    batch = push_batch(st, cfg, client, [local], dry_run=False)
    check("пакетная запись пропустила заблокированное",
          batch["written"] == [] and len(batch["blocked"]) == 1)

    st.close()
    httpd.shutdown()
    harness.cleanup()
    return summary("Teamcenter")


if __name__ == "__main__":
    raise SystemExit(main())
