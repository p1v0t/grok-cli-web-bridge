"""Ponte Grok: liga as conversas do grok.com às sessões locais do Grok CLI.

Usa o login já salvo em %USERPROFILE%\\.grok\\auth.json. Não grava esse
token em disco de novo. O comando `planejar` envia um relatório ao Grok web
quando o CLI pede o próximo passo. O site não aciona este computador.
"""

from __future__ import annotations

import json
import hashlib
from functools import wraps
from state_io import state_lock, atomic_json
import os
import re
import shutil
import subprocess

from local_http import LocalError, LocalHandler, LocalServer
import sys
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

HOST = "127.0.0.1"
PORT = int(os.environ.get("GROK_BRIDGE_PORT", "8765"))
GROK_HOME = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser().resolve()
AUTH_PATH = GROK_HOME / "auth.json"
SESSIONS_PATH = GROK_HOME / "sessions"
PONTE_HOME = Path(os.environ.get("GROK_BRIDGE_HOME", str(Path.home() / ".grok-bridge"))).expanduser().resolve()
IMPORTS_PATH = PONTE_HOME / "imports"
LINKS_PATH = PONTE_HOME / "links.json"
CANAIS_PATH = PONTE_HOME / "canais.json"
APP_DIR = Path(__file__).resolve().parent
STATIC_INDEX = APP_DIR / "static" / "index.html"
GROK_EXE_CANDIDATES = [
    GROK_HOME / "bin" / "grok.exe",
    GROK_HOME / "bin" / "grok",
]
WEB_ORIGIN = "https://grok.com"
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,80}$")
UI_TEXT_LIMIT = 8000
FILE_TEXT_LIMIT = 12000
FILE_TOTAL_LIMIT = 150000
PUSH_TOTAL_LIMIT = 50000
PLAN_LIMIT = 100000
KNOWLEDGE_PROMPT_LIMIT = 40000
SEED_SIDE_LIMIT = 30000
PLANNER_CONTRACT = (
    "PAPEL\n"
    "Você é o planejador no Grok web. O Grok CLI, neste computador, é quem executa. "
    "Você não edita arquivo, não roda comando e não aciona o CLI. "
    "Leia o conhecimento já registrado neste canal e o relatório novo. "
    "Mantenha as decisões anteriores. Não troque uma decisão sem dizer por quê. "
    "Devolva um único próximo passo, em português, com detalhe bastante para o "
    "CLI executar sem escolher no escuro: arquivos com caminho, a mudança e como "
    "verificar. Quando o objetivo estiver cumprido, comece a resposta com a "
    "palavra CONCLUIDO.\n\n"
)


def synchronized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with state_lock(PONTE_HOME):
            return function(*args, **kwargs)
    return wrapped


class PonteError(LocalError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(__import__("base64").urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return 0
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


def load_auth() -> dict:
    if not AUTH_PATH.is_file():
        raise PonteError(
            "Não achei o login do Grok CLI. Rode grok login e abra a Ponte de novo.",
            401,
        )
    try:
        raw = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PonteError("Não consegui ler o login do CLI.", 401) from None
    best = None
    best_exp = -1
    if isinstance(raw, dict):
        for entry in raw.values():
            if not isinstance(entry, dict) or not entry.get("key") or not entry.get("user_id"):
                continue
            exp = jwt_exp(str(entry["key"]))
            if exp >= best_exp:
                best = entry
                best_exp = exp
    if not best:
        raise PonteError("O login do CLI não tem um token utilizável. Rode grok login.", 401)
    if best_exp and best_exp < int(datetime.now(timezone.utc).timestamp()) + 30:
        raise PonteError(
            "O token do Grok CLI venceu. Abra o grok ou rode grok login, e tente de novo.",
            401,
        )
    return best


def auth_headers(auth: dict) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {auth['key']}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "x-userid": str(auth["user_id"]),
        "Accept": "application/json, text/event-stream",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
        "User-Agent": "grok-ponte",
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PonteError("Redirecionamento remoto rejeitado.", 502)


def grok_request(method: str, url: str, body: dict | None = None, timeout: int = 60) -> tuple[int, bytes]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "grok.com" or not parsed.path.startswith("/rest/app-chat/") or parsed.fragment:
        raise PonteError("Destino remoto não permitido.")
    auth = load_auth()
    headers = auth_headers(auth)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise PonteError("Resposta remota grande demais.", 502)
            return response.status, raw
    except HTTPError as exc:
        raise PonteError(f"Grok web respondeu HTTP {exc.code}.", exc.code if exc.code in (401, 403, 404, 429) else 502) from None
    except URLError:
        raise PonteError("Não foi possível conectar ao Grok web.", 502) from None


def require_id(value: str) -> str:
    if not ID_RE.fullmatch(value or ""):
        raise PonteError("Identificador inválido.")
    return value


def as_iso(value) -> str:
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, timezone.utc).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str):
        return value
    return ""


