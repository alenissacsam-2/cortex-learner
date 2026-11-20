"""Cortex.exe: the installed app's own program, so Windows shows Cortex, not Python.

`make` copies uv's base `pythonw.exe` into the tool's venv as `app\\Cortex.exe` with the DLLs
it loads (the loader looks beside the exe). Python's site.py takes the folder above the exe's
as the venv (its pyvenv.cfg), so the exe sits one level down, and not in `Scripts\\`, where
uv's `cortex.exe` has the same name to Windows. It then replaces its resources: the Cortex
icon (the package's assets/cortex.ico), a version resource that names it Cortex (Task
Manager, the taskbar) and pythonw's own manifest. The shortcuts run `Cortex.exe -m
cortex.app.launch` and carry APP_ID, the AppUserModelID the app sets on itself
(cortex.app.desktop), so a pinned taskbar button is this shortcut. Stdlib only:
CortexSetup.exe imports it."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import struct
import sys
import time
from ctypes import wintypes as w
from pathlib import Path

NAME = "Cortex.exe"
APP_ID = "Cortex.Learner"  # cortex.app.desktop.APP_ID
ENTRY = ["-m", "cortex.app.launch"]
DLLS = ("python3*.dll", "vcruntime140*.dll")
RT_ICON, RT_GROUP_ICON, RT_VERSION, RT_MANIFEST = 3, 14, 16, 24
LANG = 0x0409  # US English, as pythonw's own resources


def venv(d: Path) -> Path:
    return d / "tools" / "cortex-learner"


def path(d: Path) -> Path:
    return venv(d) / "app" / NAME  # cortex.app.launch.installed_app: <sys.prefix>\app


def ico(d: Path) -> Path:
    return venv(d) / "Lib" / "site-packages" / "cortex" / "assets" / "cortex.ico"


def base_python(d: Path) -> Path:
    """The folder of the Python the venv was made from (`home` in its pyvenv.cfg)."""
    for line in (venv(d) / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home":
            return Path(value.strip())
    raise OSError(f"no home in {venv(d) / 'pyvenv.cfg'}")


def make(d: Path, version: str, say) -> Path:
    """Copy and stamp Cortex.exe; a stamp that fails leaves a working Cortex.exe with
    Python's icon (a warning), a copy that fails raises OSError."""
    home, exe = base_python(d), path(d)
    exe.parent.mkdir(exist_ok=True)
    shutil.copy2(home / "pythonw.exe", exe)
    for pattern in DLLS:
        for dll in home.glob(pattern):
            shutil.copy2(dll, exe.parent / dll.name)
    try:
        stamp(exe, ico(d).read_bytes(), version)
    except OSError as e:
        say(f"warning: {NAME} keeps Python's icon and name ({e})")
    say(f"app: {exe}")
    return exe


# ---- resources ------------------------------------------------------------------------------

def icon_resources(data: bytes) -> tuple[list[bytes], bytes]:
    """An .ico file as RT_ICON images (ids 1..n) and their RT_GROUP_ICON directory."""
    reserved, kind, n = struct.unpack_from("<HHH", data)
    if reserved != 0 or kind != 1 or n == 0:
        raise OSError("not an .ico file")
    images, group = [], struct.pack("<HHH", 0, 1, n)
    for i in range(n):
        w, h, colors, _, planes, bits, size, offset = struct.unpack_from("<BBBBHHII", data,
                                                                         6 + 16 * i)
        images.append(data[offset:offset + size])
        # Pillow writes 0 planes for PNG images; Windows picks an image by its bit depth
        group += struct.pack("<BBBBHHIH", w, h, colors, 0, planes or 1, bits or 32, size, i + 1)
    return images, group


def _node(key: str, value: bytes = b"", text: bool = False, children: tuple = ()) -> bytes:
    """One VS_VERSIONINFO block: header, key, value, then children, each 32-bit aligned."""
    out = b"\0" * 6 + (key + "\0").encode("utf-16-le")
    out += b"\0" * (-len(out) % 4) + value
    for child in children:
        out += b"\0" * (-len(out) % 4) + child
    vlen = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", len(out), vlen, int(text)) + out[6:]


def version_resource(version: str, name: str = "Cortex") -> bytes:
    nums = [int(x) for x in re.findall(r"\d+", version)[:4]]
    nums += [0] * (4 - len(nums))
    ms, ls = nums[0] << 16 | nums[1], nums[2] << 16 | nums[3]
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, ms, ls, ms, ls, 0x3F, 0,
                        0x00040004, 1, 0, 0, 0)  # VOS_NT_WINDOWS32, VFT_APP
    strings = {"CompanyName": name, "FileDescription": name, "FileVersion": version,
               "InternalName": name, "OriginalFilename": NAME, "ProductName": name,
               "ProductVersion": version}
    table = _node("040904B0", children=tuple(
        _node(k, (v + "\0").encode("utf-16-le"), text=True) for k, v in strings.items()))
    var = _node("Translation", struct.pack("<HH", LANG, 1200))
    return _node("VS_VERSION_INFO", fixed, children=(
        _node("StringFileInfo", text=True, children=(table,)),
        _node("VarFileInfo", text=True, children=(var,))))


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.LoadLibraryExW.restype = w.HMODULE
    k.LoadLibraryExW.argtypes = (w.LPCWSTR, w.HANDLE, w.DWORD)
    k.FindResourceW.restype = w.HANDLE
    k.FindResourceW.argtypes = (w.HMODULE, ctypes.c_void_p, ctypes.c_void_p)
    k.SizeofResource.argtypes = (w.HMODULE, w.HANDLE)
    k.LoadResource.restype = w.HANDLE
    k.LoadResource.argtypes = (w.HMODULE, w.HANDLE)
    k.LockResource.restype = ctypes.c_void_p
    k.LockResource.argtypes = (w.HANDLE,)
    k.FreeLibrary.argtypes = (w.HMODULE,)
    k.BeginUpdateResourceW.restype = w.HANDLE
    k.BeginUpdateResourceW.argtypes = (w.LPCWSTR, w.BOOL)
    k.UpdateResourceW.argtypes = (w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, w.WORD,
                                  ctypes.c_char_p, w.DWORD)
    k.EndUpdateResourceW.argtypes = (w.HANDLE, w.BOOL)
    return k


