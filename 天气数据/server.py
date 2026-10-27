"""天气数据看板 · 本地服务入口。

用法：
    python server.py                # 起服务并后台同步
    python server.py --open         # 起服务并自动打开浏览器（start.bat 用的就是这个）
    python server.py --no-sync      # 只读已有归档，不联网
    python server.py --probe ZSPD   # 自检上游解析，不启动服务
    python server.py --init         # 建库并打印归档统计
    python server.py --backup       # 热备份数据库
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import api, config, db, source, sync, tzutil


# ---------------------------------------------------------------- 日志


# 日志文件的共享状态：轮转时要整体换 handle。stdout / stderr 两个 Tee 引用
# 同一个 dict 而不是各自的文件对象——否则一个换了新句柄，另一个还捏着旧的。
_LOG_MAX_BYTES = 2 * 1024 * 1024
_LOG_STATE: dict = {"path": None, "handle": None, "tick": 0}
_log_lock = threading.Lock()


class _Tee:
    """把 stdout 同时写到控制台和文件。

    start.bat 会把窗口留着，但用户可能截图不全、或者服务是后台起的，
    留一份 data/server.log 相当于黑匣子，出问题时直接看它。
    """

    def __init__(self, console, log_state: dict) -> None:
        self.console = console
        self.log = log_state

    def write(self, data):
        try:
            self.console.write(data)
        except Exception:
            pass
        handle = self.log.get("handle")
        if handle is None:
            return
        try:
            handle.write(data)
            self.log["tick"] += 1
            if self.log["tick"] % 512 == 0:  # 不必每行都 stat 一次文件大小
                _rotate_log_if_big(self.log)
        except Exception:
            pass

    def flush(self):
        try:
            self.console.flush()
        except Exception:
            pass
        handle = self.log.get("handle")
        if handle is not None:
            try:
                handle.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):
        return self.console.fileno()


def _rotate_log_if_big(state: dict) -> None:
    """超 2 MB 就原地重开。启动时那一次检查只救得了"正好重启"的实例，
    常年不重启的服务也得有上限。"""
    path = state.get("path")
    old = state.get("handle")
    if path is None or old is None:
        return
    with _log_lock:
        try:
            if path.stat().st_size <= _LOG_MAX_BYTES:
                return
            fresh = open(path, "w", encoding="utf-8", buffering=1)
            fresh.write(
                f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}"
                f" 超过 {_LOG_MAX_BYTES // 1024} KB，重新记录 =====\n"
            )
            state["handle"] = fresh
            try:
                old.close()
            except Exception:
                pass
        except Exception:
            pass


def setup_file_log() -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = config.DATA_DIR / "server.log"
        if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
            path.unlink()  # 超过 2 MB 就重来，避免无限增长
        handle = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    _LOG_STATE.update(path=path, handle=handle, tick=0)
    sys.stdout = _Tee(sys.stdout, _LOG_STATE)
    sys.stderr = _Tee(sys.stderr, _LOG_STATE)


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # 必须配 Content-Length，否则浏览器 keep-alive 会卡住
    server_version = "WeatherObsBoard/1.0"
    sys_version = ""

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._send_bytes(204, b"", "text/plain")

    # ------------------------------------------------------------ 分发

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()
        }

        if path.startswith("/api/"):
            body = self._read_json()
            status, payload = api.handle(method, path, query, body)
            if isinstance(payload, api.Raw):
                self._send_bytes(status, payload.text.encode("utf-8"), payload.content_type,
                                 extra_headers=payload.headers)
            else:
                self._send_json(status, payload)
            return

        self._serve_static(path)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------ 响应

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    cache_control: str = "no-store", extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    # ------------------------------------------------------------ 静态文件

    def _serve_static(self, path: str) -> None:
        if path in ("", "/", "/index.html", "/dashboard.html"):
            target = config.DASHBOARD_PATH
        elif path == "/favicon.ico":
            self._send_bytes(204, b"", "image/x-icon")
            return
        else:
            target = (config.WEB_DIR / path.lstrip("/")).resolve()
            try:
                target.relative_to(config.WEB_DIR.resolve())
            except ValueError:
                self._send_bytes(403, b"forbidden", "text/plain; charset=utf-8")
                return

        if not target.is_file():
            self._send_bytes(404, "页面不存在".encode("utf-8"), "text/plain; charset=utf-8")
            return

        content_type, _ = mimetypes.guess_type(str(target))
        content_type = content_type or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self._send_bytes(200, target.read_bytes(), content_type,
                         cache_control="no-cache, must-revalidate")

    # ------------------------------------------------------------ 日志

    def log_request(self, code="-", size="-") -> None:
        # 只记接口请求和错误，静态资源刷屏没意义
        if "/api/" in self.path or (isinstance(code, int) and code >= 400):
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path} -> {code}\n")

    def log_error(self, fmt, *args) -> None:
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------- 工具


def _is_our_service(host: str, port: int) -> bool:
    """这个端口上跑的到底是不是同一个看板。

    两个实例会共用同一个 weather.db、各自跑一个同步线程，sync_state 互相覆盖，
    表现成"某个城市一直不更新"或"数据忽新忽旧"。所以撞上自己就直接退出，
    而不是顺着往后找端口再起一个。
    """
    import json
    import urllib.request

    probe = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
    try:
        with urllib.request.urlopen(f"http://{probe}:{port}/api/health", timeout=1.5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return False
    data = payload.get("data") or {}
    return bool(payload.get("ok")) and "archive" in data and "sync" in data


class AlreadyRunning(Exception):
    """目标端口上已经跑着同一个看板。"""

    def __init__(self, port: int):
        super().__init__(f"already running on {port}")
        self.port = port


def pick_port(host: str, start: int, tries: int) -> int:
    """从 start 起找一个能绑上的端口。Windows 下不加 SO_REUSEADDR，
    否则会误判"能绑上"从而抢不到被监听中的端口。"""
    if _is_our_service(host, start):
        raise AlreadyRunning(start)
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"端口 {start}~{start + tries - 1} 都被占用了，请用 --port 换一个。")


def print_banner(port: int, worker_started: bool) -> None:
    lines = [
        "",
        "  天气数据看板 · 最高气温追踪",
        f"  Python     {sys.version.split()[0]}",
        f"  解释器     {sys.executable}",
        f"  工作目录   {os.getcwd()}",
        f"  本地地址   http://127.0.0.1:{port}/",
        f"  数据库     {config.DB_PATH}",
        f"  归档条数   {db.total_observation_count()}",
        f"  后台同步   {'已启动（每 ' + str(config.POLL_INTERVAL // 60) + ' 分钟一轮）' if worker_started else '未启动（--no-sync）'}",
    ]
    if tzutil.is_degraded():
        lines.append("  时区提示   未安装 tzdata，已用固定偏移兜底（中国城市不受影响）")
    lines += ["", "  按 Ctrl+C 停止", ""]
    print("\n".join(lines), flush=True)


def run_backup() -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = config.DATA_DIR / f"backup-{stamp}.db"
    db.hot_backup(target)
    print(f"已备份到 {target}")


def run_init() -> None:
    db.init_db()
    stats = db.obs_stats()
    print("数据库已就绪")
    print(f"  路径   {config.DB_PATH}")
    print(f"  站点   {[item['icao'] for item in db.list_stations(True)]}")
    print(f"  归档   {stats['count']} 条（最老 {stats['oldest']} / 最新 {stats['newest']}）")


# ---------------------------------------------------------------- 主流程


def main() -> None:
    parser = argparse.ArgumentParser(description="天气数据看板 · 本地服务")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-sync", action="store_true", help="只读已有归档，不联网")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--probe", metavar="ICAO", help="自检上游解析")
    parser.add_argument("--init", action="store_true", help="建库并打印统计")
    parser.add_argument("--backup", action="store_true", help="热备份数据库")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    setup_file_log()

    if args.probe:
        source.probe(args.probe)
        return

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("[boot] 初始化数据库…", flush=True)
    db.init_db()
    print(f"[boot] 数据库就绪，归档 {db.total_observation_count()} 条", flush=True)

    if args.init:
        run_init()
        return
    if args.backup:
        run_backup()
        return

    # 先把端口占住，再起同步线程：否则重复启动的第二个进程会先跑一轮同步，
    # 和正在运行的那个实例抢同一个库。
    try:
        port = pick_port(args.host, args.port, config.PORT_TRIES)
    except AlreadyRunning as exc:
        # 双击 start.bat 时最常见的情况：看板已经开着。这时候**直接把浏览器打开**，
        # 而不是只打印一行提示就退出 —— 否则用户看到的就是"双击没反应"。
        url = f"http://127.0.0.1:{exc.port}/"
        print(f"看板已经在运行：{url}", flush=True)
        print("（不重复启动：两个实例会共用同一份数据库并互相覆盖）", flush=True)
        if args.open:
            print("正在打开浏览器…", flush=True)
            try:
                webbrowser.open(url)
            except Exception as err:
                print(f"[warn] 自动打开浏览器失败（{err}），请手动访问上面的地址", flush=True)
        # 用 3 告诉 start.bat「已经有一个在跑」，别把它当成异常退出
        raise SystemExit(3)
    print(f"[boot] 绑定端口 {port}…", flush=True)

    worker_started = False
    if not args.no_sync:
        print("[boot] 启动同步线程…", flush=True)
        sync.start_worker()
        worker_started = True
        print("[boot] 同步线程已启动", flush=True)

    httpd = Server((args.host, port), Handler)
    print_banner(port, worker_started)

    if args.open:
        # 自动开浏览器失败绝不能拖死服务：没装默认浏览器、被安全软件拦都可能抛异常
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception as exc:
            print(f"[warn] 自动打开浏览器失败（{exc}），请手动访问上面的地址", flush=True)

    try:
        httpd.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n正在停止…", flush=True)
    finally:
        httpd.shutdown()
        httpd.server_close()
        try:
            from app.sync import get_worker

            worker = get_worker()
            if worker:
                worker.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