def text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        stripped = content.strip()
        if stripped[:1] in "[{":
            try:
                return text_of(json.loads(stripped))
            except json.JSONDecodeError:
                return content
        return content
    if isinstance(content, list):
        parts = [text_of(item) for item in content]
        return "\n".join(part for part in parts if part.strip())
    if isinstance(content, dict):
        for key in ("text", "content", "message", "output"):
            if key in content:
                found = text_of(content[key])
                if found.strip():
                    return found
        return ""
    return str(content)


def clip(text: str, limit: int) -> tuple[str, bool]:
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit].rstrip() + "\n…", True


def list_web(page_token: str = "", query: str = "") -> dict:
    params = {"pageSize": "40"}
    if page_token:
        params["pageToken"] = page_token
    _, raw = grok_request("GET", f"{WEB_ORIGIN}/rest/app-chat/conversations?{urlencode(params)}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise PonteError("Resposta inesperada ao listar conversas.", 502)
    needle = query.casefold()
    items = []
    for row in data.get("conversations") or []:
        if not isinstance(row, dict) or not row.get("conversationId"):
            continue
        title = (row.get("title") or "").strip() or "Sem título"
        if needle and needle not in title.casefold():
            continue
        items.append(
            {
                "id": row["conversationId"],
                "title": title,
                "created": row.get("createTime") or "",
                "updated": row.get("modifyTime") or "",
                "starred": bool(row.get("starred")),
            }
        )
    return {"items": items, "nextPageToken": data.get("nextPageToken") or ""}


def linearize(responses: list[dict]) -> list[dict]:
    by_id = {row["responseId"]: row for row in responses if row.get("responseId")}
    latest = None
    for row in responses:
        if row.get("message") and not row.get("isControl"):
            if latest is None or (row.get("createTime") or "") >= (latest.get("createTime") or ""):
                latest = row
    if latest is None:
        return []
    chain = []
    seen = set()
    node = latest
    while node and node.get("responseId") not in seen:
        seen.add(node["responseId"])
        chain.append(node)
        node = by_id.get(node.get("parentResponseId"))
    chain.reverse()
    return chain


def web_messages(conversation_id: str, per_message: int) -> dict:
    require_id(conversation_id)
    _, meta_raw = grok_request("GET", f"{WEB_ORIGIN}/rest/app-chat/conversations/{conversation_id}")
    meta = json.loads(meta_raw)
    _, raw = grok_request(
        "GET",
        f"{WEB_ORIGIN}/rest/app-chat/conversations/{conversation_id}/responses",
        timeout=90,
    )
    payload = json.loads(raw)
    rows = [row for row in payload.get("responses") or [] if isinstance(row, dict)]
    messages = []
    for row in linearize(rows):
        if row.get("isControl"):
            continue
        sender = str(row.get("sender") or "").lower()
        if sender == "human":
            role = "user"
        elif sender == "assistant":
            role = "assistant"
        else:
            continue
        text, truncated = clip(text_of(row.get("message")), per_message)
        if not text:
            continue
        messages.append(
            {
                "role": role,
                "text": text,
                "truncated": truncated,
                "at": row.get("createTime") or "",
            }
        )
    title = (meta.get("title") or "").strip() or "Sem título"
    return {
        "id": conversation_id,
        "title": title,
        "source": "web",
        "created": meta.get("createTime") or "",
        "updated": meta.get("modifyTime") or "",
        "url": f"{WEB_ORIGIN}/chat/{conversation_id}",
        "messages": messages,
    }


def iter_summaries():
    if not SESSIONS_PATH.is_dir():
        return
    for group in SESSIONS_PATH.iterdir():
        if not group.is_dir():
            continue
        try:
            children = list(group.iterdir())
        except OSError:
            continue
        for session_dir in children:
            summary = session_dir / "summary.json"
            if summary.is_file():
                yield summary


def read_summary(path: Path) -> dict | None:
    try:
        path.resolve().relative_to(SESSIONS_PATH.resolve())
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict) or not info.get("id"):
        return None
    title = (data.get("generated_title") or data.get("session_summary") or data.get("last_turn_summary") or "").strip()
    return {
        "id": str(info["id"]),
        "title": title or "Sem título",
        "cwd": info.get("cwd") or "",
        "created": as_iso(data.get("created_at")),
        "updated": as_iso(data.get("updated_at") or data.get("last_active_at")),
        "model": data.get("current_model_id") or "",
        "messages": data.get("num_chat_messages") or data.get("num_messages") or 0,
        "path": str(path.parent),
    }


