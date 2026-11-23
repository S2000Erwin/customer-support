from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.types import AgentContext


ToolHandler = Callable[[dict[str, Any], AgentContext], Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._declarations: list[dict[str, Any]] = []

    def register(self, declaration: dict[str, Any], handler: ToolHandler) -> None:
        name = declaration["name"]
        self._handlers[name] = handler
        self._schemas[name] = declaration["parameters"]
        self._declarations.append(declaration)

    @property
    def declarations(self) -> list[dict[str, Any]]:
        return self._declarations

    async def execute(self, name: str, arguments: dict[str, Any], context: AgentContext) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"ok": False, "error": "Unknown tool."}
        schema = self._schemas[name]
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(arguments, dict) or any(key not in properties for key in arguments):
            return {"ok": False, "error": "Tool arguments contain unsupported fields."}
        missing = [key for key in required if key not in arguments]
        if missing:
            return {"ok": False, "error": f"Missing required argument: {missing[0]}"}
        type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}
        for key, value in arguments.items():
            expected = type_map.get(properties[key].get("type"))
            if expected and (not isinstance(value, expected) or (expected is int and isinstance(value, bool))):
                return {"ok": False, "error": f"Invalid type for argument: {key}"}
        try:
            return await handler(arguments, context)
        except (ValueError, KeyError) as exc:
            return {"ok": False, "error": str(exc)}
