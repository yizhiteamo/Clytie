"""Chat Completions through host HTTP, assets and credential capabilities."""

import asyncio
import base64
import json
import re
from urllib.parse import urlsplit

from clytie.sdk import CredentialUnavailable
from clytie.sdk.types import ModelFeatures, ModelTurn, TokenUsage, ToolCall

MAX_BYTES = 20 * 1024 * 1024


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
        self.url = self.url.rstrip("/") + "/chat/completions"
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
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

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
            return self._decode(response)

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

    def _decode(self, response):
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
        usage = {}
        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
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
