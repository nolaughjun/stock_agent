"""查看器构建: PyInstaller 打包 app.py, 内嵌 seed.bin (设计 D3/14.2)。

平台产物形态:
- Windows: 默认 onedir 目录 (杀毒误报率显著低于 onefile 自解压形态);
  --onefile 可回退单文件 exe
- macOS:   onedir .app 目录树 (onefile 无 .app 形态, 强制 onedir),
  构建后自动 ad-hoc codesign (Apple Silicon 拒跑未签名二进制)

SEED 权衡: 默认共享 SEED (构建一次, 反复烧录); 一旦 exe 被逆向提取
SEED, 所有用它烧的盘都暴露。更 paranoid 可每盘重建 (v2 再做)。
--vt-build 用假 SEED 出"自检版", 只用于提交杀毒厂商白名单, 绝不烧盘
(真 SEED 版一旦公开, 等于把钥匙发出去 —— 设计 14.1)。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from src.app.usb_vault.config import MODULE_DIR, VIEWER_VERSION

# 长度 64B 与真 SEED 同形, 内容为公开常量 —— 用它烧的盘谁都能解
VT_FAKE_SEED = b"usb_vault-VT-CHECK-NEVER-DISTRIBUTE-".ljust(64, b"_")[:64]

_HIDDEN_IMPORTS = [
    "src.app.usb_vault",
    "src.app.usb_vault.config",
    "src.app.usb_vault.crypto",
    "src.app.usb_vault.manifest",
    "src.app.usb_vault.inspector",
    "src.app.usb_vault.unlock_code",  # 密码模式: 查看器输入码后的规范化/校验
    "src.app.usb_vault.seed",
    "src.app.usb_vault.volume_id",
    "src.app.usb_vault.boot_sector",  # mac 卷序列号解析链
    "src.app.usb_vault.mac_volume",
    "src.app.usb_vault.viewer",
    "src.app.usb_vault.viewer.state",
    "src.app.usb_vault.viewer.renderers",
    "src.app.usb_vault.viewer.win_protection",
    "src.app.usb_vault.viewer.mac_protection",
]

# 查看器真实依赖只有 stdlib/tkinter/PIL/pymupdf/cryptography, 但 modulegraph 会
# 静态跟进可选导入链 —— 实测入口是 pymupdf.table 函数体内的 `import pandas`
# (表格导出功能, 查看器不用), 由 pandas 一路带出 scipy→torch→transformers→
# fastapi/mcp/mypy 等整个 ML/Web 栈 (837MB)。逐个排除 (pandas 是断链关键,
# 其余是防其他路径复发的保险)。
_EXCLUDES = [
    "pandas", "scipy", "numpy", "matplotlib", "mpl_toolkits", "pylab",
    "torch", "functorch", "transformers", "onnxruntime", "faiss",
    "sklearn", "sympy", "networkx", "joblib", "mpmath", "openpyxl",
    "fsspec", "huggingface_hub", "tokenizers", "safetensors", "tiktoken",
    "mypy", "pytest", "_pytest", "fastapi", "starlette", "pydantic",
    "sqlalchemy", "asyncpg", "psycopg", "redis", "aiohttp", "openai", "mcp",
    "rich", "typer", "click", "uvicorn", "werkzeug", "websockets",
    "httptools", "watchfiles", "google", "grpc", "protobuf", "jinja2",
    "jsonschema", "lxml", "orjson", "yaml", "dateutil", "contourpy",
    "kiwisolver", "fontTools", "zopfli", "zstandard", "xxhash", "regex",
]


class BuildError(Exception):
    """PyInstaller 构建失败。"""


def default_artifact_name() -> str:
    """产物基名: Windows=图书阅览器(目录/exe), macOS=vault_viewer(.app, ASCII!)。

    mac 特意用 ASCII 名: 中文名 .app 经 zip 中转到 Windows 时, 文件名会被按
    本地码页改写 (2026-09 真机事故), Info.plist 的 CFBundleExecutable 对不上
    实际主程序, Mac 上双击无反应。烧录时 burn 会把目录拷为中文名
    图书阅览器.app 写到盘上, 收件人看到的名字不受影响。
    """
    if sys.platform == "darwin":
        from src.app.usb_vault.config import VIEWER_MAC_BUILD_NAME
        return VIEWER_MAC_BUILD_NAME
    from src.app.usb_vault.config import VIEWER_DIR_NAME
    return VIEWER_DIR_NAME


def _stage_pkg_chain(td: Path) -> Path:
    """暂存包链 stage/src/app/usb_vault (仅 .py), 返回 stage 根。

    单目录独立运行形态 (Mac 只拷 usb_vault 目录, 无真实 src/app/usb_vault 链)
    下 modulegraph 解析不了 --hidden-import src.app.usb_vault, PyInstaller 只打
    WARNING 不报错 → 产物缺包, 运行时 ModuleNotFoundError: No module named 'src'
    (2026-09 真机: --windowed 程序一闪即退, 表现为 ".app 双击无反应")。
    暂存链让仓库/单目录两种形态都能解析。
    """
    stage = Path(td) / "stage"
    pkg_dir = stage / "src" / "app" / "usb_vault"
    pkg_dir.mkdir(parents=True)
    (stage / "src" / "__init__.py").write_text("", encoding="utf-8")
    (stage / "src" / "app" / "__init__.py").write_text("", encoding="utf-8")
    skip = {"tests", "dist", "secrets"}
    for py in MODULE_DIR.rglob("*.py"):
        rel = py.relative_to(MODULE_DIR)
        if skip & set(rel.parts):
            continue
        target = pkg_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(py, target)
    return stage


def _run_pyinstaller(args: list[str], hidden: list[str]) -> None:
    """跑 PyInstaller 子进程 + 失败诊断 + 隐藏导入熔断。

    熔断: 隐藏导入没被解析到时 PyInstaller 只打 WARNING、returncode 照样为 0 ——
    产物缺包, 收件人端 No module named 'src' 一闪即退 (2026-09 真机)。挂到本
    构建自己的模块清单上, 当场失败, 不许坏产物出仓。
    """
    try:
        # PyInstaller 输出是 UTF-8: 不指定 encoding 时 Windows 默认 GBK,
        # 中文路径一旦出现就炸掉 reader 线程 (stdout 变 None, 真实报错丢失)
        result = subprocess.run(args, capture_output=True, check=False,
                                encoding="utf-8", errors="replace")
    except OSError as exc:
        raise BuildError(f"无法启动 PyInstaller: {exc} (uv pip install pyinstaller)") from exc
    if result.returncode != 0:
        # stdout 尾部 (进度) + stderr 尾部 (真实报错; 隔离子进程的错常在头部,
        # 留足窗口); None 安全以防将来改为流式输出
        out_tail = (result.stdout or "")[-1200:]
        err_tail = (result.stderr or "")[-2800:]
        raise BuildError(f"PyInstaller 构建失败:\n{out_tail}\n--- stderr ---\n{err_tail}")
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    missing = [
        mod for mod in hidden
        if re.search(rf"Hidden import ['\"]{re.escape(mod)}['\"] not found", combined)
    ]
    if missing:
        raise BuildError(
            "PyInstaller 未解析到以下隐藏导入, 产物会缺包"
            " (收件人端运行报 No module named 'src'):\n  "
            + "\n  ".join(missing)
            + "\n请从完整仓库或完整的 usb_vault 目录 (含全部 .py) 重新构建。"
        )


def _require_tkinter() -> None:
    """构建环境熔断: 本机 Python 必须能 import tkinter (2026-09-18 真机)。

    PyInstaller 是静态分析, 构建机缺 tkinter 不会报错 —— 产物照样出炉,
    收件人端一启动就 `No module named 'tkinter'` 闪退。brew 的 Python
    不带 tkinter (须另装 python-tk), python.org 官方安装包才自带; 版本闸门
    只查 3.10+, 这里补上 GUI 依赖存在性这一环。
    """
    try:
        import tkinter  # noqa: F401
    except ImportError as exc:
        raise BuildError(
            "本机构建查看器的 Python 缺 tkinter (查看器是 Tk 界面, 没它产物"
            "一启动就闪退)。\n"
            "  brew Python:  brew install python-tk@<主版本> 后重建;\n"
            "  或改用 python.org 官方安装包 (自带 tkinter)。\n"
            f"当前解释器: {sys.executable}"
        ) from exc


def build(out_dir: Path, seed: bytes, *, onefile: bool = False,
          name: str | None = None) -> Path:
    """打包查看器; 返回可部署产物根 (onedir/.app 目录 或 单文件)。

    onefile 仅 Windows 有效; macOS 强制 onedir (.app)。
    """
    if len(seed) != 64:
        raise BuildError(f"SEED 必须 64 字节, 收到 {len(seed)}")
    _require_tkinter()
    name = name or default_artifact_name()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    on_win = os.name == "nt"
    if sys.platform == "darwin":
        onefile = False  # mac onefile 无 .app 形态, 且误报率更高
    with tempfile.TemporaryDirectory(prefix="usb_vault_build_") as td:
        seed_file = Path(td) / "seed.bin"
        seed_file.write_bytes(seed)
        # 入口脚本先复制到临时目录: 直接传 src/app/.../app.py 时, PyInstaller 的
        # _extend_pathex 会把入口所在的包链顶层 (src/app) 加进隔离子进程的
        # PYTHONPATH —— 本仓库 src/app 下的 traceback/ 等包随之遮蔽标准库,
        # 子进程 `from traceback import format_exception` 直接 ImportError 暴毙。
        # 临时目录没有 __init__.py 链, 不会触发该行为。
        entry = Path(td) / "viewer_entry.py"
        entry.write_bytes((MODULE_DIR / "viewer" / "app.py").read_bytes())
        sep = ";" if on_win else ":"
        stage = _stage_pkg_chain(Path(td))
        args = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--windowed",
            "--name", name,
            "--distpath", str(out_dir),
            "--workpath", td,
            "--specpath", td,
            "--paths", str(stage),
            "--add-data", f"{seed_file}{sep}.",
        ]
        root = MODULE_DIR.parent.parent.parent  # 仓库根 (src 的上级)
        if (root / "src" / "app" / "usb_vault").is_dir():
            args += ["--paths", str(root)]  # 仓库形态: 原生包链也在
        for mod in _HIDDEN_IMPORTS:
            args += ["--hidden-import", mod]
        for mod in _EXCLUDES:
            args += ["--exclude-module", mod]
        if on_win:
            version_file = Path(td) / "version.txt"
            _write_version_file(version_file, name)
            args += ["--version-file", str(version_file)]
        if onefile:
            args.append("--onefile")
        args.append(str(entry))
        _run_pyinstaller(args, _HIDDEN_IMPORTS)

    if sys.platform == "darwin":
        artifact = out_dir / f"{name}.app"
        ok, note = sign_artifact(artifact)  # ad-hoc 签名, Apple Silicon 必需
        if not ok:
            raise BuildError(f"ad-hoc codesign 失败 ({note}); 请安装 Xcode 命令行工具后重试")
        return artifact
    if onefile:
        artifact = out_dir / f"{name}.exe"
    else:
        artifact = out_dir / name  # onedir 目录
    if not artifact.exists():
        raise BuildError(f"构建完成但未找到产物: {artifact}")
    return artifact


def _write_version_file(path: Path, name: str) -> None:
    """生成 PyInstaller --version-file (Windows 版本资源, 设计 14.2)。"""
    try:
        parts = tuple(int(x) for x in VIEWER_VERSION.split(".")[:3]) + (0,)
    except ValueError as exc:
        raise BuildError(f"VIEWER_VERSION 格式错误: {VIEWER_VERSION}") from exc
    quine = chr(39)  # 单引号, 避免与 f-string 冲突
    text = (
        f"# -*- coding: utf-8 -*-\n"
        f"VSVersionInfo(\n"
        f"  ffi=FixedFileInfo(\n"
        f"    filevers={parts},\n"
        f"    prodvers={parts},\n"
        f"    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,\n"
        f"    date=(0, 0)\n"
        f"  ),\n"
        f"  kids=[\n"
        f"    StringFileInfo([\n"
        f"      StringTable({quine}080404b0{quine}, [\n"
        f"        StringStruct({quine}CompanyName{quine}, {quine}usb_vault{quine}),\n"
        f"        StringStruct({quine}FileDescription{quine}, "
        f"{quine}usb_vault 图书阅览器 (只读){quine}),\n"
        f"        StringStruct({quine}FileVersion{quine}, {quine}{VIEWER_VERSION}.0{quine}),\n"
        f"        StringStruct({quine}ProductName{quine}, "
        f"{quine}usb_vault 图书阅览器{quine}),\n"
        f"        StringStruct({quine}ProductVersion{quine}, "
        f"{quine}{VIEWER_VERSION}.0{quine}),\n"
        f"        StringStruct({quine}OriginalFilename{quine}, {quine}{name}.exe{quine}),\n"
        f"      ])\n"
        f"    ]),\n"
        f"    VarFileInfo([VarStruct({quine}Translation{quine}, [2052, 1200])])\n"
        f"  ]\n"
        f")\n"
    )
    path.write_text(text, encoding="utf-8")


def sign_artifact(artifact: Path, identity: str | None = None) -> tuple[bool, str]:
    """代码签名: macOS=codesign (默认 ad-hoc "-"); Windows=signtool。

    Windows identity 传 .pfx 路径; 省略时 /a 自动选本机最佳证书
    (需先安装代码签名证书, 设计 14.2: 正式分发签名是硬要求)。
    """
    if sys.platform == "darwin":
        argv = ["codesign", "--force", "--deep", "--sign", identity or "-", str(artifact)]
    else:
        argv = ["signtool", "sign", "/fd", "SHA256",
                "/tr", "http://timestamp.digicert.com", "/td", "SHA256"]
        argv += ["/f", identity] if identity else ["/a"]
        argv.append(str(artifact))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, f"无法启动签名工具: {exc}"
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-500:].strip()
    return True, "已签名"
