"""Offline text/tool token accounting for the Cookie transport.

The pinned tokenizer is the same Gemma 3 vocabulary distributed by Google's
Gen AI SDK. Counts are local estimates, not a substitute for upstream billing
metadata; media, hidden model reasoning and private protocol overhead are not
observable. No prompt data or tokenizer downloads leave the process at runtime.
"""

import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Iterable

import sentencepiece as spm


TOKENIZER_NAME = "gemma3"
TOKENIZER_PATH = Path(__file__).resolve().parent / "tokenizer_data" / "gemma3.model"
TOKENIZER_SHA256 = "1299c11d7cf632ef3b4e11937501358ada021bbdf7c47638d13c0ee982f2e79c"
_processor = None
_processor_lock = threading.Lock()


def warm_tokenizer():
    """Load and verify the bundled vocabulary once, without any network access."""
    global _processor
    with _processor_lock:
        if _processor is None:
            data = TOKENIZER_PATH.read_bytes()
            if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
                raise RuntimeError("Local tokenizer checksum mismatch")
            _processor = spm.SentencePieceProcessor(model_proto=data)
    return _processor


def count_texts(texts: Iterable[str]) -> int:
    # Never normalize whitespace: count the actual model-facing strings.
    batch = [text for text in texts if isinstance(text, str) and text]
    if not batch:
        return 0
    tokens = warm_tokenizer().encode(batch, out_type=int, num_threads=1,
                                     add_bos=False, add_eos=False)
    return sum(len(row) for row in tokens)


def count_text(text: str) -> int:
    return count_texts([text])


def _json_texts(value: Any):
    """Include argument/result keys, values, arrays and JSON scalar values."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _json_texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_texts(child)
    else:
        yield json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _schema_texts(schema: Any):
    """Semantic schema text; accept standard maps and Studio's {key,value} list."""
    if not isinstance(schema, dict):
        return
    for key in ("title", "description", "format", "pattern"):
        if isinstance(schema.get(key), str):
            yield schema[key]
    for key in ("enum", "required"):
        if key in schema:
            yield from _json_texts(schema[key])
    for key in ("default", "example", "examples"):
        if key in schema:
            yield from _json_texts(schema[key])
    properties = schema.get("properties") or {}
    if isinstance(properties, list):
        properties = {item["key"]: item.get("value") for item in properties
                      if isinstance(item, dict) and isinstance(item.get("key"), str)}
    if isinstance(properties, dict):
        for name, child in properties.items():
            yield str(name)
            yield from _schema_texts(child)
    for key in ("items", "additionalProperties", "not"):
        yield from _schema_texts(schema.get(key))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for child in schema.get(key) or []:
            yield from _schema_texts(child)


def _function_texts(function: dict, field: str):
    if function.get("name"):
        yield str(function["name"])
    if field in function:
        yield from _json_texts(function[field])


def _part_texts(part: dict):
    if isinstance(part.get("text"), str):
        yield part["text"]
    for key, field in (("functionCall", "args"), ("functionResponse", "response")):
        value = part.get(key)
        if isinstance(value, dict) and value.get("name"):
            yield from _function_texts(value, field)
    for key, field in (("executableCode", "code"), ("codeExecutionResult", "output"),
                       ("audioTranscription", "text")):
        value = part.get(key)
        if isinstance(value, dict) and isinstance(value.get(field), str):
            yield value[field]


def _has_media(part: dict) -> bool:
    return any(isinstance(part.get(key), dict) and
               any(part[key].get(field) for field in fields)
               for key, fields in (("inlineData", ("data",)), ("fileData", ("fileUri",))))


def prompt_texts(body: dict) -> tuple[list[str], bool]:
    """Count the final outgoing payload, not unselected/disabled OpenAI tools."""
    variables = body.get("variables") or {}
    texts = []
    has_media = False
    contents = list(variables.get("contents") or [])
    if isinstance(variables.get("systemInstruction"), dict):
        contents.append(variables["systemInstruction"])
    for content in contents:
        for part in content.get("parts") or []:
            texts.extend(_part_texts(part))
            has_media = has_media or _has_media(part)
    for tool in variables.get("tools") or []:
        for declaration in tool.get("functionDeclarations") or []:
            for key in ("name", "description"):
                if isinstance(declaration.get(key), str):
                    texts.append(declaration[key])
            for key in ("parameters", "response"):
                texts.extend(_schema_texts(declaration.get(key)))
    return texts, has_media


def _token_value(metadata: dict, key: str):
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    return value if isinstance(value, int) and value >= 0 else None


class CookieUsageCounter:
    """Accumulate one attempt's semantic output, independent of wire chunking."""

    def __init__(self):
        self.fragments: list[tuple[str, list[str]]] = []
        self.last_kind = None
        self.functions: list[dict] = []
        self.output_media = False

    def observe(self, kind: str, value: Any):
        if kind in ("text", "thought"):
            if self.last_kind == kind:
                self.fragments[-1][1].append(value)
            else:
                self.fragments.append((kind, [value]))
            self.last_kind = kind
        elif kind == "function_call":
            self.functions.append({"name": value.get("name"), "args": value.get("args")})
            self.last_kind = None
        elif kind == "image":
            # Do not tokenize image Markdown, URLs or base64 as model text.
            self.output_media = True
            self.last_kind = None

    def resolve(self, body: dict, metadata: dict | None = None) -> dict:
        remote = metadata if isinstance(metadata, dict) else {}
        p = _token_value(remote, "promptTokenCount")
        c = _token_value(remote, "candidatesTokenCount")
        t = _token_value(remote, "totalTokenCount")
        r = _token_value(remote, "thoughtsTokenCount")
        cached = _token_value(remote, "cachedContentTokenCount")
        # Proto default zeroes are not evidence that a non-empty response was free.
        has_remote = any(value is not None and value > 0 for value in (p, c, t, r))
        if not has_remote:
            p = c = t = r = cached = None
        if c is not None:
            c += r or 0
        if p is None and t is not None and c is not None and t >= c:
            p = t - c
        if p is not None and t is not None and t >= p and c is None:
            c = t - p
        needs_local = p is None or c is None
        input_media = False
        if needs_local:
            inputs, input_media = prompt_texts(body)
            if p is None:
                p = count_texts(inputs)
            if c is None:
                output_texts = ["".join(chunks) for _, chunks in self.fragments]
                for function in self.functions:
                    output_texts.extend(_function_texts(function, "args"))
                c = count_texts(output_texts)
                if r is None:
                    r = count_texts("".join(chunks) for kind, chunks in self.fragments if kind == "thought")
        if t is not None and t >= p + c:
            # Upstream total may include undisclosed thinking/tool output tokens.
            c = t - p
        else:
            t = p + c
        usage = {"prompt_tokens": p, "completion_tokens": c, "total_tokens": t}
        if r:
            usage["completion_tokens_details"] = {"reasoning_tokens": r}
        if cached:
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        info = {"source": "mixed" if needs_local and has_remote else (
            "local_tokenizer" if needs_local else "upstream"), "estimated": needs_local}
        if needs_local:
            info.update(tokenizer=TOKENIZER_NAME, scope="text_and_tools",
                        media_tokens_included=False,
                        contains_media=input_media or self.output_media)
        return {"usage": usage, "usage_metadata": info}
