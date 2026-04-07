import json
import struct
import gzip
import time
import uuid
import os
import re
import asyncio
import hashlib
import queue
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse
from curl_cffi import requests
import uvicorn

app = FastAPI()
security = HTTPBearer()

# 配置项，支持环境变量覆盖
COOKIES_PATH = os.environ.get("COOKIES_PATH", "cookies.json")
PROXY = os.environ.get("HTTP_PROXY", None)  # 不设置则不走代理

# 同步阻塞调用用的线程池
_executor = ThreadPoolExecutor(max_workers=16)


def _load_cookies(path: str) -> dict:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cookies_list = json.load(f)
        return {c['name']: c['value'] for c in cookies_list}
    except Exception as e:
        print(f"Error loading cookies: {e}")
        return {}


def _generate_device_id(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    return str(int(h[:16], 16))[:19]


def _generate_session_id(seed: str) -> str:
    h = hashlib.sha256(("session-" + seed).encode()).hexdigest()
    return str(int(h[:16], 16))[:19]


def pack_connect_message(data: dict) -> bytes:
    payload = json.dumps(data, separators=(',', ':')).encode('utf-8')
    header = struct.pack('>BI', 0, len(payload))
    return header + payload


def _convert_citations(text: str) -> str:
    """将 Kimi 的 [^N^] 引用格式转换为 [N]"""
    return re.sub(r'\[\^(\d+)\^\]', r'[\1]', text)


def _format_references(refs: list) -> str:
    """将搜索引用格式化为 markdown 脚注"""
    if not refs:
        return ""
    lines = ["\n\n---", "**Sources:**"]
    for ref in refs:
        base = ref.get("base", {})
        title = base.get("title", "")
        url = base.get("url", "")
        ref_id = ref.get("id", "")
        if title and url:
            lines.append(f"[{ref_id}] [{title}]({url})")
    return "\n".join(lines) + "\n"


# ── 帧解析 ──────────────────────────────────────────────

def _parse_kimi_frames(buffer: bytes):
    """解析 connect 帧，返回 (events, remaining_buffer)。
    event 类型:
      - {"type": "text", "content": "..."}
      - {"type": "tool_status", "name": "...", "status": "..."}
      - {"type": "search_refs", "refs": [...]}
      - {"type": "done"}
    """
    events = []
    while len(buffer) >= 5:
        flag, length = struct.unpack_from('>BI', buffer, 0)
        if len(buffer) < 5 + length:
            break
        payload_bytes = buffer[5:5 + length]
        buffer = buffer[5 + length:]

        if flag == 2:
            try:
                payload_bytes = gzip.decompress(payload_bytes)
            except:
                pass

        if flag not in (0, 2):
            continue

        try:
            data = json.loads(payload_bytes.decode('utf-8'))
        except Exception as e:
            print(f"DEBUG: Error decoding frame JSON: {e}")
            continue

        # done 信号
        if "done" in data:
            events.append({"type": "done"})
            continue

        # heartbeat 跳过
        if "heartbeat" in data:
            continue

        op = data.get("op")
        if op not in ("set", "append"):
            continue

        # 文本内容
        if "block" in data and "text" in data["block"]:
            content = data["block"]["text"].get("content", "")
            if content:
                events.append({"type": "text", "content": content})

        # message.blocks 里的文本 — 只提取 assistant 角色的，跳过 user/system 回显
        if "message" in data and "blocks" in data.get("message", {}):
            msg_role = data["message"].get("role", "")
            if msg_role == "assistant":
                content = ""
                for block in data["message"]["blocks"]:
                    if "text" in block:
                        content += block["text"].get("content", "")
                if content:
                    events.append({"type": "text", "content": content})

        # 工具调用状态
        if "block" in data and "tool" in data["block"]:
            tool = data["block"]["tool"]
            name = tool.get("name", "")
            status = tool.get("status", "")
            if name and status:
                events.append({"type": "tool_status", "name": name, "status": status})

        # 搜索引用 (usedSearchChunks 优先)
        msg = data.get("message", {})
        refs = msg.get("refs", {})
        if "usedSearchChunks" in refs:
            events.append({"type": "search_refs", "refs": refs["usedSearchChunks"]})

    return events, buffer


# 硬编码的 API Key，匹配时使用 cookies.json 认证
API_KEY = "sk-sseworld-kimi"

# ── Kimi Bridge ─────────────────────────────────────────

class KimiBridge:
    def __init__(self):
        self.base_url = "https://www.kimi.com"

    def create_session(self, api_key: str):
        if api_key == API_KEY:
            cookies = _load_cookies(COOKIES_PATH)
            auth_token = cookies.get("kimi-auth", "")
            fingerprint_seed = "cookies-default"
        else:
            cookies = {}
            auth_token = api_key
            fingerprint_seed = api_key

        device_id = _generate_device_id(fingerprint_seed)
        session_id = _generate_session_id(fingerprint_seed)

        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "authorization": f"Bearer {auth_token}",
            "content-type": "application/connect+json",
            "connect-protocol-version": "1",
            "origin": "https://www.kimi.com",
            "referer": "https://www.kimi.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "x-language": "zh-CN",
            "x-msh-device-id": device_id,
            "x-msh-platform": "web",
            "x-msh-session-id": session_id,
            "x-msh-version": "1.0.0",
            "x-traffic-id": f"u{device_id[:20]}",
        }

        return requests.Session(
            headers=headers,
            cookies=cookies,
            impersonate="chrome124",
            proxy=PROXY,
        )


bridge = KimiBridge()


