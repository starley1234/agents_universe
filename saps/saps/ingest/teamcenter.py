"""Коннектор к Teamcenter SOA — режим А слоя импорта (ТЗ п.3.1).

СХЕМА РАБОТЫ ВЗЯТА ИЗ РАБОЧЕГО ПРИМЕРА ЗАКАЗЧИКА (PHP-контроллер) и
воспроизведена один в один, потому что она проверена на реальном
сервере, а документация Teamcenter по REST/SOA расходится от версии к
версии:

  1. POST {tc_url}/JsonRestServices/Core-2011-06-Session/login
     с телом {"header": {"state": {}, "policy": {}},
              "body": {"credentials": {...}}}.
     Из ответа берётся кука сессии. В примере это ASP.NET_SessionId —
     она появляется, когда перед Teamcenter стоит IIS; при других
     развёртываниях приходит JSESSIONID. Коннектор принимает ЛЮБУЮ из
     известных кук сессии и запоминает её имя, потому что жёстко зашитое
     'ASP.NET_SessionId' сломалось бы на первом же сервере под Tomcat.
  2. POST {tc_url}/RestServices/Core-2008-06-DataManagement/
     getItemAndRelatedObjects с XML-конвертом, где полезная нагрузка
     завёрнута в CDATA внутри <bodystring>. Кука передаётся в заголовке
     Cookie.
  3. Ответ — XML; из него извлекаются Item, ItemRevision и свойства.

ПОЧЕМУ ВСЁ НА urllib, А НЕ НА requests. Ноль зависимостей — принцип
репозитория, а в КБ это ещё и практический вопрос: чем меньше пакетов,
тем меньше согласований с безопасностью. Cookie-jar не нужен: кука одна
и её жизненный цикл мы контролируем явно.

ЗАПИСЬ ОБРАТНО (ТЗ п.5, Этап 3) намеренно ограничена:
  * выключена по умолчанию (`tc_write_enabled`);
  * пишет ТОЛЬКО требования со статусом approved — то есть прошедшие
    через человека;
  * каждая запись логируется в audit_log ДО отправки.
Причина простая: обратная запись меняет данные в промышленном PDM, и
ошибка здесь дороже, чем любая ошибка в самой САПС.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .word import ParsedRequirement, find_requirement_id

#: Куки сессии, которые встречаются у разных развёртываний Teamcenter.
#: ASP.NET_SessionId — вариант из примера заказчика (IIS перед TC).
SESSION_COOKIES = ("ASP.NET_SessionId", "ASP_NET_SessionId", "JSESSIONID",
                   "TcSessionId")

#: Свойства ItemRevision, в которых обычно лежит текст требования.
TEXT_PROPS = ("object_desc", "item_desc", "requirement_text", "description",
              "object_string", "l2_text")

#: Свойства, похожие на владельца/ответственного.
OWNER_PROPS = ("owning_user", "owner", "responsible_user", "user_id")


class TeamcenterError(RuntimeError):
    """Ожидаемая ошибка работы с Teamcenter: сеть, авторизация, формат."""


@dataclass
class TCObject:
    """Объект Teamcenter в нормализованном виде."""
    uid: str = ""
    type: str = ""
    item_id: str = ""
    name: str = ""
    revision: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    related: list["TCObject"] = field(default_factory=list)

    def text(self) -> str:
        for key in TEXT_PROPS:
            value = (self.properties.get(key) or "").strip()
            if value:
                return value
        return ""

    def owner(self) -> str:
        for key in OWNER_PROPS:
            value = (self.properties.get(key) or "").strip()
            if value:
                return value
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "type": self.type, "item_id": self.item_id,
                "name": self.name, "revision": self.revision,
                "properties": dict(self.properties),
                "related": [r.to_dict() for r in self.related]}


class TeamcenterClient:
    """Клиент SOA/REST Teamcenter. Сессия — кука, полученная при логине."""

    def __init__(self, base_url: str, *, user: str = "", password: str = "",
                 group: str = "", role: str = "", locale: str = "",
                 timeout: int = 60, opener: Any = None) -> None:
        if not base_url:
            raise TeamcenterError("Не задан адрес Teamcenter (tc_url)")
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.group = group
        self.role = role
        self.locale = locale
        self.timeout = timeout
        #: Позволяет подменить транспорт в тестах на локальный сервер,
        #: не трогая логику протокола.
        self.opener = opener or urllib.request.build_opener()
        self.session_cookie: str = ""      # "ИМЯ=значение"
        self.session_name: str = ""

    # --- транспорт --------------------------------------------------------
    def _request(self, url: str, data: bytes, content_type: str,
                 *, with_cookie: bool = True) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": content_type}
        if with_cookie and self.session_cookie:
            headers["Cookie"] = self.session_cookie
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
                head = {k.lower(): v for k, v in resp.headers.items()}
                # Set-Cookie может прийти несколько раз — берём все.
                head["__set_cookie__"] = "\n".join(
                    resp.headers.get_all("Set-Cookie") or [])
                return resp.status, head, raw
        except urllib.error.HTTPError as exc:
            body = exc.read()[:2000].decode("utf-8", "replace")
            raise TeamcenterError(
                f"Teamcenter вернул HTTP {exc.code} на {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TeamcenterError(
                f"Не удалось соединиться с Teamcenter ({url}): {exc}") from exc

    # --- шаг 1: авторизация ------------------------------------------------
    def login(self) -> str:
        """Логин. Возвращает куку сессии, она же запоминается в клиенте."""
        if not self.user:
            raise TeamcenterError("Не задан пользователь Teamcenter (tc_user)")
        payload = {
            "header": {"state": {}, "policy": {}},
            "body": {"credentials": {
                "user": self.user,
                "password": self.password,
                "role": self.role,
                "descrimator": "",      # орфография из API Teamcenter, не опечатка
                "locale": self.locale,
                "group": self.group,
            }},
        }
        url = f"{self.base_url}/JsonRestServices/Core-2011-06-Session/login"
        status, headers, body = self._request(
            url, json.dumps(payload).encode("utf-8"),
            "application/json", with_cookie=False)

        cookie = _extract_session_cookie(headers.get("__set_cookie__", ""))
        if not cookie:
            # Бывает, что сервер отвечает 200 с телом-ошибкой: показываем
            # его целиком — гадать о причине отказа авторизации бесполезно.
            snippet = body[:500].decode("utf-8", "replace")
            raise TeamcenterError(
                "Teamcenter не вернул куку сессии "
                f"({', '.join(SESSION_COOKIES)}). Ответ: {snippet}")
        self.session_name, _, _ = cookie.partition("=")
        self.session_cookie = cookie
        return cookie

    def set_session(self, cookie: str) -> None:
        """Использовать уже полученную куку (например, из внешней сессии)."""
        self.session_cookie = cookie
        self.session_name = cookie.partition("=")[0]

    def ensure_session(self) -> None:
        if not self.session_cookie:
            self.login()

    def logout(self) -> None:
        if not self.session_cookie:
            return
        url = f"{self.base_url}/JsonRestServices/Core-2006-03-Session/logout"
        try:
            self._request(url, json.dumps({"header": {"state": {}, "policy": {}},
                                           "body": {}}).encode("utf-8"),
                          "application/json")
        except TeamcenterError:
            pass       # разлогин «на всякий случай» не должен ронять работу
        finally:
            self.session_cookie = ""
            self.session_name = ""

    # --- шаг 2: получение объекта -----------------------------------------
    def get_item_and_related_objects(self, item_id: str) -> str:
        """Сырой XML-ответ getItemAndRelatedObjects по item_id."""
        self.ensure_session()
        url = (f"{self.base_url}/RestServices/"
               "Core-2008-06-DataManagement/getItemAndRelatedObjects")
        xml = _build_item_request(item_id)
        status, _, body = self._request(url, xml.encode("utf-8"),
                                        "application/xml; charset=utf-8")
        return body.decode("utf-8", "replace")

    def fetch_item(self, item_id: str) -> TCObject:
        """Объект Teamcenter в нормализованном виде."""
        return parse_item_response(self.get_item_and_related_objects(item_id))

    def fetch_requirements(self, item_id: str) -> list[ParsedRequirement]:
        """Требования, привязанные к изделию, в формате слоя импорта."""
        obj = self.fetch_item(item_id)
        return tc_to_requirements(obj)

    # --- шаг 3: запись обратно (ТЗ п.5, Этап 3) ---------------------------
    def set_properties(self, uid: str, properties: dict[str, str]) -> str:
        """Записать свойства объекта обратно в Teamcenter.

        Вызывающий код ОБЯЗАН убедиться, что запись разрешена конфигом и
        что требование прошло через человека, — см. sync.py. Клиент лишь
        выполняет протокол и не принимает решений о допустимости записи.
        """
        self.ensure_session()
        url = (f"{self.base_url}/RestServices/"
               "Core-2007-01-DataManagement/setProperties")
        xml = _build_set_properties_request(uid, properties)
        _, _, body = self._request(url, xml.encode("utf-8"),
                                   "application/xml; charset=utf-8")
        return body.decode("utf-8", "replace")


# --- разбор ответов --------------------------------------------------------
def _extract_session_cookie(set_cookie_header: str) -> str:
    """Найти куку сессии среди всех Set-Cookie."""
    if not set_cookie_header:
        return ""
    for line in set_cookie_header.split("\n"):
        first = line.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name = first.split("=", 1)[0].strip()
        if name in SESSION_COOKIES:
            return first
    return ""


def _xml_escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def _build_item_request(item_id: str) -> str:
    """XML-конверт getItemAndRelatedObjects — как в примере заказчика."""
    safe = _xml_escape(item_id)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<RequestEnvelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" \
xmlns:xsd="http://www.w3.org/2001/XMLSchema" \
xmlns="http://teamcenter.com/Schemas/Soa/2006-09/ClientContext">
    <header>
    </header>
    <body>
        <bodystring><![CDATA[
        <GetItemAndRelatedObjectsInput \
xmlns="http://teamcenter.com/Schemas/Core/2008-06/DataManagement">
            <infos clientId="{safe}">
                <itemInfo clientId="{safe}" useIdFirst="1" uid="">
                    <ids name="item_id" value="{safe}" />
                </itemInfo>
                <revInfo clientId="{safe}" processing="All" useIdFirst="0" \
uid="" nRevs="2147483647" revisionRule="" />
                <datasetInfo clientId="" uid="">
                    <filter useNameFirst="0" processing="All" name="" />
                </datasetInfo>
                <bvrTypeNames>view</bvrTypeNames>
            </infos>
        </GetItemAndRelatedObjectsInput>
        ]]></bodystring>
    </body>
</RequestEnvelope>"""


