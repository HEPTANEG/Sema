"""Windows Task Scheduler management for the Telegram Gateway."""

import json
import locale
import os
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path
from xml.sax.saxutils import escape

from remote.state import GatewayState

TASK_NAME = "Sema Telegram Gateway"
WORKSPACE = Path.home().resolve()
CREDENTIAL_NAMES = ("SEMA_TELEGRAM_BOT_TOKEN", "SEMA_TELEGRAM_USER_ID", "SEMA_TELEGRAM_CHAT_ID")


def persistent_credentials_present():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return all(bool(winreg.QueryValueEx(key, name)[0]) for name in CREDENTIAL_NAMES)
    except OSError:
        return False


def current_user_identity():
    """Return the current Windows account in Task Scheduler's expected form."""
    result = _run("whoami.exe")
    identity = result.stdout.strip()
    if not result.returncode and identity:
        return identity
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    return f"{domain}\\{user}" if domain and user else user


def task_xml(workspace=WORKSPACE, user_identity=None):
    workspace = Path(workspace).resolve()
    user_identity = user_identity or current_user_identity()



    command = Path(sys.executable).resolve()
    entry = Path(__file__).resolve().parents[1] / "main.py"
    arguments = f'"{entry}" remote service --workspace "{workspace}"'
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user_identity)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>{escape(str(command))}</Command><Arguments>{escape(arguments)}</Arguments><WorkingDirectory>{escape(str(workspace))}</WorkingDirectory></Exec></Actions>
</Task>'''


def _run(*args):


    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
    )


def install():
    if not persistent_credentials_present():
        raise RuntimeError("Set all three Telegram values as persistent User environment variables before installing the Gateway.")
    file_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as handle:
            handle.write(task_xml())
            file_path = handle.name
        result = _run("schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", file_path, "/F")
        if result.returncode:
            details = result.stderr.strip() or result.stdout.strip()
            if "access is denied" in details.casefold() or "拒绝访问" in details:
                raise RuntimeError(
                    "Task Scheduler denied creation. Open PowerShell as Administrator and run `sema remote install` again."
                )
            raise RuntimeError(details)
        return {"installed": True, "workspace": str(WORKSPACE)}
    finally:
        if file_path:
            Path(file_path).unlink(missing_ok=True)


def status():
    result = _run("schtasks.exe", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V")
    snapshot = GatewayState(WORKSPACE).snapshot()
    return {"installed": result.returncode == 0, "workspace": str(WORKSPACE), "task": result.stdout.strip() if result.returncode == 0 else "not installed", **snapshot}


def restart():



    _run("schtasks.exe", "/End", "/TN", TASK_NAME)
    _stop_orphaned_service_processes()
    deadline = time.monotonic() + 10
    while _task_is_running() and time.monotonic() < deadline:
        time.sleep(0.25)
    if _task_is_running():
        raise RuntimeError("Sema Telegram Gateway did not stop in time; retry `sema remote restart`.")
    result = _run("schtasks.exe", "/Run", "/TN", TASK_NAME)
    if result.returncode: raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return {"started": True, "workspace": str(WORKSPACE)}


def _task_is_running():
    result = _run(
        "powershell.exe",
        "-NoProfile",
        "-Command",
        f"(Get-ScheduledTask -TaskName '{TASK_NAME}').State",
    )
    return result.returncode == 0 and result.stdout.strip().casefold() == "running"


def _stop_orphaned_service_processes():
    """Clean up Python children left by old task or supervisor generations."""
    workspace = str(WORKSPACE).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | "
        "Where-Object { (( $_.CommandLine -like '*main.py*remote service*--workspace*') -or "
        "($_.CommandLine -like '*main.py*remote worker*--workspace*')) "
        f"-and ($_.CommandLine -like '*{workspace}*') }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    _run("powershell.exe", "-NoProfile", "-Command", script)


def uninstall():
    result = _run("schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F")
    if result.returncode and "cannot find" not in (result.stderr + result.stdout).lower(): raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return {"uninstalled": True}


def main(argv):
    command = argv[0] if argv else ""
    if command == "install": result = install()
    elif command == "status": result = status()
    elif command == "restart": result = restart()
    elif command == "uninstall": result = uninstall()
    else: raise RuntimeError("Usage: sema remote [install|status|restart|uninstall]")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
