"""MCP 服务：/mcp 总览与重连 / 停用、信任项目 MCP、后台连接完成后接进智能体。

从 repl.py 拆出，作为 Repl 的 mixin：方法里的 self 就是 Repl，方法名与行为都没变。
"""

from __future__ import annotations

from rich.markup import escape
from rich.panel import Panel

from heng_agent.mcp import MCPManager
from heng_agent.mcp import trust as mcp_trust
from heng_agent.ui import mcp_view
from heng_agent.ui.render import (
    _PLAIN,
)
from heng_agent.ui.select import Option


class McpMixin:
    """MCP 服务：/mcp 总览与重连 / 停用、信任项目 MCP、后台连接完成后接进智能体。"""

    def ask_trust_mcp(self) -> None:
        """项目里配置的 MCP 服务等于「启动一个程序」：完整列出启动命令，用户确认后才启动。"""
        servers = self.untrusted_mcp
        lines = "\n".join(
            f"{s.name}：{s.url}（http）" if s.url else f"{s.name}：{s.command} {' '.join(s.args)}".rstrip()
            for s in servers
        )
        self.console.print(
            Panel(escape(lines), title="[bold]这个项目配置了 MCP 服务（会启动这些程序）[/]", border_style="red")
        )
        answer = self._ask("信任并启动这些 MCP 服务吗？不认识就选 n [y/n] > ").strip().lower()
        self.untrusted_mcp = []
        if answer not in ("y", "yes", "是"):
            self.console.print("本次不启动项目 MCP 服务。")
            return
        mcp_trust(self.agent.settings.home, self.agent.settings.workdir, servers)
        manager = MCPManager(servers, self.agent.settings.workdir, proxy=self.agent.settings.proxy)
        self.mcp_managers.append(manager)
        for tool in manager.tools:
            self.agent.tools[tool.name] = tool
        self.mcp_status += manager.status
        self.print_mcp_status(manager.status)  # 失败的服务在这里以红色显示原因
        self.console.print(
            f"已启动 {len(manager.clients)} 个 MCP 服务，接入 {len(manager.tools)} 个工具。配置一改会再问你。"
        )

    def mcp_command(self, rest: str) -> None:
        """/mcp 服务总览（方向键选服务 → 看工具 / 重连 / 停用）；/mcp 服务、/mcp 服务 工具 看工具与详情；
        /mcp reconnect|disable|enable 服务。"""
        parts = rest.split()
        if parts and parts[0] in mcp_view.ACTIONS:
            if len(parts) < 2:
                self.console.print(f"用法：/mcp {parts[0]} 服务名")
            else:
                self.mcp_action(parts[0], parts[1])
            return
        if len(parts) >= 2:
            mcp_view.show_tool(self, parts[0], parts[1])
            return
        if parts:
            mcp_view.show_server(self, parts[0])
            return
        infos = mcp_view.servers(self)
        if not infos:
            self.console.print("没有接入 MCP 服务。在 config.json 的 mcp_servers 里配置（见 README）。")
            return
        hint = "[dim]/mcp 服务名 看工具；/mcp reconnect | disable | enable 服务名[/]"
        if self._plain_input:  # 选择器用不了：纯文字列一遍
            mcp_view.show_summary(self)
            self.console.print(hint)
            return
        # 服务只列一次：状态、连接方式、工具数直接写在选择器的每一行里（以前先打一遍总览、再弹一遍选择器）
        self.console.print(mcp_view.headline(self, infos))
        width = max(len(s.name) for s in infos)
        options = [Option(s.name, mcp_view.option_label(s, width), detail=mcp_view.option_detail(s)) for s in infos]
        picked = self._choose("选一个服务：看工具、重连或停用（ESC 退出）", options)
        if picked is _PLAIN:
            mcp_view.show_summary(self)
            self.console.print(hint)
            return
        if not picked:
            return
        info = next(s for s in infos if s.name == picked)
        if info.ok:
            actions = [Option("tools", "看工具", "t"), Option("reconnect", "重新连接", "r", "服务崩了或刚更新过时用"),
                       Option("disable", "本会话停用", "d", "断开并移除它的工具")]  # fmt: skip
        elif info.disabled:
            actions = [Option("enable", "重新启用", "e")]
        else:
            actions = [Option("reconnect", "重新连接", "r"), Option("tools", "看失败原因", "t")]
        action = self._choose(f"{picked}：做什么？", actions)
        if action == "tools":
            mcp_view.show_server(self, str(picked))
        elif action in mcp_view.ACTIONS:
            self.mcp_action(str(action), str(picked))

    def mcp_action(self, action: str, name: str) -> None:
        """重连 / 停用 / 启用一个 MCP 服务，并把智能体的工具表同步过来（模型马上看得到新工具、看不到停用的）。"""
        c, a = self.console, self.agent
        manager = next((m for m in self.mcp_managers if m.has(name)), None)
        if manager is None:
            c.print(f"没有叫 {escape(name)} 的 MCP 服务。")
            return
        if name in mcp_view.connecting(self):  # 跳过了等待、后台还在连：这时重连会和后台连接撞在一起
            c.print(f"{escape(name)} 还在后台连接，连好后再试。")
            return
        if action == "disable":
            for tool_name in manager.disconnect(name):
                a.tools.pop(tool_name, None)
            self.mcp_disabled.add(name)
            self._set_mcp_status(name, False, "已停用（本会话）")
            c.print(f"[dim]○ 已停用 {escape(name)}（本会话；/mcp enable {escape(name)} 重新启用）[/]")
            return
        c.print(f"[dim]正在连接 {escape(name)}……[/]")
        removed, tools = manager.reconnect(name)
        for tool_name in removed:
            a.tools.pop(tool_name, None)
        for tool in tools:
            a.tools[tool.name] = tool
        self.mcp_disabled.discard(name)
        entry = next(e for e in manager.status if e[0] == name)
        self._set_mcp_status(*entry)
        self.print_mcp_status([entry])

    def _sync_mcp(self, wait: bool = True) -> None:
        """后台连接的 MCP 服务：连完后把工具接进智能体、打印连接结果。只在主线程、用户提交输入时调。

        不在后台线程里直接改 agent.tools：主线程每轮都在遍历它，同时改会出错。还没连完就等（模型这一轮可能要用），
        等的时候 Ctrl+C 跳过：这一句先不带这些工具，下次输入时再接。
        """
        # 按「哪些后台管理器还没接入」判断，不能看状态里有没有「连接中」：服务连得比建界面还快时，
        # 界面拿到的已经是连好的状态，看状态会以为不用接，工具就永远进不了智能体（测试里实际撞到过）
        managers = [m for m in self.mcp_managers if m.background and id(m) not in self._mcp_synced]
        if not managers:
            return
        shown_connecting = bool(mcp_view.connecting(self))  # 欢迎页显示的是「连接中」，连完要补打印结果
        pending = [name for m in managers for name in m.pending()]
        if pending and not wait:  # 显示输入框之前顺手接一下：还没连完就不等，交给状态栏显示进度
            return
        if pending:
            self._spin(f"等待 MCP 服务连接（{len(pending)} 个）…", "Ctrl+C 跳过")
            try:
                for manager in managers:
                    while not manager.wait(0.2):  # 分段等：Windows 上一直阻塞在 wait() 里收不到 Ctrl+C
                        pass
            except KeyboardInterrupt:
                self._unspin()
                self.console.print("[dim]已跳过等待：这一句先不带 MCP 工具，连好后下次输入时接入。[/]")
                return
            self._unspin()
        for manager in managers:
            for tool in manager.tools:
                self.agent.tools[tool.name] = tool
            for entry in manager.status:
                self._set_mcp_status(*entry)
            self._mcp_synced.add(id(manager))
        if shown_connecting:  # 欢迎页已经显示过结果的就不重复打印
            for line in mcp_view.startup_line(self):
                self.console.print(line)

    def _set_mcp_status(self, name: str, ok: bool, detail: str) -> None:
        for i, (old, _, _) in enumerate(self.mcp_status):
            if old == name:
                self.mcp_status[i] = (name, ok, detail)
                return
        self.mcp_status.append((name, ok, detail))

    def print_mcp_status(self, entries: list[tuple[str, bool, str]] | None = None) -> None:
        """每个 MCP 服务一行：MCP 服务 "名字" 已连接 · N tools (stdio / http)；失败的红色写原因。"""
        for name, ok, detail in entries if entries is not None else self.mcp_status:
            if ok:
                self.console.print(f'[green]MCP 服务 "{escape(name)}" 已连接 · {escape(detail)}[/]')
            else:
                self.console.print(f'[red]MCP 服务 "{escape(name)}" 连接失败：{escape(detail)}[/]')