def list_cli(query: str = "") -> dict:
    items = []
    needle = query.casefold()
    for summary_path in iter_summaries():
        item = read_summary(summary_path)
        if not item:
            continue
        haystack = f"{item['title']}\n{item['cwd']}".casefold()
        if needle and needle not in haystack:
            continue
        items.append(item)
    items.sort(key=lambda row: row.get("updated") or "", reverse=True)
    public = []
    for item in items:
        public.append({key: item[key] for key in ("id", "title", "cwd", "created", "updated", "model", "messages")})
    return {"items": public}


def find_cli(session_id: str) -> dict:
    require_id(session_id)
    for summary_path in iter_summaries():
        item = read_summary(summary_path)
        if item and item["id"] == session_id:
            return item
    raise PonteError("Sessão do CLI não encontrada.", 404)


def cli_messages(session_id: str, per_message: int, include_tools: bool) -> dict:
    item = find_cli(session_id)
    history = (Path(item["path"]) / "chat_history.jsonl").resolve()
    try:
        history.relative_to(SESSIONS_PATH.resolve())
    except ValueError:
        raise PonteError("Histórico fora da pasta de sessões.") from None
    messages = []
    tool_count = 0
    if history.is_file():
        for line in history.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = row.get("type")
            if kind == "tool_result":
                tool_count += 1
                if not include_tools:
                    continue
                role = "tool"
            elif kind in ("user", "assistant"):
                role = kind
            else:
                continue
            text, truncated = clip(text_of(row.get("content")), per_message)
            if not text:
                continue
            messages.append({"role": role, "text": text, "truncated": truncated, "at": ""})
    return {
        "id": item["id"],
        "title": item["title"],
        "source": "cli",
        "cwd": item["cwd"],
        "created": item["created"],
        "updated": item["updated"],
        "model": item["model"],
        "toolCount": tool_count,
        "messages": messages,
    }


def find_grok() -> str:
    for candidate in GROK_EXE_CANDIDATES:
        if candidate.is_file():
            if candidate.suffix.lower() in (".bat", ".cmd"):
                raise PonteError("Configure um executável nativo do Grok, não um script de shell.")
            return str(candidate)
    found = shutil.which("grok")
    if found:
        if Path(found).suffix.lower() in (".bat", ".cmd"):
            raise PonteError("Configure um executável nativo do Grok, não um script de shell.")
        return found
    raise PonteError("Não achei o executável do Grok CLI em ~/.grok/bin nem no PATH.")


@synchronized
def remember_link(record: dict) -> None:
    PONTE_HOME.mkdir(parents=True, exist_ok=True)
    links = []
    if LINKS_PATH.is_file():
        try:
            stored = json.loads(LINKS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, list):
                links = stored
        except (OSError, json.JSONDecodeError):
            links = []
    links.append(record)
    atomic_json(LINKS_PATH, links[-200:])


def render_markdown(title: str, source: str, meta_lines: list[str], messages: list[dict], total_limit: int) -> str:
    lines = [f"# {title}", "", f"Origem: {source}", *meta_lines, ""]
    used = 0
    omitted = 0
    labels = {"user": "Você", "assistant": "Grok", "tool": "Ferramenta"}
    for message in messages:
        text = message["text"].strip()
        if not text:
            continue
        block = f"## {labels.get(message['role'], message['role'])}\n\n{text}\n"
        if used + len(block) > total_limit:
            omitted += 1
            continue
        lines.append(block)
        used += len(block)
    if omitted:
        lines.append(f"\n_{omitted} mensagens ficaram de fora para caber no arquivo._\n")
    return "\n".join(lines).strip() + "\n"