def read_resource(exe: Path, kind: int, name: int) -> bytes | None:
    k = _k32()
    h = k.LoadLibraryExW(str(exe), None, 0x2 | 0x20)  # AS_DATAFILE | AS_IMAGE_RESOURCE
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        res = k.FindResourceW(h, name, kind)
        if not res:
            return None
        return ctypes.string_at(k.LockResource(k.LoadResource(h, res)), k.SizeofResource(h, res))
    finally:
        k.FreeLibrary(h)


def stamp(exe: Path, ico_data: bytes, version: str) -> None:
    """Replace every resource of `exe` with the Cortex icon, a Cortex version resource and
    the manifest it had (pythonw's: Windows versions, long paths)."""
    if sys.platform != "win32":
        raise OSError("resources are stamped on Windows only")
    images, group = icon_resources(ico_data)
    manifest = read_resource(exe, RT_MANIFEST, 1)
    items = [(RT_ICON, i + 1, img) for i, img in enumerate(images)]
    items += [(RT_GROUP_ICON, 1, group), (RT_VERSION, 1, version_resource(version))]
    if manifest:
        items.append((RT_MANIFEST, 1, manifest))
    k = _k32()
    for attempt in range(5):  # an antivirus scan of the new file can hold it for a moment
        h = k.BeginUpdateResourceW(str(exe), True)  # True: delete pythonw's resources
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        for kind, name, data in items:
            if not k.UpdateResourceW(h, kind, name, LANG, data, len(data)):
                err = ctypes.WinError(ctypes.get_last_error())
                k.EndUpdateResourceW(h, True)
                raise err
        if k.EndUpdateResourceW(h, False):
            return
        err = ctypes.WinError(ctypes.get_last_error())
        if attempt == 4:
            raise err
        time.sleep(0.5)


# ---- shortcuts ------------------------------------------------------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16),
                ("d", ctypes.c_ubyte * 8)]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", w.DWORD)]


class PROPVARIANT(ctypes.Structure):  # only the VT_LPWSTR case
    _fields_ = [("vt", w.USHORT), ("r1", w.USHORT), ("r2", w.USHORT), ("r3", w.USHORT),
                ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]


def _guid(s: str) -> GUID:
    g = GUID()
    ctypes.oledll.ole32.CLSIDFromString(s, ctypes.byref(g))
    return g


def _call(obj, index: int, *args, types: tuple = (), restype=ctypes.HRESULT):
    """Method `index` of a COM interface pointer (HRESULT errors raise OSError)."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)(vtable[index])(obj, *args)


def _query(obj, iid: str) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    _call(obj, 0, ctypes.byref(_guid(iid)), ctypes.byref(out),
          types=(ctypes.c_void_p, ctypes.c_void_p))  # QueryInterface
    return out


def shortcut(lnk: Path, target: Path, args: str, icon: Path, app_id: str = APP_ID,
             description: str = "Open the Cortex app") -> None:
    """Write `lnk` (IShellLinkW) with an AppUserModelID (IPropertyStore), which WScript.Shell
    cannot set; the folder is the user's home, as before."""
    ole32 = ctypes.windll.ole32
    init = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED (S_FALSE: already)
    link = ctypes.c_void_p()
    held = []
    try:
        ctypes.oledll.ole32.CoCreateInstance(
            ctypes.byref(_guid("{00021401-0000-0000-C000-000000000046}")), None, 1,  # ShellLink
            ctypes.byref(_guid("{000214F9-0000-0000-C000-000000000046}")),  # IShellLinkW
            ctypes.byref(link))
        held.append(link)
        s = (w.LPCWSTR,)
        _call(link, 20, str(target), types=s)  # SetPath
        _call(link, 11, args, types=s)  # SetArguments
        _call(link, 9, os.path.expanduser("~"), types=s)  # SetWorkingDirectory
        _call(link, 7, description, types=s)  # SetDescription
        _call(link, 17, str(icon), 0, types=(w.LPCWSTR, ctypes.c_int))  # SetIconLocation
        store = _query(link, "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")  # IPropertyStore
        held.append(store)
        key = PROPERTYKEY(_guid("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)  # AppUserModel.ID
        text = ctypes.create_unicode_buffer(app_id)
        value = PROPVARIANT(31, 0, 0, 0, ctypes.cast(text, ctypes.c_void_p))  # VT_LPWSTR
        _call(store, 6, ctypes.byref(key), ctypes.byref(value),
              types=(ctypes.c_void_p, ctypes.c_void_p))  # SetValue
        _call(store, 7)  # Commit
        persist = _query(link, "{0000010B-0000-0000-C000-000000000046}")  # IPersistFile
        held.append(persist)
        _call(persist, 6, str(lnk), True, types=(w.LPCWSTR, w.BOOL))  # Save
    finally:
        for obj in reversed(held):
            if obj:
                _call(obj, 2, restype=ctypes.c_ulong)  # Release
        if init in (0, 1):
            ole32.CoUninitialize()
