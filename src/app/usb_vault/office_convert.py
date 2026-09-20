"""Office -> PDF 批量转换: 后端注册表 (设计文档 D4)。

- win32com    Windows + MS Office (COM, DispatchEx 独立进程, 转完即退)
- applescript macOS + MS Office for Mac (osascript; 实现期对照各 App 字典校准)
- soffice     任一平台 + LibreOffice headless (免费兜底)

转换目的: 收件人端一律内置渲染、绝不调用外部程序, Office 文档必须
先变成 PDF 才能进查看器。后端选择: 显式 backend 参数 (--converter) >
平台默认 (win32com / applescript)。单文件失败即中止烧录。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    import winreg
else:  # 非 Windows 无 winreg 模块
    winreg = None  # type: ignore[assignment]

PROG_ID_BY_EXT: dict[str, str] = {
    ".doc": "Word.Application",
    ".docx": "Word.Application",
    ".xls": "Excel.Application",
    ".xlsx": "Excel.Application",
    ".ppt": "PowerPoint.Application",
    ".pptx": "PowerPoint.Application",
}

MAC_APP_BY_EXT: dict[str, str] = {
    ".doc": "Microsoft Word",
    ".docx": "Microsoft Word",
    ".xls": "Microsoft Excel",
    ".xlsx": "Microsoft Excel",
    ".ppt": "Microsoft PowerPoint",
    ".pptx": "Microsoft PowerPoint",
}

BACKENDS = ("win32com", "applescript", "soffice")

_SOFFICE_TIMEOUT_S = 600.0
_OSASCRIPT_TIMEOUT_S = 300.0


class ConvertError(Exception):
    """Office 转换失败 (未装组件 / 后端执行出错)。"""


def default_backend() -> str:
    """平台默认后端: Windows=win32com, macOS=applescript, 其余=soffice。"""
    if sys.platform == "win32":
        return "win32com"
    if sys.platform == "darwin":
        return "applescript"
    return "soffice"


def _office_files(files: list[Path]) -> list[Path]:
    return [f for f in files if f.suffix.lower() in PROG_ID_BY_EXT]


def is_app_available(prog_id: str) -> bool:
    """查 HKCR 判断 COM 组件是否注册 (不真正启动 Office 进程)。仅 Windows。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id):
            return True
    except OSError:
        return False


def missing_apps(files: list[Path], backend: str | None = None) -> set[str]:
    """这批文件需要但本机缺失的组件名集合 (按后端各自探测)。"""
    backend = backend or default_backend()
    office = _office_files(files)
    if backend == "soffice":
        return set() if shutil.which("soffice") else {"LibreOffice"}
    if backend == "applescript":
        apps = {MAC_APP_BY_EXT[f.suffix.lower()] for f in office}
        return {a for a in apps if not (Path("/Applications") / f"{a}.app").is_dir()}
    if backend == "win32com":
        need = {PROG_ID_BY_EXT[f.suffix.lower()] for f in office}
        return {p for p in need if not is_app_available(p)}
    raise ConvertError(f"未知转换后端: {backend} (可选: {', '.join(BACKENDS)})")


