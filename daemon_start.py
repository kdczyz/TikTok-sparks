#!/usr/bin/env python3
"""
抖音登录控制台守护启动器（双 fork 精灵进程）。

用法（跑一次即可返回）:
    /Users/a1412/.workbuddy/binaries/python/envs/default/bin/python3 daemon_start.py

原理：双 fork + setsid 使进程完全脱离终端会话（挂到 launchd 名下），
     会话结束/终端关闭都不会被杀；内部 while 循环负责进程退出后 3 秒自动重启。
停止: kill $(cat output/daemon.pid)

修复「登录面板未出现」：
    沙箱代理端口会轮换，旧 daemon 继承的 HTTPS_PROXY 过期后 chromium 直连失败，
    页面白屏 / state=error。
    1) 每次拉起 server 前自动探测当前可用的本地代理（先试环境变量，再扫监听端口），
       把新代理写进子进程环境；
    2) 健康检查将「连续 N 次接口无响应」和「持续 state=error/白屏」都视为不健康，
       自动重启 server → 重新探测代理 → 自愈。
"""
import json
import os
import re
import signal
import sys
import time
import subprocess
import urllib.request

CWD = "/Users/a1412/Desktop/火花/douyin_qr_login"
PY = "/Users/a1412/.workbuddy/binaries/python/envs/default/bin/python3"
SCRIPT = os.path.join(CWD, "server_v3.py")
OUT = os.path.join(CWD, "output")
STATUS_URL = "http://localhost:8765/api/status"
PROBE_TARGET = "https://www.douyin.com/robots.txt"

if os.fork() > 0:
    sys.exit(0)          # 父退出
os.setsid()              # 脱离会话/进程组
if os.fork() > 0:
    sys.exit(0)          # 二次 fork，确保永不重获终端

os.chdir(CWD)
devnull = os.open(os.devnull, os.O_RDWR)
os.dup2(devnull, 0)
os.dup2(devnull, 1)
os.dup2(devnull, 2)

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "daemon.pid"), "w") as f:
    f.write(str(os.getpid()))

log = open(os.path.join(OUT, "server_v3.log"), "ab", buffering=0)
err = open(os.path.join(OUT, "server_v3.err.log"), "ab", buffering=0)


def _dlog(msg):
    try:
        log.write(f"[daemon {time.strftime('%H:%M:%S')}] {msg}\n".encode())
    except Exception:
        pass


def _test_proxy(proxy):
    """proxy 形如 http://127.0.0.1:PORT；能连通探测目标即视为可用"""
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
        req = urllib.request.Request(PROBE_TARGET,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=5) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError:
        return True   # 有 HTTP 响应就说明代理通
    except Exception:
        return False


def _listening_local_ports():
    """扫描本机监听中的回环端口（lsof），返回候选列表"""
    ports = []
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        for m in re.finditer(r"(?:127\.0\.0\.1|localhost|\*):(\d+)\s", out):
            p = int(m.group(1))
            if p in (8765, 22, 631) or p < 1024:
                continue
            ports.append(p)
    except Exception as e:
        _dlog(f"lsof scan failed: {e}")
    # 去重保序，优先大端口（沙箱代理通常是高位端口）
    seen, uniq = set(), []
    for p in sorted(set(ports), reverse=True):
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def find_live_proxy():
    """找到当前可用的本地代理，返回 URL 或 None"""
    # 1) 环境变量里的候选优先
    env_candidates = []
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        v = os.environ.get(k)
        if v and v not in env_candidates:
            env_candidates.append(v)
    for c in env_candidates:
        if _test_proxy(c):
            return c
    # 2) 扫描本机监听端口逐个试
    for p in _listening_local_ports()[:25]:
        cand = f"http://127.0.0.1:{p}"
        if cand in env_candidates:
            continue
        if _test_proxy(cand):
            return cand
    return None


def get_child_env():
    """每次拉起 server 前重新探测代理，写入子进程环境。
    返回 (env, proxy_url_or_None)，proxy 即本次子进程实际绑定的代理。"""
    env = dict(os.environ)
    proxy = find_live_proxy()
    if proxy:
        _dlog(f"live proxy found: {proxy}")
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[k] = proxy
    else:
        _dlog("no live proxy found, child runs direct")
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(k, None)
    try:
        with open(os.path.join(OUT, "current_proxy.txt"), "w") as f:
            f.write(proxy or "direct")
    except Exception:
        pass
    return env, proxy


def _status_state(timeout=5):
    """返回 (alive, state_str)；接口无响应时 alive=False"""
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=timeout) as r:
            body = r.read(4096)
            if r.status != 200 or b"state" not in body:
                return False, None
            try:
                st = json.loads(body.decode("utf-8", "ignore")).get("state")
            except Exception:
                st = None
            return True, st
    except Exception:
        return False, None


def healthy():
    """/api/status 正常返回且状态不是持续 error 视为健康"""
    alive, st = _status_state(5)
    return alive and st != STATE_ERROR


STATE_ERROR = "error"

def _kill_tree(proc):
    """杀掉 server 及其 chromium 子进程（整组），防止孤儿 chrome 占住 browser_profile"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


child = None
fail_n = 0
last_proxy = None   # 当前子进程 chromium 绑定的代理
# 关键修复：旧版在 spawn 后调用 child.wait() 会永久阻塞，
# 导致循环顶部的健康检查成为死代码（子进程存活期间永远走不到）。
# 改为单循环非阻塞轮询：每 10s 做一次「活着? + 健康检查」，坏了就杀，下轮重生。
while True:
    if child is None or child.poll() is not None:
        # 子进程不存在/已退出 → 用新探测的代理拉起
        fail_n = 0
        try:
            env, last_proxy = get_child_env()
            child = subprocess.Popen([PY, SCRIPT, "--port", "8765"],
                                     cwd=CWD, stdout=log, stderr=err,
                                     env=env, start_new_session=True)
            _dlog(f"server spawned pid={child.pid}, proxy={last_proxy}")
        except Exception as e:
            _dlog(f"spawn failed: {e}")
            child = None
        time.sleep(10)
        continue

    # 子进程存活 → 健康检查。坏信号：
    #   a) 接口连续 3 次无响应（浏览器卡死/假活），或
    #   b) 持续 state=error（初始化失败），或
    #   c) 绑定的代理已失效（沙箱代理端口轮换 → 页面白屏/导航失败，
    #      此时 /api/status 仍可能返回 success，必须直接探代理活性）
    alive, st = _status_state(5)
    proxy_ok = _test_proxy(last_proxy) if last_proxy else True
    bad = (not alive) or (st == STATE_ERROR) or (not proxy_ok)
    fail_n = fail_n + 1 if bad else 0
    if fail_n >= 3:
        _dlog(f"health check x3 bad (alive={alive}, state={st}, "
              f"proxy={last_proxy}, proxy_ok={proxy_ok}), restarting server")
        _kill_tree(child)
        child = None
        fail_n = 0
    time.sleep(10)
