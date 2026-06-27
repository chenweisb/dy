import argparse
import subprocess
import sys
import time
from pathlib import Path

import frida

PACKAGE_NAME = "com.ss.android.ugc.aweme"
SCRIPT_PATH = Path(__file__).with_name("ssl_bypass.js")


def on_message(message, data):
    if message["type"] == "send":
        print(f"[*] {message['payload']}")
    elif message["type"] == "error":
        print(f"[!] Frida error: {message.get('stack', message)}")
    else:
        print(message)


def adb_force_stop():
    try:
        subprocess.run(
            ["adb", "shell", "am", "force-stop", PACKAGE_NAME],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def load_script(session):
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    script = session.create_script(source)
    script.on("message", on_message)
    script.load()
    return script


def check_frida_server(device) -> int:
    """返回可见进程数；<10 通常表示 frida-server 未正常运行。"""
    try:
        procs = device.enumerate_processes()
        return len(procs)
    except Exception:
        return 0


def find_douyin_pid(device) -> int | None:
    """优先 attach 主进程 com.ss.android.ugc.aweme（非 :push 等子进程）。"""
    exact = None
    fallback = None
    for proc in device.enumerate_processes():
        name = proc.name or ""
        if name == PACKAGE_NAME:
            return proc.pid
        lower = name.lower()
        if lower in ("抖音", "douyin"):
            fallback = proc.pid
        elif PACKAGE_NAME in lower and ":" not in name:
            exact = proc.pid
        elif PACKAGE_NAME in lower and not fallback:
            fallback = proc.pid
    return exact or fallback


def connect_session(device, attach_only: bool) -> tuple:
    """返回 (session, pid, used_attach)。"""
    if attach_only:
        pid = find_douyin_pid(device)
        if pid:
            print(f"已 attach 到抖音 PID: {pid}")
            return device.attach(pid), pid, True
        print(f"attach 模式: 按包名 attach {PACKAGE_NAME}")
        return device.attach(PACKAGE_NAME), None, True

    adb_force_stop()
    time.sleep(0.5)
    try:
        pid = device.spawn([PACKAGE_NAME])
        print(f"抖音已冷启动，PID: {pid}")
        return device.attach(pid), pid, False
    except Exception as spawn_err:
        err = str(spawn_err)
        if "Gadget" not in err and "jailed" not in err.lower():
            raise
        pid = find_douyin_pid(device)
        if pid:
            print("[!] spawn 失败，改 attach 到已运行的抖音（请先完全杀掉抖音再试 spawn 更稳）")
            return device.attach(pid), pid, True
        raise spawn_err


def print_gadget_help():
    print("\n=== Frida 无法注入 ===\n")
    print("frida-ps -U 若能看到「抖音、设置、浏览器」等很多进程 → server 已运行，请试:")
    print("  python run_spy.py --attach\n")
    print("若 frida-ps 几乎没进程 → 手机端先启动 frida-server（需 Root）:")
    print("  su")
    print("  chmod 755 /data/local/tmp/frida-server")
    print("  /data/local/tmp/frida-server -D    ← 保持此窗口不要关\n")
    print(f"  PC/手机 Frida 版本需一致（当前 PC: {frida.__version__}）")
    print("  验证: frida-ps -U\n")
    print("若无 Root，本方案不可行。")


def print_cert_help():
    print("\n=== mitm 证书（certificate unknown 时必做）===\n")
    print("1. 手机浏览器打开 http://mitm.it 安装 mitmproxy 证书")
    print("2. 小米/MIUI 必须 Root + Magisk 模块「Move Certificates」或「MagiskTrustUserCreds」")
    print("   把用户证书移到【系统信任区】，然后重启手机")
    print("3. 验证: 设置 → 安全 → 加密与凭据 → 受信任的凭据 → 【系统】里应有 mitmproxy")
    print("4. 仅装用户证书不够，Android 7+ 抖音会忽略用户区 CA\n")


def main():
    parser = argparse.ArgumentParser(description="Frida SSL bypass for Douyin + mitmproxy")
    parser.add_argument(
        "--attach",
        action="store_true",
        help="attach 到已运行的抖音（不推荐：SSL 可能在注入前已初始化）",
    )
    args = parser.parse_args()

    if not SCRIPT_PATH.is_file():
        print(f"缺少脚本: {SCRIPT_PATH}")
        sys.exit(1)

    try:
        device = frida.get_usb_device(timeout=5)
        print(f"成功连接至设备: {device.name}")
        print(f"Frida {frida.__version__}")

        proc_count = check_frida_server(device)
        print(f"可见进程数: {proc_count}")
        if proc_count < 10:
            print("[!] 进程过少，frida-server 可能未运行，请先启动 /data/local/tmp/frida-server -D")

        session, pid, used_attach = connect_session(device, args.attach)

        if used_attach:
            print("[!] attach 模式: 若 ecombdapi 仍 certificate unknown，请改用 spawn:")
            print("    完全杀掉抖音 → python run_spy.py")
            load_script(session)
        else:
            load_script(session)
            print("[*] 正在 resume 抖音（冷启动，hook 在 SSL 初始化前生效）…")
            device.resume(pid)
            time.sleep(2.0)

        print("[*] SSL bypass 脚本已加载")
        print("[*] mitmproxy 端口 8080，手机代理=电脑IP:8080")
        print("[*] 看到「native 就绪」后进商城；mitm 里 ecombdapi 应无 certificate unknown")
        print_cert_help()

        input("[*] 保持本窗口运行，按 Enter 退出...\n")

    except frida.ProcessNotFoundError:
        print(f"未找到抖音。请先打开抖音，或运行: python run_spy.py --attach")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except frida.ServerNotRunningError:
        print("frida-server 未运行。手机端: /data/local/tmp/frida-server -D")
        sys.exit(1)
    except Exception as e:
        err = str(e)
        if "Gadget" in err or "jailed" in err.lower():
            print_gadget_help()
            sys.exit(1)
        print(f"发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