# ── OpenAI 格式化 ──────────────────────────────────────

def format_openai_stream_chunk(content: str, model: str, chat_id: str, *, role: str = None, finish_reason: str = None):
    delta = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


# ── 同步辅助函数 (线程池中执行) ────────────────────────

def _sync_kimi_request(session, url, body_bytes):
    return session.post(url, data=body_bytes, stream=True, timeout=30)


def _sync_read_all(response):
    """同步读取完整响应，返回 (full_text, search_refs)"""
    full_content = ""
    search_refs = []
    buffer = b""
    for chunk in response.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += chunk
        events, buffer = _parse_kimi_frames(buffer)
        for ev in events:
            if ev["type"] == "text":
                full_content += ev["content"]
            elif ev["type"] == "search_refs":
                search_refs = ev["refs"]
    full_content = _convert_citations(full_content)
    if search_refs:
        full_content += _format_references(search_refs)
    return full_content


# ── 路由 ────────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"DEBUG: Incoming request: {request.method} {request.url}")
    response = await call_next(request)
    print(f"DEBUG: Response status: {response.status_code}")
    return response


KIMI_MODELS = {
    "kimi-k2.5": {"scenario": "SCENARIO_K2D5", "thinking": False},
    "kimi-k2.5-thinking": {"scenario": "SCENARIO_K2D5", "thinking": True},
}
DEFAULT_MODEL = "kimi-k2.5"


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "moonshot"}
            for mid in KIMI_MODELS
        ]
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)):
    api_key = credentials.credentials
    print(f"DEBUG: chat_completions endpoint hit, key prefix: {api_key[:6]}...")
    print(f"DEBUG: Request headers: {dict(request.headers)}")

    session = bridge.create_session(api_key)

    try:
        body = await request.json()
    except Exception as e:
        print(f"DEBUG: Failed to parse request JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages = body.get("messages", [])
    model = body.get("model", "kimi-k2.5")
    stream = body.get("stream", False)
    model_config = KIMI_MODELS.get(model, KIMI_MODELS[DEFAULT_MODEL])

    print(f"DEBUG: Received request: model={model}, thinking={model_config['thinking']}, stream={stream}, messages_count={len(messages)}")

    if not messages:
        raise HTTPException(status_code=400, detail="Messages are required")

    # 构造 Kimi 的请求
    kimi_blocks = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        prefix = "User: " if role == "user" else "Assistant: "
        kimi_blocks.append({"message_id": "", "text": {"content": f"{prefix}{content}\n"}})

    kimi_payload = {
        "scenario": model_config["scenario"],
        "tools": [{"type": "TOOL_TYPE_SEARCH", "search": {}}],
        "message": {
            "role": "user",
            "blocks": kimi_blocks,
            "scenario": model_config["scenario"]
        },
        "options": {"thinking": model_config["thinking"]}
    }

    print(f"DEBUG: Kimi payload size: {len(json.dumps(kimi_payload))}")

    url = f"{bridge.base_url}/apiv2/kimi.gateway.chat.v1.ChatService/Chat"
    body_bytes = pack_connect_message(kimi_payload)

    print(f"DEBUG: Forwarding to Kimi: {url}")

    loop = asyncio.get_event_loop()

    try:
        response = await loop.run_in_executor(_executor, _sync_kimi_request, session, url, body_bytes)
        print(f"DEBUG: Kimi response status: {response.status_code}")
    except Exception as e:
        print(f"DEBUG: Request to Kimi failed: {e}")
        session.close()
        raise HTTPException(status_code=500, detail=f"Failed to connect to Kimi: {str(e)}")

    if response.status_code != 200:
        error_text = response.text
        print(f"DEBUG: Kimi error: {error_text}")
        session.close()
        raise HTTPException(status_code=response.status_code, detail=f"Kimi API error: {error_text}")

    chat_id = str(uuid.uuid4())

    if stream:
        async def generate():
            q = queue.Queue()
            sentinel = object()
            sent_role = False

            def _stream_worker():
                try:
                    buf = b""
                    search_refs = []
                    for chunk in response.iter_content(chunk_size=None):
                        if not chunk:
                            continue
                        buf += chunk
                        events, buf = _parse_kimi_frames(buf)
                        for ev in events:
                            if ev["type"] == "text":
                                q.put(("text", _convert_citations(ev["content"])))
                            elif ev["type"] == "tool_status" and ev["status"] == "STATUS_RUNNING":
                                q.put(("text", "\n\n> [Searching...]\n\n"))
                            elif ev["type"] == "search_refs":
                                search_refs = ev["refs"]
                    # 流结束，追加引用
                    if search_refs:
                        q.put(("text", _format_references(search_refs)))
                finally:
                    q.put(sentinel)
                    session.close()

            loop.run_in_executor(_executor, _stream_worker)

            while True:
                try:
                    item = await loop.run_in_executor(None, q.get, True, 0.5)
                except:
                    continue
                if item is sentinel:
                    break
                _, content = item
                if not sent_role:
                    yield format_openai_stream_chunk(content, model, chat_id, role="assistant")
                    sent_role = True
                else:
                    yield format_openai_stream_chunk(content, model, chat_id)

            # finish_reason: stop
            yield format_openai_stream_chunk("", model, chat_id, finish_reason="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        try:
            full_content = await loop.run_in_executor(_executor, _sync_read_all, response)
        finally:
            session.close()

        return {
            "id": chat_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": full_content},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("openai:app", host="0.0.0.0", port=8001, reload=False, log_level="debug")
