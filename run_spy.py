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
    for proc in device.enumerate_processes():
        name = (proc.name or "").lower()
        if name in (PACKAGE_NAME, "抖音", "douyin") or PACKAGE_NAME in name:
            return proc.pid
    return None


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


def main():
    parser = argparse.ArgumentParser(description="Frida SSL bypass for Douyin + mitmproxy")
    parser.add_argument(
        "--attach",
        action="store_true",
        help="attach 到已运行的抖音（spawn 失败时用此模式）",
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

        load_script(session)
        time.sleep(0.8)
        print("[*] SSL bypass 已注入")
        print("[*] ① mitmproxy 已开  ② 手机代理=电脑IP:8080  ③ 证书已装 d:\\dy\\certs\\")

        if used_attach:
            print("[*] attach 模式: 请【完全杀掉抖音再重新打开】让 hook 在启动时生效，然后再进商城")
        else:
            device.resume(pid)
            print("[*] 抖音已 resume，请等首页加载完再进商城")

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
