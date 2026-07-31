"""Политики безопасности и прав доступа к инструментам (SecurityPolicy)."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any


class ToolPolicyError(Exception):
    """Отказ в доступе к инструменту по политике безопасности (гранты/ограничения)."""


@dataclass
class SecurityPolicy:
    """Политика безопасности и разрешений для агента или рабочего процесса.

    Регулирует доступ к инструментам по имени, скилсам, опасности (dangerous)
    и сетевым правилам (защита от SSRF).
    """

    allow_tools: set[str] | None = None  # None = разрешены все
    deny_tools: set[str] = field(default_factory=set)
    allow_skills: set[str] | None = None  # None = разрешены все скилсы
    deny_skills: set[str] = field(default_factory=set)
    allow_dangerous: bool = False
    read_only: bool = False
    allow_network: bool = True
    allow_local_network: bool = False  # False = блокировать приватные/loopback IP (SSRF)

    def is_tool_allowed(self, tool: Any) -> bool:
        """Проверить, разрешён ли инструмент данной политикой."""
        # 1. Запрет по имени
        if tool.name in self.deny_tools:
            return False

        # 2. Белый список по имени
        if self.allow_tools is not None and tool.name not in self.allow_tools:
            return False

        # 3. Запрет по скилсам
        if any(skill in self.deny_skills for skill in getattr(tool, "skills", [])):
            return False

        # 4. Белый список по скилсам
        if self.allow_skills is not None:
            tool_skills = getattr(tool, "skills", [])
            if not any(sk in self.allow_skills for sk in tool_skills):
                return False

        # 5. Опасные действия (dangerous)
        if getattr(tool, "dangerous", False) and not self.allow_dangerous:
            return False

        # 6. Режим только для чтения
        if self.read_only:
            attrs = getattr(tool, "attributes", {})
            if not attrs.get("read_only", False):
                return False

        # 7. Запрет сети
        attrs = getattr(tool, "attributes", {})
        if attrs.get("requires_network", False) and not self.allow_network:
            return False

        return True

    def validate_call(self, tool: Any, arguments: dict[str, Any] | None = None) -> None:
        """Проверить вызов инструмента и выбросить ToolPolicyError при нарушении."""
        if not self.is_tool_allowed(tool):
            if getattr(tool, "dangerous", False) and not self.allow_dangerous:
                raise ToolPolicyError(
                    f"Инструмент {tool.name!r} помечен как dangerous=True, а политика "
                    "запрещает опасные операции (allow_dangerous=False)."
                )
            if self.read_only and not getattr(tool, "attributes", {}).get(
                "read_only", False
            ):
                raise ToolPolicyError(
                    f"Инструмент {tool.name!r} изменяет данные, что запрещено в режиме "
                    "read_only=True."
                )
            raise ToolPolicyError(
                f"Инструмент {tool.name!r} запрещён текущей политикой безопасности."
            )

        # Проверка сетевой безопасности для URL (защита от SSRF)
        if arguments and "url" in arguments:
            self.validate_url(str(arguments["url"]))

    def validate_url(self, url: str) -> None:
        """Проверить URL на SSRF (вызов внутренних IP-адресов)."""
        if self.allow_local_network or not url:
            return

        from urllib.parse import urlparse

        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return

        if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            raise ToolPolicyError(
                f"Запрещён доступ к локальному хосту {hostname!r} (защита от SSRF)"
            )

        try:
            for _, _, _, _, sockaddr in socket.getaddrinfo(
                hostname, None, proto=socket.IPPROTO_TCP
            ):
                ip_str = sockaddr[0]
                ip_obj = ipaddress.ip_address(ip_str)
                if (
                    ip_obj.is_private
                    or ip_obj.is_loopback
                    or ip_obj.is_link_local
                    or ip_obj.is_multicast
                    or ip_obj.is_reserved
                ):
                    raise ToolPolicyError(
                        f"Запрещён доступ к внутреннему IP {ip_str} хоста {hostname!r} (SSRF)"
                    )
        except (socket.gaierror, ValueError):
            # Если DNS не резолвится в тестах или офлайн — не блокируем здесь,
            # сетевое исключение возникнет при попытке подключения
            pass
