"""Chat Completions and Responses through host capabilities."""

import asyncio
import base64
import json
import re
from copy import deepcopy
from urllib.parse import urlsplit

from clytie.sdk import CredentialUnavailable
from clytie.sdk.types import ModelFeatures, ModelTurn, TokenUsage, ToolCall

MAX_BYTES = 20 * 1024 * 1024


def cached_input(usage, details_key):
    details = usage.get(details_key)
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    return value if type(value) is int and value >= 0 else None


class OpenAIModel:
    def __init__(self, ctx):
        self.ctx = ctx
        config = ctx.config
        self.model, self.ref, self.url = (
            config.get(k) for k in ("model", "api_key_ref", "base_url")
        )
        if not all(isinstance(v, str) and v.strip() for v in (self.model, self.ref, self.url)):
            raise ValueError("model, api_key_ref and base_url are required")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid base_url")
        self.api = config.get("api", "chat_completions")
        self.image_detail = config.get("image_detail", "high")
        self.effort = config.get("reasoning_effort")
        if self.api not in {"chat_completions", "responses"}:
            raise ValueError("invalid api")
        if "reasoning_effort" in config and (
            not isinstance(self.effort, str) or not re.fullmatch(r"[a-z]{1,16}", self.effort)
        ):
            raise ValueError("invalid reasoning_effort")
        self.url = self.url.rstrip("/") + (
            "/responses" if self.api == "responses" else "/chat/completions"
        )
        self._reasoning = {}
        self._features = ModelFeatures(
            supports_tools=config.get("supports_tools", True),
            supports_images=config.get("supports_images", True),
            context_chars=config["context_chars"],
            agent_type=False,
        )

    def features(self):
        return self._features.model_copy(deep=True)

    async def quota(self):
        # Chat Completions has no reliable remaining-credit query.
        return "unknown"

    def _error(self, code, *, status=None, usage=None, provider_error=None):
        fields = {"error_code": code}
        if status is not None:
            fields["http_status"] = status
        if provider_error is not None:
            for key in ("code", "type", "param"):
                value = provider_error.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._\[\]-]{1,64}", value):
                    fields["openai_error_" + key] = value
        self.ctx.log.warning("model_backend_error", **fields)
        return ModelTurn(finish="error", usage=usage or {}, error={"code": code, "message": code})

    async def _body(self, req, cancel):
        messages = []
        for message in req.messages:
            cancel.raise_if_cancelled()
            parts = []
            for part in message.parts:
                if part.type == "text":
                    parts.append({"type": "text", "text": part.text})
                elif part.type == "image":
                    if (
                        not self._features.supports_images
                        or message.role != "user"
                        or part.asset is None
                    ):
                        raise ValueError("unsupported image")
                    info = await self.ctx.assets.info(part.asset.asset_id)
                    if info.media_type not in {
                        "image/png",
                        "image/jpeg",
                        "image/webp",
                        "image/gif",
                    }:
                        raise ValueError("unsupported image")
                    if info.size > MAX_BYTES:
                        raise ValueError("image too large")
                    content = await self.ctx.assets.read(part.asset.asset_id)
                    if len(content) > MAX_BYTES:
                        raise ValueError("image too large")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "detail": self.image_detail,
                                "url": "data:"
                                + info.media_type
                                + ";base64,"
                                + base64.b64encode(content).decode(),
                            },
                        }
                    )
                else:
                    raise ValueError("unsupported message part")
            content = (
                parts
                if any(p["type"] == "image_url" for p in parts)
                else "\n".join(p["text"] for p in parts)
            )
            item = {"role": message.role, "content": content}
            if message.tool_calls:
                if not self._features.supports_tools:
                    raise ValueError("tools disabled")
                item["tool_calls"] = [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {
                            "name": c.tool,
                            "arguments": json.dumps(
                                c.model_dump(mode="json")["arguments"], ensure_ascii=False
                            ),
                        },
                    }
                    for c in message.tool_calls
                ]
            if message.role == "tool":
                item["tool_call_id"] = message.tool_call_id
            messages.append(item)
        body = {"model": self.model, "messages": messages, "stream": False}
        if req.tools:
            if not self._features.supports_tools:
                raise ValueError("tools disabled")
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description
                        + "\n"
                        + t.when_not_to_use
                        + "\n"
                        + json.dumps(t.model_dump(mode="json")["examples"], ensure_ascii=False),
                        "parameters": t.model_dump(mode="json")["parameters"],
                    },
                }
                for t in req.tools
            ]
        if self.api == "responses":
            body = self._responses_body(body, req.trace_id)
        elif self.effort is not None:
            body["reasoning_effort"] = self.effort
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _expire_reasoning(self):
        now = self.ctx.clock.monotonic()
        for key, (created, _) in list(self._reasoning.items()):
            if now - created >= 600:
                del self._reasoning[key]

    def _responses_body(self, chat, trace_id):
        self._expire_reasoning()
        items = []
        for message in chat["messages"]:
            role, content = message["role"], message["content"]
            if role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message["tool_call_id"],
                        "output": content,
                    }
                )
                continue
            if role != "assistant" or content:
                if role == "user":
                    content = (
                        [{"type": "input_text", "text": content}]
                        if isinstance(content, str)
                        else [
                            {"type": "input_text", "text": p["text"]}
                            if p["type"] == "text"
                            else {
                                "type": "input_image",
                                "image_url": p["image_url"]["url"],
                                "detail": p["image_url"]["detail"],
                            }
                            for p in content
                        ]
                    )
                items.append(
                    {"role": "developer" if role == "system" else role, "content": content}
                )
            for call in message.get("tool_calls", []):
                cached = self._reasoning.pop((trace_id, call["id"]), None)
                if cached is not None:
                    items.extend(cached[1])
                items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
        body = {
            "model": self.model,
            "input": items,
            "stream": False,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if "tools" in chat:
            body["tools"] = [
                {"type": "function", **t["function"], "strict": False} for t in chat["tools"]
            ]
        if self.effort is not None:
            body["reasoning"] = {"effort": self.effort}
        return body

    async def generate(self, req, cancel):
        cancel.raise_if_cancelled()
        if req.budget.deadline <= self.ctx.clock.now():
            return ModelTurn(finish="deadline", usage={})

        async def operation():
            body = await self._body(req, cancel)
            key = await self.ctx.credentials.read(self.ref)
            cancel.raise_if_cancelled()
            remaining = (req.budget.deadline - self.ctx.clock.now()).total_seconds()
            if remaining <= 0:
                return ModelTurn(finish="deadline", usage={})
            response = await self.ctx.http.request(
                "POST",
                self.url,
                headers=dict(
                    [("Authorization", "Bearer " + key), ("Content-Type", "application/json")]
                ),
                body=body,
                timeout_s=remaining,
                max_bytes=MAX_BYTES,
            )
            return self._decode(response, trace_id=req.trace_id)

        task = asyncio.create_task(operation())
        cancelled = asyncio.create_task(cancel.wait())
        timer = asyncio.create_task(self.ctx.clock.sleep_until(req.budget.deadline))
        try:
            done, _ = await asyncio.wait(
                {task, cancelled, timer}, return_when=asyncio.FIRST_COMPLETED
            )
            cancel.raise_if_cancelled()
            if task not in done:
                return ModelTurn(finish="deadline", usage={})
            return task.result()
        except CredentialUnavailable:
            return self._error("auth_invalid")
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return self._error("invalid_request")
        except Exception:
            return self._error("transient")
        finally:
            for pending in (task, cancelled, timer):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, cancelled, timer, return_exceptions=True)

    def _decode(self, response, *, trace_id=None):
        if len(response.body) > MAX_BYTES:
            return self._error("invalid_response")
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if not 200 <= response.status < 300:
            error = data.get("error")
            error = error if isinstance(error, dict) else {}
            identifiers = (error.get("code"), error.get("type"))
            if response.status in {401, 403}:
                code = "auth_invalid"
            elif any(
                v in ("insufficient_quota", "billing_hard_limit_reached") for v in identifiers
            ):
                code = "quota_exhausted"
            elif response.status == 429:
                code = "rate_limited"
            elif response.status >= 500 or response.status in {408, 409}:
                code = "transient"
            else:
                code = "invalid_request"
            return self._error(code, status=response.status, provider_error=error)
        if self.api == "responses":
            return self._decode_responses(data, trace_id)
        usage = {}
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage["cached_input_tokens"] = cached_input(raw_usage, "prompt_tokens_details")
            for source, target in (
                ("prompt_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
            ):
                value = raw_usage.get(source)
                if type(value) is int and value >= 0:
                    usage[target] = value
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            if not isinstance(text, str):
                raise ValueError
            calls = []
            for call in message.get("tool_calls") or []:
                if call["type"] != "function" or not self._features.supports_tools:
                    raise ValueError
                arguments = json.loads(call["function"]["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError
                calls.append(
                    ToolCall(call_id=call["id"], tool=call["function"]["name"], arguments=arguments)
                )
            reason = choice["finish_reason"]
            if reason not in {"stop", "tool_calls"} or bool(calls) != (reason == "tool_calls"):
                return self._error("incomplete_response", usage=usage)
            return ModelTurn(text=text, tool_calls=calls, finish=reason, usage=TokenUsage(**usage))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return self._error("invalid_response", usage=usage)

    def _decode_responses(self, data, trace_id):
        usage = {}
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                key: raw_usage[key]
                for key in ("input_tokens", "output_tokens")
                if type(raw_usage.get(key)) is int and raw_usage[key] >= 0
            }
            usage["cached_input_tokens"] = cached_input(raw_usage, "input_tokens_details")
        if data.get("status") != "completed":
            return self._error("incomplete_response", usage=usage)
        try:
            text, calls, pending, staged = [], [], [], []
            if not isinstance(data["output"], list):
                raise ValueError
            for item in data["output"]:
                kind = item["type"]
                if kind == "reasoning":
                    pending.append(
                        deepcopy(
                            {
                                k: item[k]
                                for k in ("type", "id", "encrypted_content", "summary")
                                if k in item
                            }
                        )
                    )
                elif kind == "message":
                    for part in item["content"]:
                        if part["type"] == "refusal":
                            return self._error("incomplete_response", usage=usage)
                        if part["type"] != "output_text" or not isinstance(part["text"], str):
                            raise ValueError
                        text.append(part["text"])
                elif kind == "function_call":
                    if not self._features.supports_tools:
                        raise ValueError
                    arguments = json.loads(item["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError
                    calls.append(
                        ToolCall(call_id=item["call_id"], tool=item["name"], arguments=arguments)
                    )
                    if pending:
                        staged.append((item["call_id"], pending))
                        pending = []
                elif kind == "refusal":
                    return self._error("incomplete_response", usage=usage)
                else:
                    raise ValueError
            turn = ModelTurn(
                text="".join(text),
                tool_calls=calls,
                finish="tool_calls" if calls else "stop",
                usage=TokenUsage(**usage),
            )
            self._expire_reasoning()
            for call_id, reasoning in staged:
                key = (trace_id, call_id)
                self._reasoning.pop(key, None)
                self._reasoning[key] = (self.ctx.clock.monotonic(), reasoning)
                while len(self._reasoning) > 64:
                    del self._reasoning[next(iter(self._reasoning))]
            return turn
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return self._error("invalid_response", usage=usage)