def convert_batch(files: list[Path], out_dir: Path,
                  backend: str | None = None) -> dict[Path, Path]:
    """把 Office 文件转成同名 PDF 到 out_dir; 返回 {源文件: PDF 路径}。"""
    backend = backend or default_backend()
    missing = missing_apps(files, backend)
    if missing:
        names = ", ".join(f.name for f in _office_files(files)) or "(无 Office 文件)"
        raise ConvertError(
            f"后端 {backend} 缺少组件 {', '.join(sorted(missing))}, 无法转换: {names}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    if backend == "soffice":
        return _convert_soffice(files, out_dir)
    if backend == "applescript":
        return _convert_applescript(files, out_dir)
    return _convert_win32com(files, out_dir)


# ---------------------------------------------------------------------------
# LibreOffice headless (跨平台兜底)
# ---------------------------------------------------------------------------

def _convert_soffice(files: list[Path], out_dir: Path) -> dict[Path, Path]:
    """逐文件调用 soffice --headless --convert-to pdf (失败可定位到文件)。"""
    soffice = shutil.which("soffice")
    if not soffice:
        raise ConvertError("未找到 LibreOffice (soffice); 安装后加入 PATH")
    result: dict[Path, Path] = {}
    for src in _office_files(files):
        dst = out_dir / (src.stem + ".pdf")
        argv = [soffice, "--headless", "--convert-to", "pdf",
                "--outdir", str(out_dir), str(src)]
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               check=False, timeout=_SOFFICE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConvertError(f"LibreOffice 转换出错 {src.name}: {exc}") from exc
        if r.returncode != 0 or not dst.is_file():
            tail = (r.stdout + r.stderr)[-400:] if r.returncode != 0 else ""
            raise ConvertError(f"LibreOffice 未产出 {src.stem}.pdf: {tail}".strip())
        result[src] = dst
    return result


# ---------------------------------------------------------------------------
# AppleScript (macOS + Office for Mac)
# ---------------------------------------------------------------------------

def _as_quote(s: str) -> str:
    """AppleScript 字符串字面量转义 (反斜杠与双引号)。"""
    bs = chr(92)
    return '"' + s.replace(bs, bs * 2).replace('"', bs + '"') + '"'


def _applescript_for(app: str, src: Path, dst: Path) -> str:
    s, d = _as_quote(str(src)), _as_quote(str(dst))
    if app == "Microsoft Word":
        return (
            f'tell application "Microsoft Word"\n'
            f"    open POSIX file {s}\n"
            f"    set active document's saved to true\n"
            f"    save as active document file name {d} file format format PDF\n"
            f"    close active document saving no\n"
            f"end tell"
        )
    if app == "Microsoft Excel":
        return (
            f'tell application "Microsoft Excel"\n'
            f"    open POSIX file {s}\n"
            f"    save active workbook in {d} as PDF file format\n"
            f"    close active workbook saving no\n"
            f"end tell"
        )
    return (
        f'tell application "Microsoft PowerPoint"\n'
        f"    open POSIX file {s}\n"
        f"    save active presentation in {d} as save as PDF\n"
        f"    close active presentation saving no\n"
        f"end tell"
    )


def _convert_applescript(files: list[Path], out_dir: Path) -> dict[Path, Path]:
    if sys.platform != "darwin":
        raise ConvertError("applescript 后端仅支持 macOS")
    result: dict[Path, Path] = {}
    for src in _office_files(files):
        app = MAC_APP_BY_EXT[src.suffix.lower()]
        dst = out_dir / (src.stem + ".pdf")
        script = _applescript_for(app, src, dst)
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True,
                               text=True, check=False, timeout=_OSASCRIPT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConvertError(f"AppleScript 转换出错 {src.name}: {exc}") from exc
        if r.returncode != 0 or not dst.is_file():
            raise ConvertError(
                f"转换失败 {src.name}: {r.stderr.strip()[-300:]}".strip())
        result[src] = dst
    return result


# ---------------------------------------------------------------------------
# win32com (Windows + MS Office)
# ---------------------------------------------------------------------------

def _convert_win32com(files: list[Path], out_dir: Path) -> dict[Path, Path]:
    """pywin32 无类型标注, 按动态 COM 对象处理。"""
    import pythoncom  # type: ignore[import-untyped]  # noqa: PLC0415 延迟导入避免无 Office 机器导入失败
    from win32com import client  # type: ignore[import-untyped]

    files = _office_files(files)
    pythoncom.CoInitialize()
    apps: dict[str, object] = {}
    result: dict[Path, Path] = {}
    try:
        for src in files:
            prog_id = PROG_ID_BY_EXT[src.suffix.lower()]
            app = apps.get(prog_id)
            if app is None:
                app = client.DispatchEx(prog_id)
                _configure_app(prog_id, app)
                apps[prog_id] = app
            dst = out_dir / (src.stem + ".pdf")
            _convert_one(prog_id, app, src, dst)
            result[src] = dst
    except ConvertError:
        raise
    except Exception as exc:  # noqa: BLE001 COM 异常类型不稳定, 统一包装
        raise ConvertError(f"Office 转换出错: {exc}") from exc
    finally:
        for app in apps.values():
            try:
                app.Quit()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 退出失败不掩盖转换结果
                pass
        pythoncom.CoUninitialize()
    return result


def _configure_app(prog_id: str, app: object) -> None:
    """静默化 Office 进程 (无窗口/无弹窗)。PowerPoint 无 DisplayAlerts 设置。"""
    try:
        if prog_id != "PowerPoint.Application":
            app.Visible = False  # type: ignore[attr-defined]
            app.DisplayAlerts = 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 个别版本属性缺失不致命
        pass


def _convert_one(prog_id: str, app: object, src: Path, dst: Path) -> None:
    """单文件转换; 失败抛 ConvertError (带文件名)。"""
    try:
        if prog_id == "Word.Application":
            doc = app.Documents.Open(str(src), ReadOnly=True)  # type: ignore[attr-defined]
            try:
                doc.ExportAsFixedFormat(str(dst), 17)  # 17 = wdExportFormatPDF
            finally:
                doc.Close(False)
        elif prog_id == "Excel.Application":
            wb = app.Workbooks.Open(str(src), ReadOnly=True)  # type: ignore[attr-defined]
            try:
                wb.ExportAsFixedFormat(0, str(dst))  # 0 = xlTypePDF
            finally:
                wb.Close(False)
        else:  # PowerPoint
            pres = app.Presentations.Open(  # type: ignore[attr-defined]
                str(src), True, False, False)  # FileName, ReadOnly, Untitled, WithWindow
            try:
                pres.SaveAs(str(dst), 32)  # 32 = ppSaveAsPDF
            finally:
                pres.Close()
    except Exception as exc:  # noqa: BLE001 COM 错误类型不稳定
        raise ConvertError(f"转换失败 {src.name}: {exc}") from exc