def _build_set_properties_request(uid: str, properties: dict[str, str]) -> str:
    props = "".join(
        f'<props name="{_xml_escape(k)}" value="{_xml_escape(v)}" />'
        for k, v in properties.items())
    return f"""<?xml version="1.0" encoding="utf-8"?>
<RequestEnvelope xmlns="http://teamcenter.com/Schemas/Soa/2006-09/ClientContext">
    <header>
    </header>
    <body>
        <bodystring><![CDATA[
        <SetPropertiesInput \
xmlns="http://teamcenter.com/Schemas/Core/2007-01/DataManagement">
            <objects uid="{_xml_escape(uid)}">{props}</objects>
        </SetPropertiesInput>
        ]]></bodystring>
    </body>
</RequestEnvelope>"""


def _localname(tag: str) -> str:
    return tag.split("}")[-1]


def parse_item_response(xml_text: str) -> TCObject:
    """Разобрать XML-ответ Teamcenter в TCObject.

    Схема ответа отличается между версиями TC и настройками сервера,
    поэтому разбор устроен по СМЫСЛУ, а не по жёсткому пути: ищем
    элементы, похожие на объекты (есть uid/type), и собираем их
    свойства из атрибутов и вложенных <props>. Жёсткий XPath ломался бы
    при первом же обновлении сервера.
    """
    text = (xml_text or "").strip()
    if not text:
        raise TeamcenterError("Пустой ответ Teamcenter")
    # Полезная нагрузка часто завёрнута в CDATA внутри <bodystring>.
    inner = re.search(r"<bodystring><!\[CDATA\[(.*?)\]\]></bodystring>",
                      text, re.DOTALL)
    if inner:
        text = inner.group(1).strip()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TeamcenterError(
            f"Ответ Teamcenter не разбирается как XML: {exc}. "
            f"Начало ответа: {text[:300]}") from exc

    fault = _find_fault(root)
    if fault:
        raise TeamcenterError(f"Teamcenter вернул ошибку: {fault}")

    objects: list[TCObject] = []
    for el in root.iter():
        name = _localname(el.tag)
        if name in ("Item", "ItemRevision", "item", "itemRev", "object",
                    "output", "Requirement"):
            obj = _object_from_element(el)
            if obj.uid or obj.item_id or obj.properties:
                objects.append(obj)
    if not objects:
        # Ответ есть, но объектов нет — обычно значит «не найдено».
        raise TeamcenterError(
            "В ответе Teamcenter не найдено объектов Item/ItemRevision. "
            "Проверьте item_id и права пользователя.")

    root_obj = objects[0]
    root_obj.related = objects[1:]
    return root_obj


