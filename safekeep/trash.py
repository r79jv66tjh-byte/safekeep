"""Löschen ausschließlich über den Windows-Papierkorb.

Nutzt SHFileOperationW mit FOF_ALLOWUNDO. FOF_WANTNUKEWARNING sorgt dafür,
dass Windows nachfragt, falls ein Laufwerk keinen Papierkorb hat und die
Datei sonst endgültig gelöscht würde.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


class TrashError(OSError):
    pass


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def move_to_trash(paths, chunk: int = 200) -> None:
    """Verschiebt Dateien/Ordner in den Papierkorb. Wirft TrashError, wenn
    danach noch etwas am alten Ort liegt."""
    if sys.platform != "win32":
        raise TrashError("Papierkorb wird nur unter Windows unterstützt")
    items = [os.path.abspath(os.fspath(p)) for p in paths]
    items = [p for p in items if os.path.lexists(p)]
    for i in range(0, len(items), chunk):
        part = items[i:i + chunk]
        joined = "\0".join(part) + "\0\0"
        buf = ctypes.create_unicode_buffer(joined, len(joined))
        op = _SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
        op.pTo = None
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                     | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0:
            raise TrashError(f"Papierkorb-Vorgang fehlgeschlagen (Code {rc:#x})")
        if op.fAnyOperationsAborted:
            raise TrashError("Verschieben in den Papierkorb wurde abgebrochen")
    rest = [p for p in items if os.path.lexists(p)]
    if rest:
        raise TrashError("Nicht in den Papierkorb verschoben: " + ", ".join(rest[:5]))
