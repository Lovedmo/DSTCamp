"""在 Windows 伪控制台（ConPTY，Windows 10 1809+）里运行命令行程序，实时逐行取得输出（不依赖界面）。

坑（真机实测 SteamCMD）：输出接管道时程序发现不是控制台，会把输出攒到退出才一次性吐出；放进伪控制台后
下载进度每 2 秒一行实时到达。伪控制台的输出带 VT 控制序列，进度行用回车原地刷新，这里统一去掉、按行切分。
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import threading
from ctypes import wintypes
from typing import Callable

_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_STARTF_USESTDHANDLES = 0x00000100
_WAIT_OBJECT_0 = 0
_POLL_MS = 500
# VT 控制序列：CSI（光标、颜色、清屏）、OSC（窗口标题）和单字符转义
_VT_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_LINE_RE = re.compile(r"[\r\n]+")


class _COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    if not hasattr(k, "CreatePseudoConsole"):
        raise OSError("当前 Windows 版本不支持伪控制台（需要 Windows 10 1809 或更高版本）")
    k.CreatePseudoConsole.argtypes = [_COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
    k.CreatePseudoConsole.restype = ctypes.c_long
    k.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    k.ClosePseudoConsole.restype = None
    k.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                             ctypes.c_void_p, wintypes.DWORD]
    k.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                    ctypes.POINTER(ctypes.c_size_t)]
    k.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                 wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                 ctypes.POINTER(_STARTUPINFOEXW), ctypes.POINTER(_PROCESS_INFORMATION)]
    k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    return k


def clean_output(text: str) -> list[str]:
    """去掉 VT 控制序列后按回车/换行切成行（不含空行）。"""
    return [line.strip() for line in _LINE_RE.split(_VT_RE.sub("", text)) if line.strip()]


def _kill_tree(pid: int) -> None:
    # SteamCMD 自更新后会重新拉起自己，只结束父进程会留下子进程
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def run_in_pseudo_console(
    command: list[str],
    on_line: Callable[[str], None],
    *,
    stdin_text: str | None = None,
    cancel_event: threading.Event | None = None,
) -> int:
    """运行命令并实时回调每一行输出，返回退出码；取消时结束整个进程树。

    伪控制台不可用或创建失败抛 OSError，调用方可退回普通管道方式。stdin_text 在启动后写入，
    换行按控制台回车（\\r）发送。"""
    if sys.platform != "win32":
        raise OSError("伪控制台仅支持 Windows")
    k = _kernel32()

    def check(ok, name: str) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error(), name)

    in_read, in_write = wintypes.HANDLE(), wintypes.HANDLE()
    out_read, out_write = wintypes.HANDLE(), wintypes.HANDLE()
    check(k.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0), "CreatePipe")
    check(k.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0), "CreatePipe")
    console = wintypes.HANDLE()
    # 宽度给足，避免长路径被折行切成两行
    result = k.CreatePseudoConsole(_COORD(400, 50), in_read, out_write, 0, ctypes.byref(console))
    k.CloseHandle(in_read)
    k.CloseHandle(out_write)
    if result != 0:
        k.CloseHandle(in_write)
        k.CloseHandle(out_read)
        raise OSError(f"CreatePseudoConsole 失败：{result & 0xFFFFFFFF:#x}")

    attr = None
    process = _PROCESS_INFORMATION()
    try:
        size = ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attr = ctypes.create_string_buffer(size.value)
        check(k.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
        check(k.UpdateProcThreadAttribute(attr, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, console,
                                          ctypes.sizeof(console), None, None), "UpdateProcThreadAttribute")
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        # 坑：父进程标准句柄被重定向时，子进程会继承它们而绕过伪控制台，必须显式置空
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        check(k.CreateProcessW(None, cmdline, None, None, False, _EXTENDED_STARTUPINFO_PRESENT,
                               None, None, ctypes.byref(startup), ctypes.byref(process)), "CreateProcessW")
    except OSError:
        k.ClosePseudoConsole(console)
        k.CloseHandle(in_write)
        k.CloseHandle(out_read)
        if attr is not None:
            k.DeleteProcThreadAttributeList(attr)
        raise

    def reader() -> None:
        buffer = ctypes.create_string_buffer(8192)
        got = wintypes.DWORD()
        pending = ""
        last = None
        while k.ReadFile(out_read, buffer, len(buffer), ctypes.byref(got), None) and got.value:
            pending += buffer.raw[:got.value].decode("utf-8", errors="replace")
            # 最后一段可能是被截断的半行，留到下次拼接
            cut = max(pending.rfind("\r"), pending.rfind("\n"))
            if cut < 0:
                continue
            ready, pending = pending[:cut], pending[cut + 1:]
            for line in clean_output(ready):
                if line != last:  # 伪控制台重绘时会重复输出同一行
                    last = line
                    on_line(line)
        for line in clean_output(pending):
            if line != last:
                on_line(line)

    read_thread = threading.Thread(target=reader, daemon=True)
    read_thread.start()
    try:
        if stdin_text:
            data = stdin_text.replace("\r\n", "\n").replace("\n", "\r").encode("utf-8")
            written = wintypes.DWORD()
            k.WriteFile(in_write, data, len(data), ctypes.byref(written), None)
        while k.WaitForSingleObject(process.hProcess, _POLL_MS) != _WAIT_OBJECT_0:
            if cancel_event is not None and cancel_event.is_set():
                _kill_tree(process.dwProcessId)
                k.WaitForSingleObject(process.hProcess, 5000)
                break
        code = wintypes.DWORD()
        k.GetExitCodeProcess(process.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        # 进程结束后关闭伪控制台，输出管道才会结束，读取线程随之退出
        k.ClosePseudoConsole(console)
        read_thread.join(5)
        k.CloseHandle(in_write)
        k.CloseHandle(out_read)
        k.CloseHandle(process.hProcess)
        k.CloseHandle(process.hThread)
        k.DeleteProcThreadAttributeList(attr)