def _find_fault(root: ET.Element) -> str:
    for el in root.iter():
        name = _localname(el.tag).lower()
        if name in ("fault", "faultstring", "partialerrors", "errorvalues",
                    "exception"):
            message = (el.text or "").strip()
            if not message:
                message = el.get("message", "") or el.get("value", "")
            for child in el.iter():
                if not message:
                    message = (child.text or "").strip()
            if message:
                return message[:500]
    return ""


def _object_from_element(el: ET.Element) -> TCObject:
    props: dict[str, str] = {}
    for key, value in el.attrib.items():
        props[_localname(key)] = value
    for child in el:
        cname = _localname(child.tag)
        if cname in ("props", "property", "prop", "ids"):
            pname = child.get("name") or child.get("nameValue") or ""
            pvalue = child.get("value")
            if pvalue is None:
                pvalue = (child.text or "").strip()
            if pname:
                props[pname] = pvalue
        elif cname and child.text and child.text.strip() and len(child) == 0:
            props.setdefault(cname, child.text.strip())

    return TCObject(
        uid=props.get("uid", ""),
        type=props.get("type", "") or _localname(el.tag),
        item_id=props.get("item_id", "") or props.get("id", ""),
        name=props.get("object_name", "") or props.get("name", ""),
        revision=props.get("item_revision_id", "") or props.get("revision", ""),
        properties=props)