def continue_in_cli(conversation_id: str, launch: bool) -> dict:
    detail = web_messages(conversation_id, FILE_TEXT_LIMIT)
    IMPORTS_PATH.mkdir(parents=True, exist_ok=True)
    target = IMPORTS_PATH / f"{conversation_id}.md"
    body = render_markdown(
        detail["title"],
        "Grok web",
        [
            f"Conversa: {conversation_id}",
            f"URL: {detail['url']}",
            f"Exportado: {now_iso()}",
        ],
        detail["messages"],
        FILE_TOTAL_LIMIT,
    )
    target.write_text(body, encoding="utf-8")
    prompt = (
        f"Continue a conversa do Grok web \"{detail['title']}\". "
        f"O transcript está em {target}. "
        "Leia esse arquivo, resuma em poucos tópicos o que já foi decidido e espere o próximo pedido. "
        "Não altere arquivos até eu pedir."
    )
    record = {
        "at": now_iso(),
        "direction": "web-to-cli",
        "webId": conversation_id,
        "title": detail["title"],
        "file": str(target),
        "launched": bool(launch),
    }
    pid = None
    if launch:
        grok = find_grok()
        process = subprocess.Popen(
            [grok, "--cwd", str(APP_DIR), prompt],
            cwd=str(APP_DIR),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        pid = process.pid
    remember_link(record)
    return {"file": str(target), "title": detail["title"], "pid": pid, "launched": bool(launch)}


def walk_conversation_id(value) -> str | None:
    if isinstance(value, dict):
        found = value.get("conversationId")
        if isinstance(found, str) and ID_RE.fullmatch(found):
            return found
        for child in value.values():
            hit = walk_conversation_id(child)
            if hit:
                return hit
    elif isinstance(value, list):
        for child in value:
            hit = walk_conversation_id(child)
            if hit:
                return hit
    return None


def conversation_body(message: str, *, temporary: bool, concise: bool, parent: str | None = None) -> dict:
    body = {
        "temporary": temporary,
        "modelName": "grok-4",
        "message": message,
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": True,
        "enableImageGeneration": False,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": False,
        "imageGenerationCount": 1,
        "forceConcise": concise,
        "toolOverrides": {},
        "enableSideBySide": False,
        "sendFinalMetadata": True,
        "isReasoning": False,
        "webpageUrls": [],
        "disableTextFollowUps": True,
        "responseMetadata": {"requestModelDetails": {"modelId": "grok-4"}},
        "disableMemory": True,
        "forceSideBySide": False,
        "modelMode": "MODEL_MODE_AUTO",
        "isAsyncChat": False,
    }
    if parent:
        body["parentResponseId"] = parent
    return body


def parse_model_stream(raw: bytes) -> tuple[str | None, str]:
    conversation_id = None
    parts: list[str] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        conversation_id = walk_conversation_id(obj) or conversation_id
        if isinstance(obj, dict):
            token = ((obj.get("result") or {}).get("response") or {}).get("token")
            if isinstance(token, str) and token:
                parts.append(token)
    return conversation_id, "".join(parts)


def assert_safe_outbound(text: str) -> None:
    # This is a tripwire, not a substitute for reviewing the complete payload.
    patterns = (
        r"(?:gh[pousr]_|github_pat_|xai-|sk-(?:proj-)?)[A-Za-z0-9_-]{20,}",
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"AKIA[A-Z0-9]{16}",
        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
        r"(?i)(?:authorization|password|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s]{8,}",
    )
    if any(re.search(pattern, text) for pattern in patterns):
        raise PonteError("Possível segredo no texto. Revise e remova-o antes do envio.")


def post_conversation(message: str, *, conversation_id: str | None, parent: str | None, temporary: bool, concise: bool) -> tuple[str | None, str]:
    assert_safe_outbound(message)
    if conversation_id:
        require_id(conversation_id)
        url = f"{WEB_ORIGIN}/rest/app-chat/conversations/{conversation_id}/responses"
    else:
        url = f"{WEB_ORIGIN}/rest/app-chat/conversations/new"
    _, raw = grok_request(
        "POST",
        url,
        conversation_body(message, temporary=temporary, concise=concise, parent=parent),
        timeout=180,
    )
    return parse_model_stream(raw)


def latest_assistant_reply(conversation_id: str) -> tuple[str, str | None]:
    _, raw = grok_request(
        "GET",
        f"{WEB_ORIGIN}/rest/app-chat/conversations/{conversation_id}/responses",
        timeout=90,
    )
    payload = json.loads(raw)
    rows = [row for row in payload.get("responses") or [] if isinstance(row, dict)]
    assistants = [
        row for row in rows
        if str(row.get("sender") or "").lower() == "assistant" and row.get("responseId")
    ]
    assistants.sort(key=lambda row: row.get("createTime") or "")
    if not assistants:
        return "", None
    last = assistants[-1]
    return text_of(last.get("message")), str(last["responseId"])


def export_preview(session_id: str) -> dict:
    detail = cli_messages(session_id, FILE_TEXT_LIMIT, include_tools=False)
    spoken = [message for message in detail["messages"] if message["role"] in ("user", "assistant")]
    if not spoken:
        raise PonteError("Essa sessão do CLI não tem falas para enviar.")
    transcript = render_markdown(
        detail["title"],
        "Grok CLI",
        [
            f"Sessão: {session_id}",
                f"Ferramentas omitidas: {detail.get('toolCount') or 0}",
        ],
        spoken,
        PUSH_TOTAL_LIMIT,
    )
    message = (
        "Esta mensagem veio da Ponte Grok, a partir de uma sessão local do Grok CLI. "
        "Resuma em poucos tópicos o que essa sessão decidiu e fique pronto para continuar daqui.\n\n"
        + transcript
    )
    assert_safe_outbound(message)
    return {"message": message, "hash": hashlib.sha256(message.encode()).hexdigest()}


def send_to_web(session_id: str, expected_hash: str) -> dict:
    preview = export_preview(session_id)
    if not expected_hash or expected_hash != preview["hash"]:
        raise PonteError("A sessão mudou. Revise a prévia completa antes de enviar.", 409)
    message = preview["message"]
    detail = find_cli(session_id)
    conversation_id, preview_text = post_conversation(
        message, conversation_id=None, parent=None, temporary=False, concise=True,
    )
    if not conversation_id:
        raise PonteError("O Grok web aceitou o envio, mas não devolveu o id da conversa.", 502)
    preview, _ = clip(preview_text, 600)
    url = f"{WEB_ORIGIN}/chat/{conversation_id}"
    remember_link(
        {
            "at": now_iso(),
            "direction": "cli-to-web",
            "webId": conversation_id,
            "cliId": session_id,
            "title": detail["title"],
            "url": url,
        }
    )
    return {"id": conversation_id, "url": url, "title": detail["title"], "preview": preview}


def fit_report(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    if len(cleaned) <= PLAN_LIMIT:
        return cleaned
    head = cleaned[:20000]
    tail = cleaned[-(PLAN_LIMIT - 22000):]
    return head + "\n\n[trecho do meio omitido por tamanho]\n\n" + tail


def load_channels() -> list[dict]:
    if not CANAIS_PATH.is_file():
        return []
    try:
        data = json.loads(CANAIS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("channels") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def save_channels(channels: list[dict]) -> None:
    PONTE_HOME.mkdir(parents=True, exist_ok=True)
    atomic_json(CANAIS_PATH, {"channels": channels})


def web_title(conversation_id: str) -> str:
    require_id(conversation_id)
    _, raw = grok_request("GET", f"{WEB_ORIGIN}/rest/app-chat/conversations/{conversation_id}")
    meta = json.loads(raw)
    if not isinstance(meta, dict):
        raise PonteError("Resposta inesperada ao ler a conversa da web.", 502)
    return (meta.get("title") or "").strip() or "Sem título"


def write_channel_marker(channel: dict) -> None:
    try:
        cli = find_cli(channel["cliSessionId"])
    except PonteError:
        return
    marker = (Path(cli["path"]) / "ponte-canal.json").resolve()
    try:
        marker.relative_to(SESSIONS_PATH.resolve())
    except ValueError:
        raise PonteError("Marcador fora da pasta de sessões.") from None
    atomic_json(marker, channel)


def clear_channel_marker(cli_session_id: str) -> None:
    try:
        cli = find_cli(cli_session_id)
    except PonteError:
        return
    marker = (Path(cli["path"]) / "ponte-canal.json").resolve()
    try:
        marker.relative_to(SESSIONS_PATH.resolve())
    except ValueError:
        raise PonteError("Marcador fora da pasta de sessões.") from None
    if marker.is_file():
        marker.unlink()


@synchronized
def open_channel(cli_session_id: str, web_conversation_id: str) -> dict:
    cli = find_cli(cli_session_id)
    title = web_title(web_conversation_id)
    channels = load_channels()
    now = now_iso()
    current = next((row for row in channels if row.get("cliSessionId") == cli["id"]), None)
    previous_web_id = str(current.get("webConversationId")) if current and current.get("webConversationId") else None
    if current:
        clear_channel_marker(cli["id"])
        current.update(
            {
                "cliTitle": cli["title"],
                "webConversationId": web_conversation_id,
                "webTitle": title,
                "updated": now,
            }
        )
        channel = current
    else:
        channel = {
            "id": str(uuid.uuid4()),
            "cliSessionId": cli["id"],
            "cliTitle": cli["title"],
            "webConversationId": web_conversation_id,
            "webTitle": title,
            "created": now,
            "updated": now,
        }
        channels.append(channel)
    save_channels(channels)
    write_channel_marker(channel)
    remember_channel_opened(channel, previous_web_id)
    return channel


@synchronized
def close_channel(channel_id: str) -> None:
    require_id(channel_id)
    channels = load_channels()
    found = next((row for row in channels if row.get("id") == channel_id), None)
    if not found:
        raise PonteError("Canal não encontrado.", 404)
    clear_channel_marker(str(found.get("cliSessionId") or ""))
    save_channels([row for row in channels if row.get("id") != channel_id])


def channel_for_session(session_id: str | None) -> dict:
    if not session_id or not ID_RE.fullmatch(session_id):
        raise PonteError(
            "Não identifiquei a sessão do CLI. Abra o canal na Ponte, com esta sessão e uma conversa da web."
        )
    matches = [row for row in load_channels() if row.get("cliSessionId") == session_id]
    if not matches:
        raise PonteError(
            "Esta sessão do CLI não tem canal. Na Ponte, escolha esta sessão e uma conversa da web e abra o canal."
        )
    return matches[-1]


def knowledge_path(session_id: str, conversation_id: str) -> Path:
    require_id(session_id)
    web_id = require_id(conversation_id)
    return PONTE_HOME / "canais" / session_id / web_id / "conhecimento.md"


@synchronized
def append_knowledge(session_id: str, conversation_id: str, title: str, text: str) -> Path:
    path = knowledge_path(session_id, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not path.is_file():
        header = f"# Conhecimento do canal\n\nSessão CLI: {session_id}\n"
    block = f"{header}\n\n## {title} — {now_iso()}\n\n{text.strip()}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block)
    return path


def knowledge_for_prompt(session_id: str, conversation_id: str) -> str:
    path = knowledge_path(session_id, conversation_id)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if len(text) <= KNOWLEDGE_PROMPT_LIMIT:
        return text
    return (
        "[o conhecimento anterior deste canal continua no arquivo local]\n\n"
        + text[-KNOWLEDGE_PROMPT_LIMIT:]
    )


def transcript_text(detail: dict, limit: int) -> str:
    labels = {"user": "Você", "assistant": "Grok", "tool": "Ferramenta"}
    lines = []
    used = 0
    for message in detail.get("messages") or []:
        text = (message.get("text") or "").strip()
        if not text:
            continue
        block = f"### {labels.get(message.get('role'), message.get('role'))}\n{text}\n"
        if used + len(block) > limit:
            lines.append("\n[resto do transcript omitido]\n")
            break
        lines.append(block)
        used += len(block)
    return "".join(lines).strip()


def remember_channel_opened(channel: dict, previous_web_id: str | None) -> None:
    session_id = str(channel["cliSessionId"])
    web_id = str(channel["webConversationId"])
    path = knowledge_path(session_id, web_id)
    same_web = path.is_file()
    if path.is_file() and (same_web or previous_web_id is None):
        if previous_web_id is None:
            append_knowledge(
                session_id,
                web_id,
                "Canal reaberto",
                f"CLI: {channel.get('cliTitle') or session_id}\nWeb: {channel.get('webTitle') or web_id}\n"
                "O conhecimento anterior deste canal continua acima.",
            )
        return
    parts = [f"CLI: {channel.get('cliTitle') or session_id}", f"Web: {channel.get('webTitle') or web_id}"]
    if not path.is_file():
        try:
            cli_detail = cli_messages(session_id, 4000, include_tools=False)
            cli_text = transcript_text(cli_detail, SEED_SIDE_LIMIT)
            if cli_text:
                parts.append("\n# Conversa já existente no CLI\n\n" + cli_text)
        except PonteError as exc:
            parts.append(f"\nConversa do CLI não copiada: {exc}")
    if not path.is_file() or not same_web:
        try:
            web_detail = web_messages(web_id, 4000)
            web_text = transcript_text(web_detail, SEED_SIDE_LIMIT)
            if web_text:
                parts.append("\n# Conversa já existente no Grok web\n\n" + web_text)
        except PonteError as exc:
            parts.append(f"\nConversa web não copiada: {exc}")
    title = "Canal aberto" if not path.is_file() else "Par web atualizado"
    append_knowledge(session_id, web_id, title, "\n".join(parts))


def planner_message(report: str, prior: str) -> str:
    parts = [PLANNER_CONTRACT]
    if prior.strip():
        parts.append("CONHECIMENTO JÁ REGISTRADO NESTE CANAL\n" + prior.strip() + "\n\n")
    parts.append("RELATÓRIO NOVO\n" + report.strip() + "\n")
    return fit_report("".join(parts))


@synchronized
def ask_planner(report: str, conversation_id: str, session_id: str | None = None) -> str:
    require_id(conversation_id)
    prior = knowledge_for_prompt(session_id, conversation_id) if session_id else ""
    knowledge_file = None
    if session_id:
        knowledge_file = append_knowledge(session_id, conversation_id, "Relatório do CLI", report)
    message = planner_message(report, prior)
    try:
        parent = latest_assistant_reply(conversation_id)[1]
    except PonteError as exc:
        if exc.status == 404:
            raise PonteError(
                "A conversa web deste canal não existe mais. Escolha outra na Ponte.",
                404,
            ) from exc
        raise
    try:
        _, answer = post_conversation(
            message,
            conversation_id=conversation_id,
            parent=parent,
            temporary=False,
            concise=False,
        )
    except PonteError as exc:
        if exc.status == 404:
            raise PonteError(
                "A conversa web deste canal não existe mais. Escolha outra na Ponte.",
                404,
            ) from exc
        raise
    if not answer.strip():
        answer, _ = latest_assistant_reply(conversation_id)
    if not answer.strip():
        raise PonteError("O planejador respondeu vazio.", 502)
    if session_id:
        knowledge_file = append_knowledge(session_id, conversation_id, "Plano do Grok web", answer.strip())
    print(f"Planejador: {WEB_ORIGIN}/chat/{conversation_id}", file=sys.stderr)
    if knowledge_file:
        print(f"Conhecimento: {knowledge_file}", file=sys.stderr)
    return answer.strip() + "\n"


def planejar_cli(argv: list[str]) -> int:
    if "--confirm-web-send" not in argv:
        raise PonteError("Revise o relatório e o conhecimento; use --confirm-web-send para autorizar o envio à web.")
    argv = [arg for arg in argv if arg != "--confirm-web-send"]
    situation_path = None
    canal_id = None
    session_id = None
    web_override = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--situacao", "--canal", "--sessao", "--conversa"):
            if index + 1 >= len(argv):
                raise PonteError(f"{arg} precisa de um valor.")
            value = argv[index + 1]
            if arg == "--situacao":
                situation_path = value
            elif arg == "--canal":
                canal_id = require_id(value)
            elif arg == "--sessao":
                session_id = require_id(value)
            else:
                web_override = require_id(value)
            index += 2
            continue
        if arg == "--novo":
            raise PonteError("O par do canal é escolhido na Ponte. Abra ou troque o canal por lá.")
        raise PonteError(f"Argumento desconhecido: {arg}")
    if situation_path:
        path = Path(situation_path)
        if not path.is_file():
            raise PonteError(f"Não achei o relatório: {path}")
        report = path.read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        raise PonteError(
            "Passe o relatório com --situacao arquivo.md ou pela entrada padrão."
        )
    else:
        report = sys.stdin.read()
    if not report.strip():
        raise PonteError("O relatório da situação está vazio.")
    channel = None
    if canal_id:
        channel = next((row for row in load_channels() if row.get("id") == canal_id), None)
        if not channel:
            raise PonteError("Canal não encontrado.", 404)
    elif web_override:
        conversation_id = web_override
    else:
        channel = channel_for_session(session_id or os.environ.get("GROK_SESSION_ID"))
    knowledge_session = None
    if channel:
        conversation_id = str(channel["webConversationId"])
        knowledge_session = str(channel.get("cliSessionId") or "") or None
        print(
            f"Canal: {channel.get('cliTitle') or channel.get('cliSessionId')} -> {channel.get('webTitle') or conversation_id}",
            file=sys.stderr,
        )
    answer = ask_planner(report, conversation_id, knowledge_session)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(answer)
    return 0


def status() -> dict:
    auth = load_auth()
    exp = jwt_exp(str(auth["key"]))
    expires = datetime.fromtimestamp(exp, timezone.utc).astimezone().isoformat(timespec="seconds") if exp else ""
    cli_count = sum(1 for _ in iter_summaries())
    return {
        "ok": True,
        "name": auth.get("first_name") or "",
        "expires": expires,
        "cliSessions": cli_count,
        "grok": find_grok() if any(path.is_file() for path in GROK_EXE_CANDIDATES) or shutil.which("grok") else "",
    }


class Handler(LocalHandler):
    server_version = "GrokPonte/1.0"







    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            self.guard(api=path.startswith("/api/"))
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            if path in ("/", "/index.html"):
                self.serve_index()
                return
            if path == "/api/status":
                self.send_json(status())
                return
            if path == "/api/web":
                self.send_json(list_web(query.get("pageToken", ""), query.get("q", "").strip()))
                return
            if path == "/api/cli":
                self.send_json(list_cli(query.get("q", "").strip()))
                return
            if path == "/api/canais":
                self.send_json({"items": load_channels()})
                return
            if path.startswith("/api/web/"):
                self.send_json(web_messages(path.removeprefix("/api/web/"), UI_TEXT_LIMIT))
                return
            if path.startswith("/api/cli/") and path.endswith("/export-preview"):
                session_id = path.removeprefix("/api/cli/").removesuffix("/export-preview")
                self.send_json(export_preview(session_id))
                return
            if path.startswith("/api/cli/"):
                self.send_json(cli_messages(path.removeprefix("/api/cli/"), UI_TEXT_LIMIT, include_tools=False))
                return
            self.send_json({"error": "Não encontrado."}, 404)
        except LocalError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001 - a UI local precisa de um erro legível
            self.send_json({"error": "Falha interna. Confira a configuração local."}, 500)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            self.guard(api=True, mutation=True)
            body = self.read_json()
            if body.get("confirm") is not True:
                raise PonteError("Confirme a ação na Ponte antes de continuar.")
            if parsed.path.startswith("/api/web/") and parsed.path.endswith("/to-cli"):
                conversation_id = parsed.path.removeprefix("/api/web/").removesuffix("/to-cli").strip("/")
                self.send_json(continue_in_cli(conversation_id, bool(body.get("launch", True))))
                return
            if parsed.path.startswith("/api/cli/") and parsed.path.endswith("/to-web"):
                session_id = parsed.path.removeprefix("/api/cli/").removesuffix("/to-web").strip("/")
                self.send_json(send_to_web(session_id, str(body.get("previewHash") or "")))
                return
            if parsed.path == "/api/canais":
                self.send_json(open_channel(str(body.get("cliId") or ""), str(body.get("webId") or "")))
                return
            if parsed.path.startswith("/api/canais/") and parsed.path.endswith("/fechar"):
                channel_id = parsed.path.removeprefix("/api/canais/").removesuffix("/fechar").strip("/")
                close_channel(channel_id)
                self.send_json({"ok": True})
                return
            self.send_json({"error": "Não encontrado."}, 404)
        except LocalError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": "Falha interna. Confira a configuração local."}, 500)



    def serve_index(self) -> None:
        self.serve_html(STATIC_INDEX)


def serve(open_browser: bool) -> None:
    try:
        server = LocalServer((HOST, PORT), Handler)
    except OSError:
        raise PonteError("Porta local ocupada. Feche a outra instância ou configure GROK_BRIDGE_PORT.") from None
    url = f"http://{HOST}:{server.server_port}"
    print(f"Ponte Grok em {url}. Ctrl+C encerra.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def check() -> int:
    info = status()
    web = list_web()
    cli = list_cli()
    print(json.dumps(
        {
            "ok": info["ok"],
            "expires": info["expires"],
            "cliSessions": len(cli["items"]),
            "webPage": len(web["items"]),
            "grokFound": bool(info["grok"]),
        },
        ensure_ascii=False,
    ))
    return 0


def main() -> int:
    if os.name != "nt":
        os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "planejar":
        try:
            return planejar_cli(sys.argv[2:])
        except PonteError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if "--check" in sys.argv:
        try:
            return check()
        except PonteError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    open_browser = "--no-browser" not in sys.argv
    try:
        serve(open_browser)
        return 0
    except PonteError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
