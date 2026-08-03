"""Read-only bridge from the business workbench to the local Codex app server.

This module deliberately does not contain business rules and does not open the
formal database.  It is a model-channel adapter only.  The normal session is
read-only; when the workbench explicitly supplies a small local tool set, the
tool handler remains responsible for all business validation and formal writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from scripts.core.model_gateway.goal07_model_gateway import (
    ModelProviderResult,
    ModelRequest,
    ModelRoute,
    ModelUsage,
)


class CodexAppServerProviderError(RuntimeError):
    """A closed, non-retrying failure at the local app-server boundary."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


@dataclass(frozen=True)
class CodexDynamicTool:
    """One explicitly allowed business tool exposed to the model session."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def protocol_spec(self) -> dict[str, Any]:
        if not self.name.strip():
            raise CodexAppServerProviderError("dynamic tool name is required")
        if not self.description.strip():
            raise CodexAppServerProviderError(
                f"dynamic tool {self.name!r} needs a description"
            )
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


DynamicToolHandler = Callable[
    [str, dict[str, Any], dict[str, Any]],
    dict[str, Any],
]


@dataclass(frozen=True)
class CodexAppServerProviderConfig:
    """Explicit settings needed to use the subscription-backed route."""

    executable: str
    model: str
    cwd: Path
    timeout_seconds: float = 120.0
    network_access: bool = False
    sandbox: str = "read-only"

    @classmethod
    def from_environment(
        cls,
        *,
        cwd: Path,
        environment: dict[str, str] | None = None,
        env_path: Path | None = None,
    ) -> "CodexAppServerProviderConfig":
        values = environment if environment is not None else os.environ
        dotenv = _load_dotenv(env_path)
        executable = _configured_value("CODEX_CLI_PATH", values, dotenv)
        model = _configured_value("CODEX_WORKBENCH_MODEL", values, dotenv)
        if not executable:
            raise CodexAppServerProviderError(
                "订阅路线未配置 CODEX_CLI_PATH；不会猜测或自动寻找另一个入口"
            )
        if not model:
            raise CodexAppServerProviderError(
                "订阅路线未配置 CODEX_WORKBENCH_MODEL；不会使用客户端默认模型"
            )
        timeout_text = _configured_value(
            "CODEX_WORKBENCH_TIMEOUT_SECONDS", values, dotenv
        ) or "120"
        try:
            timeout_seconds = float(timeout_text)
        except ValueError as exc:
            raise CodexAppServerProviderError(
                "CODEX_WORKBENCH_TIMEOUT_SECONDS 必须是数字"
            ) from exc
        if timeout_seconds <= 0 or timeout_seconds > 900:
            raise CodexAppServerProviderError(
                "CODEX_WORKBENCH_TIMEOUT_SECONDS 必须在 0 到 900 秒之间"
            )
        return cls(
            executable=executable,
            model=model,
            cwd=cwd.resolve(),
            timeout_seconds=timeout_seconds,
            network_access=False,
            sandbox="read-only",
        )


class CodexAppServerProviderAdapter:
    """Use one local app-server session for one isolated model request.

    A fresh process per request is intentional during validation.  It avoids
    carrying conversation state, approvals, hidden retries or tools from one
    business operation into another.  Conversation persistence can be added
    later only after the formal business boundary is defined.
    """

    provider_name = "codex_app_server"
    billing_mode = "chatgpt_subscription"

    def __init__(
        self,
        config: CodexAppServerProviderConfig,
        *,
        popen_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory or subprocess.Popen

    def complete(
        self,
        request: ModelRequest,
        route: ModelRoute,
        *,
        dynamic_tools: Iterable[CodexDynamicTool] = (),
        dynamic_tool_handler: DynamicToolHandler | None = None,
    ) -> ModelProviderResult:
        if route.provider_name != self.provider_name:
            raise CodexAppServerProviderError(
                f"route provider must be {self.provider_name}"
            )
        if not request.prompt.strip():
            raise CodexAppServerProviderError("prompt is required")
        if request.input_payload.get("allow_formal_write") is True:
            raise CodexAppServerProviderError(
                "订阅路线验证阶段禁止模型写入正式数据"
            )

        tool_specs = tuple(dynamic_tools)
        if tool_specs and dynamic_tool_handler is None:
            raise CodexAppServerProviderError(
                "dynamic tools require a local handler; the model cannot execute them by itself"
            )

        started = time.monotonic()
        process: Any | None = None
        try:
            process = self._start_process()
            client = _JsonLineRpcClient(
                process,
                timeout_seconds=self.config.timeout_seconds,
            )
            output, metadata = client.complete(
                prompt=request.prompt,
                cwd=self.config.cwd,
                model=self.config.model,
                sandbox=self.config.sandbox,
                network_access=self.config.network_access,
                dynamic_tools=tool_specs,
                dynamic_tool_handler=dynamic_tool_handler,
            )
        except CodexAppServerProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider boundary normalizes failures.
            raise CodexAppServerProviderError(
                f"订阅路线调用失败：{type(exc).__name__}: {exc}",
                diagnostics={"failure_type": type(exc).__name__},
            ) from exc
        finally:
            if process is not None:
                _terminate_process(process)

        if not output.strip():
            raise CodexAppServerProviderError("订阅路线返回了空内容")
        duration_ms = int(max(0.0, time.monotonic() - started) * 1000)
        return ModelProviderResult(
            output_text=output,
            usage=ModelUsage(),
            cost={"status": "not_reported", "billing_mode": self.billing_mode},
            provider_request_id=metadata.get("turn_id"),
            metadata={
                **metadata,
                "formal_data_written": False,
                "sandbox": self.config.sandbox,
                "network_access": self.config.network_access,
                "duration_ms": duration_ms,
            },
        )

    def _start_process(self) -> Any:
        executable = self.config.executable
        command_parts = _command_parts(executable)
        command_executable = command_parts[0]
        if not Path(command_executable).exists() and shutil.which(command_executable) is None:
            raise CodexAppServerProviderError(
                "订阅路线配置的 Codex 客户端不存在；不会改用系统中另一个程序"
            )
        command = command_parts + [
            "app-server",
            "--listen",
            "stdio://",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return self._popen_factory(
                command,
                cwd=str(self.config.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CodexAppServerProviderError(
                f"无法启动已配置的 Codex 客户端：{exc}"
            ) from exc


class _JsonLineRpcClient:
    def __init__(self, process: Any, *, timeout_seconds: float) -> None:
        self.process = process
        self.timeout_seconds = timeout_seconds
        self._next_id = 1
        self._stdout_queue: queue.Queue[Any] = queue.Queue()
        self._stdout_reader = threading.Thread(
            target=self._read_stdout_forever,
            name="codex-app-server-stdout",
            daemon=True,
        )
        self._stdout_reader.start()

    def complete(
        self,
        *,
        prompt: str,
        cwd: Path,
        model: str,
        sandbox: str,
        network_access: bool,
        dynamic_tools: Iterable[CodexDynamicTool] = (),
        dynamic_tool_handler: DynamicToolHandler | None = None,
    ) -> tuple[str, dict[str, Any]]:
        tool_specs = tuple(dynamic_tools)
        self._call(
            "initialize",
            {
                "clientInfo": {
                    "name": "creation-assistant-workbench",
                    "title": "Creation Assistant Workbench",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": bool(tool_specs)},
            },
        )
        self._notify("initialized", {})
        thread_response = self._call(
            "thread/start",
            {
                "cwd": str(cwd),
                "model": model,
                "approvalPolicy": "never",
                "sandbox": sandbox,
                "networkAccess": network_access,
                "ephemeral": True,
                "dynamicTools": [tool.protocol_spec() for tool in tool_specs] or None,
            },
        )
        thread = _nested_dict(thread_response, "result", "thread")
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            raise CodexAppServerProviderError(
                "订阅路线没有返回会话编号",
                diagnostics={"protocol_method": "thread/start"},
            )

        turn_response, collector = self._call_with_collector(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
            request_handler=dynamic_tool_handler,
        )
        turn = _nested_dict(turn_response, "result", "turn")
        turn_id = str(turn.get("id") or "").strip()
        self._drain_until_turn_complete(
            collector,
            request_handler=dynamic_tool_handler,
        )
        completed = collector.completed_status
        if completed and completed not in {"completed", "success", "succeeded"}:
            raise CodexAppServerProviderError(
                f"订阅路线任务未完成：{completed}",
                diagnostics={"turn_id": turn_id, "turn_status": completed},
            )
        output = collector.text.strip()
        return output, {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "protocol": "codex_app_server_stdio_jsonrpc",
            "completed_status": completed or "completed",
        }

    def _drain_until_turn_complete(
        self,
        collector: "_OutputCollector",
        *,
        request_handler: DynamicToolHandler | None = None,
    ) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        while collector.completed_status is None:
            message = self._read(deadline)
            if self._handle_server_request(message, request_handler):
                continue
            collector.accept(message)

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response, _ = self._call_with_collector(method, params)
        return response

    def _call_with_collector(
        self,
        method: str,
        params: dict[str, Any],
        *,
        request_handler: DynamicToolHandler | None = None,
    ) -> tuple[dict[str, Any], "_OutputCollector"]:
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        collector = _OutputCollector()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            message = self._read(deadline)
            if self._handle_server_request(message, request_handler):
                continue
            collector.accept(message)
            if message.get("id") != request_id:
                if message.get("method") in {"error", "turn/failed"}:
                    raise CodexAppServerProviderError(
                        f"订阅路线协议失败：{message.get('method')}",
                        diagnostics={"protocol_message": message},
                    )
                continue
            if "error" in message:
                raise CodexAppServerProviderError(
                    f"订阅路线请求失败：{message['error']}",
                    diagnostics={"protocol_method": method},
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerProviderError(
                    f"订阅路线协议返回格式错误：{method}",
                    diagnostics={"protocol_method": method},
                )
            return message, collector

    def _handle_server_request(
        self,
        message: dict[str, Any],
        request_handler: DynamicToolHandler | None,
    ) -> bool:
        if message.get("method") != "item/tool/call" or "id" not in message:
            return False
        params = message.get("params")
        if not isinstance(params, dict):
            self._write_tool_response(
                message["id"],
                success=False,
                text="工具请求格式不正确，未执行任何正式操作。",
            )
            return True
        if request_handler is None:
            self._write_tool_response(
                message["id"],
                success=False,
                text="当前工作台没有开启业务工具，未执行任何正式操作。",
            )
            return True
        arguments = params.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if not isinstance(arguments, dict):
            self._write_tool_response(
                message["id"],
                success=False,
                text="工具参数不是对象，未执行任何正式操作。",
            )
            return True
        try:
            result = request_handler(
                str(params.get("tool") or ""),
                arguments,
                params,
            )
            if not isinstance(result, dict):
                raise ValueError("tool handler must return an object")
            self._write_tool_response(
                message["id"],
                success=bool(result.get("success", True)),
                text=json.dumps(result, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:  # noqa: BLE001 - tool failures return to the model.
            self._write_tool_response(
                message["id"],
                success=False,
                text=json.dumps(
                    {
                        "success": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "formal_data_written": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        return True

    def _write_tool_response(
        self,
        request_id: Any,
        *,
        success: bool,
        text: str,
    ) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "contentItems": [{"type": "inputText", "text": text}],
                    "success": success,
                },
            }
        )

    def _read(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerProviderError("订阅路线等待超时；不会自动重试")
        try:
            line = self._stdout_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise CodexAppServerProviderError("订阅路线等待超时；不会自动重试") from exc
        if line is _STDOUT_EOF:
            stderr = getattr(self.process, "stderr", None)
            detail = ""
            if stderr is not None:
                try:
                    detail = str(stderr.read() or "")[-500:]
                except Exception:  # noqa: BLE001 - diagnostics must not mask the failure.
                    detail = ""
            raise CodexAppServerProviderError(
                "订阅路线进程提前结束" + (f"：{detail}" if detail else ""),
            )
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        try:
            message = json.loads(str(line))
        except json.JSONDecodeError as exc:
            raise CodexAppServerProviderError(
                "订阅路线返回了无法识别的协议消息"
            ) from exc
        if not isinstance(message, dict):
            raise CodexAppServerProviderError("订阅路线协议消息不是对象")
        return message

    def _read_stdout_forever(self) -> None:
        stdout = getattr(self.process, "stdout", None)
        if stdout is None:
            self._stdout_queue.put(_STDOUT_EOF)
            return
        try:
            while True:
                line = stdout.readline()
                if not line:
                    self._stdout_queue.put(_STDOUT_EOF)
                    return
                self._stdout_queue.put(line)
        except Exception:
            self._stdout_queue.put(_STDOUT_EOF)

    def _write(self, message: dict[str, Any]) -> None:
        stdin = getattr(self.process, "stdin", None)
        if stdin is None:
            raise CodexAppServerProviderError("订阅路线没有可写入的标准输入")
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        stdin.write(payload)
        stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})


class _OutputCollector:
    def __init__(self) -> None:
        self.delta_parts: list[str] = []
        self.completed_text: str | None = None
        self.completed_status: str | None = None

    @property
    def text(self) -> str:
        return self.completed_text or "".join(self.delta_parts)

    def accept(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            return
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                self.delta_parts.append(delta)
            return
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and str(item.get("type") or "") in {"agentMessage", "agent_message"}:
                final_text = "".join(_item_text(item)).strip()
                if final_text:
                    partial_text = "".join(self.delta_parts).strip()
                    if partial_text and partial_text.endswith(final_text):
                        self.completed_text = partial_text
                    else:
                        self.completed_text = final_text
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict):
                self.completed_status = str(turn.get("status") or "") or None


def _item_text(item: dict[str, Any]) -> Iterable[str]:
    text = item.get("text")
    if isinstance(text, str):
        yield text
        return
    content = item.get("content")
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            yield str(part["text"])


def _nested_dict(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _command_parts(executable: str) -> list[str]:
    parts = shlex.split(executable, posix=False)
    if not parts:
        raise CodexAppServerProviderError("订阅路线的客户端命令为空")
    return parts


def _load_dotenv(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _configured_value(
    key: str,
    environment: dict[str, str],
    dotenv: dict[str, str],
) -> str:
    process_value = str(environment.get(key) or "").strip()
    dotenv_value = str(dotenv.get(key) or "").strip()
    if process_value and dotenv_value and process_value != dotenv_value:
        raise CodexAppServerProviderError(
            f"订阅路线的 {key} 在进程和本地配置中不一致；不会猜选其中一个"
        )
    return process_value or dotenv_value


def _terminate_process(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    except Exception:
        # The provider result has already been decided.  Cleanup must not
        # replace the real model error with a process-cleanup error.
        pass


_STDOUT_EOF = object()