def is_requirement(obj: TCObject) -> bool:
    """Похож ли объект Teamcenter на требование.

    По типу («Requirement», «ReqSpec» и подобные) либо по опознаваемому
    номеру требования в item_id. Изделия, сборки и датасеты сюда не
    попадают: превращать сборку в требование — значит засорить
    сертификационный базис объектами, которые нечем подтверждать.
    """
    type_name = (obj.type or "").lower()
    if "requirement" in type_name or "reqspec" in type_name:
        return True
    return bool(find_requirement_id(obj.item_id or ""))


def tc_to_requirements(obj: TCObject) -> list[ParsedRequirement]:
    """TCObject -> кандидаты в требования для общего конвейера импорта.

    КОРНЕВОЙ ОБЪЕКТ ЗАПРОСА — ЭТО ИЗДЕЛИЕ, А НЕ ТРЕБОВАНИЕ (если только
    его тип не говорит обратного). Он становится УЗЛОМ, к которому
    привязываются найденные требования: так в базе появляется структура
    «узел -> требования», на которой держится индикатор здоровья
    сертификации по узлу (ТЗ п.3.3). Ранняя версия превращала изделие в
    требование с идентификатором вида АСДБ.04.32.8734.00.99 — объект,
    который нечем подтверждать и который портит статистику покрытия.
    """
    out: list[ParsedRequirement] = []
    counter = 0
    root_is_req = is_requirement(obj)
    #: Код узла для требований: item_id изделия из корня запроса.
    node_code = "" if root_is_req else (obj.item_id or "")

    for candidate in [obj, *obj.related]:
        text = candidate.text()
        if not text:
            continue
        if candidate is obj and not root_is_req:
            continue                     # изделие -> узел, а не требование
        if candidate is not obj and not is_requirement(candidate):
            continue
        counter += 1
        external = (candidate.item_id
                    or find_requirement_id(text)
                    or candidate.uid)
        req = ParsedRequirement(
            external_id=external.upper(),
            title=candidate.name,
            text=text,
            section_path=f"Teamcenter > {obj.item_id or obj.uid}",
            owner=candidate.owner(),
            node=node_code,
            origin="teamcenter",
            ord=counter,
            attributes={k: v for k, v in candidate.properties.items()
                        if k not in ("uid",)},
        )
        req.attributes["tc_uid"] = candidate.uid
        req.attributes["tc_type"] = candidate.type
        if candidate.revision:
            req.attributes["tc_revision"] = candidate.revision
        req.confidence = 0.9
        out.append(req)
    return out
