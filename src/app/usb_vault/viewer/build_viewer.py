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
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from src.app.usb_vault.config import MODULE_DIR, VIEWER_LAUNCHER_BUILD_NAME, VIEWER_VERSION

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
    "src.app.usb_vault.win_volumes",  # v2.4.0 反转布局: 无盘符分区2 的 GUID 寻址
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
        if sys.platform == "darwin":
            # (g) tcl/tk 数据指回 Resources 真树, 见 _write_mac_runtime_hook
            mac_rthook = Path(td) / "pyi_rth_usbvault_mac_dirs.py"
            _write_mac_runtime_hook(mac_rthook)
            args += ["--runtime-hook", str(mac_rthook)]
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
        # 拍平符号链接+框架扁平化必须在 patch/签名之前 (2026-09-25(b)/26(f)):
        # 新版 PyInstaller 布局布满链接, zip 跟随+exFAT 无链接语义两头都失守,
        # 封条登记的就是拍平后的树; 版本化 .framework 靠链接撑规范形态, 链接
        # 既失守 → 一并重构扁平, 否则 codesign 拒签
        _deref_mac_symlinks(artifact)
        # (i) 搬移后的 rpath 归一化 + 解析自包含/scrypt 双不变量, 必须在
        # patch/签名之前 (改的是字节内容, 封条登记的就是改写后的树)
        _fix_mac_rpaths(artifact)
        _assert_mac_dylib_resolution(artifact)
        _assert_mac_scrypt_ready(artifact)
        patch_mac_bundle_metadata(artifact, name)  # 公证前提: 合法 identifier+版本
        ok, note = sign_artifact(artifact)  # ad-hoc 签名, Apple Silicon 必需
        if not ok:
            # 报错不写死 "ad-hoc": --sign 用户第二遍正式签名前死在这里时,
            # 文案曾误导排障方向 (2026-09-26 真机)
            raise BuildError(f"codesign 签名失败 ({note}); 请安装 Xcode 命令行工具后重试")
        return artifact
    if onefile:
        artifact = out_dir / f"{name}.exe"
    else:
        artifact = out_dir / name  # onedir 目录
    if not artifact.exists():
        raise BuildError(f"构建完成但未找到产物: {artifact}")
    return artifact


def build_launcher(out_dir: Path) -> Path:
    """构建书名引导壳 (onefile, 仅 Windows; v2.4.5)。返回 exe 路径。

    壳的职责见 launcher_entry.py 模块注释: 收件人双击的书名 exe 必须自包含,
    否则被单独拷走时死在 PyInstaller C 装载器层, 中文谢幕文案到不了屏幕。
    依赖刻意最小 (ctypes/subprocess/pathlib, 无 tkinter/PIL/PyMuPDF):
    产物 ~10MB、解包亚秒, 杀毒签名面不随壳扩张。
    """
    if os.name != "nt":
        raise BuildError("书名引导壳仅 Windows 构建 (双入口盘的 Windows 目录入口专用)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="usb_vault_launcher_") as td:
        # 入口同样先复制到临时目录 (与 build() 同一 pathex 防投毒理由)
        entry = Path(td) / "launcher_entry.py"
        entry.write_bytes((MODULE_DIR / "viewer" / "launcher_entry.py").read_bytes())
        stage = _stage_pkg_chain(Path(td))
        args = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--windowed",
            "--onefile",  # 壳存在的意义就是"单独拷走也能跑"
            "--name", VIEWER_LAUNCHER_BUILD_NAME,
            "--distpath", str(out_dir),
            "--workpath", td,
            "--specpath", td,
            "--paths", str(stage),
            "--hidden-import", "src.app.usb_vault.config",
            str(entry),
        ]
        root = MODULE_DIR.parent.parent.parent  # 仓库根 (src 的上级)
        if (root / "src" / "app" / "usb_vault").is_dir():
            args += ["--paths", str(root)]
        version_file = Path(td) / "version.txt"
        _write_version_file(version_file, VIEWER_LAUNCHER_BUILD_NAME)
        args += ["--version-file", str(version_file)]
        _run_pyinstaller(args, ["src.app.usb_vault.config"])
    artifact = out_dir / f"{VIEWER_LAUNCHER_BUILD_NAME}.exe"
    if not artifact.exists():
        raise BuildError(f"引导壳构建完成但未找到产物: {artifact}")
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


def _write_mac_runtime_hook(path: Path) -> None:
    """写 Mac 专用运行时钩子: .app 下把 TCL_LIBRARY/TK_LIBRARY 指回 Resources 真树。

    2026-09-26(g): _tcl_data/_tk_data 是 PyInstaller 收的顶层 tcl/tk 数据树
    (hook-_tkinter.py → utils/hooks/tcl_tk.py), .app 里真身在 Resources、
    Frameworks 侧靠目录链接镜像 —— 拍平已删该链接 (拷贝会把树内带点子目录
    opt0.4 等搬进 Frameworks, codesign 拒签)。自带钩子 pyi_rth__tkinter 只认
    MEIPASS/_tcl_data, 目录不在时静默不设环境变量 → Tk 初始化死于
    "Can't find a usable init.tcl"。本钩子按 .app 布局 (MEIPASS 上级是
    Contents) 补设环境变量; 与自带钩子顺序无关: 它只在 MEIPASS 下目录
    存在时才覆写, 本产物里恒不存在。非 .app 布局 (onedir 目录) 整体空转。
    """
    path.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "_meipass = getattr(sys, '_MEIPASS', '')\n"
        "if (os.path.basename(_meipass) == 'Frameworks'\n"
        "        and os.path.basename(os.path.dirname(_meipass)) == 'Contents'):\n"
        "    _resources = os.path.join(os.path.dirname(_meipass), 'Resources')\n"
        "    for _var, _sub in (('TCL_LIBRARY', '_tcl_data'),\n"
        "                       ('TK_LIBRARY', '_tk_data')):\n"
        "        _path = os.path.join(_resources, _sub)\n"
        "        if os.path.isdir(_path):\n"
        "            os.environ[_var] = _path\n",
        encoding="utf-8")


def _resolve_via_dir_links(path: Path, links: dict[Path, Path]) -> Path:
    """词法解析 path: 归一 .., 把仍是指令链接的祖先目录替换为其目标, 迭代到不再变。

    例: Resources/libXau.6.dylib 的目标 ../Frameworks/PIL/.dylibs/libXau.6.dylib
    途经目录链接 Frameworks/PIL/.dylibs —— 替换后落到真身
    Frameworks/PIL/__dot__dylibs/libXau.6.dylib, 拷贝从此不再依赖链接存活
    (真链接上 OS 会自己穿, 注入式伪链接只有这条路)。
    """
    cur = Path(os.path.normpath(path))
    for _ in range(10):
        hit = max((a for a in cur.parents if a in links),
                  key=lambda q: len(q.parts), default=None)
        if hit is None:
            return cur
        nxt = Path(os.path.normpath(links[hit] / cur.relative_to(hit)))
        if nxt == cur:
            return cur
        cur = nxt
    raise BuildError(f".app 符号链接解析不收敛 (疑似循环): {path}")


# python3.14 类版本目录 (点名替换链接的名字形态, 两侧镜像都要删)
_PYTHON_VER_DIR_RE = re.compile(r"python\d+\.\d+")

# Mach-O 载入命令解析 (rpath 归一化 / 依赖自包含不变量共用)。只认四种
# 单架构瘦二进制魔数; fat 通用二进制 (cafebabe) PyInstaller 产物实测不
# 出现, 刻意不认 —— 误把随机数据 (如 seed.bin) 当 fat 解析反而是坑。
_MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                 b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce")
_LC_LOAD_DYLIB = 0xC
_LC_RPATH = 0x8000001C
_LC_STR_OFF = 8  # load_command 头 (cmd+cmdsize) 之后即 lc_str {uint32 offset}


def _deref_mac_symlinks(
    app: Path,
    *,
    is_symlink: Callable[[Path], bool] = Path.is_symlink,
    readlink: Callable[[Path], str] = os.readlink,
) -> int:
    """把 .app 内符号链接处理成纯文件树 + .framework 扁平化, 返回处理数。必须在签名前执行。

    2026-09-25(b) 真机事故: 新版 PyInstaller 的 .app 布局布满符号链接
    (实测 54 处: Resources/⇄Frameworks/ 互链、PIL/.dylibs→__dot__dylibs、
    python3.14→python3__dot__14、seed.bin→../Resources/seed.bin…)。两条死路:
    - Info-ZIP `zip -r` 默认**跟随**链接打包 —— zip 里 0 个链接、全是目标内容的
      副本, 解压树与封条 (封条逐条登记了链接) 不符 → 收件人 Gatekeeper 必弹
      「已损坏」;
    - 交付棒分区1 是 exFAT, 没有符号链接语义, 就算中转无损烧上盘也必失守。

    2026-09-26(c) 三分法定谳 (Mac 真机 codesign 拒签事故: `bundle format
    unrecognized ... Frameworks/PIL/.dylibs`): 拷贝策略对"名字带点的目录链接"
    是毒药 —— codesign 规定 Contents/Frameworks 下只有 .framework 目录许带点,
    这正是 PyInstaller 把真目录起名 __dot__dylibs、另留软链的原因。依据
    PyInstaller 6.22 源码 (utils/osx.py: 所有非系统依赖的 load command 一律
    改写成 `@rpath/<纯文件名>`, 目录分量不进引用):
    - 名字带点的**目录**链接且在 Contents/Frameworks 下 → 先拍完所有途经它的
      拷贝, 再**删除** (落成真目录 = codesign 必拒; 引用它的人只有顶层文件
      链接, 拷贝后自成真文件, 无引用残留);
    - 其余 (带点文件链接如 python3.14→真文件、Resources 下带点目录链接、
      所有不带点链接) → 照旧拍平成真实拷贝 (Resources 侧点名目录 codesign
      不禁; 文件名带点天然合法)。

    2026-09-26(d) 收口 (Mac 真机二连: 不变量自检在 Frameworks 下抓到带点
    真目录, 用户报作 "opt0.4, codesign 拒签"): (c) 只删"带点名"目录链接
    还不够 —— Frameworks 下**不带点**目录链接 (如 tcl8) 走拷贝, copytree
    把 Resources 侧数据树里的带点子目录 (Tcl/Tk 的版本目录 8.4/8.6 这类)
    原样搬进 Frameworks, 同样是 codesign 毒。定谳 (PyInstaller osx.py
    Step 2): Frameworks 侧目录链接**全部**是纯数据目录的镜像软链
    (_DATA_DIR_TYPE: 真身在 Resources, 链接只为维持两棵树内容一致的观感),
    运行期无人寻址 (二进制依赖全走 @rpath/<纯文件名> → Frameworks 顶层
    真实文件)。收口规则: Frameworks 下目录链接**一律删除** ((d) 曾留
    ".framework 内部照常拍平"的例外)。

    2026-09-26(e) 三连收口 (Mac 真机: `bundle format is ambiguous (could be
    app or framework)`): (d) 的例外错了 —— 框架规范 (PyInstaller osx.py:197-199
    转述 Apple 规则): **Versions/ 旁边只许站符号链接** (指向
    Versions/Current/... 的别名)。把 Versions/Current、框架根的 Resources/
    Python 别名拍平成真目录/真文件 = 非规范框架结构, codesign 面对无扩展名
    却带 Info.plist/Resources 的目录无法分类 → ambiguous。同理, 名字以
    .framework 结尾的镜像链接 (Resources/Python.framework→../Frameworks/…)
    落成拷贝会在拷贝内部再造违规结构 (copytree 跟随内层链接)。规则:
    **.framework 子树内链接 (含文件别名) 一律删除; 名字以 .framework 结尾
    的链接一律删除**。

    2026-09-26(f) 四连收口 (Mac 真机: `bundle format unrecognized … In
    subcomponent: …/Frameworks/Python.framework`): (e) 删光框架内链接后,
    剩下无 Current 无根级别名的 Versions/<ver>/ 残架 codesign 仍不认
    (PyInstaller utils/osx.py:672 自己的注释: Current "seems to be
    necessary for code signing")。链接留不住 (exFAT/zip 中转必失守),
    版本化布局又离不开链接 —— 唯一活路 = 链接删净后把 .framework 重构成
    **扁平布局** (二进制与 Resources 提到框架根, Versions/ 整体移除):
    规范第 4 条 (osx.py:196-199) 是条件式 —— "若顶层**有** Versions 目录,
    旁边才不许有他物", 没有 Versions/ 的经典扁平框架不触此条。运行时
    零影响 (PyInstaller 6.22.2 源码链实证): bootloader dlopen 的是
    Frameworks 顶层锚点 (api.py 存纯 basename → bindepend 为非顶层二进制
    造顶层链接 → bootloader pyi_dylib_python.c 裸拼接 "%s/%s" 直接 dlopen,
    无框架路径魔法), 该顶层链接本就被拍平成真文件; 其余依赖的 load
    command 全是 @rpath/<纯文件名> → 同样解析到 Frameworks 顶层, 框架
    内部布局无人在意。见 _flatten_mac_frameworks。

    2026-09-26(g) 直跑收口 (真机 ModuleNotFoundError: No module named
    '_struct'): (c)(d) 的"目录链接一律删"漏算了一类运行时寻址 —— 导入器
    按 sys.path 落盘探测扩展模块。PyInstaller 把 lib-dynload 扩展的落盘名
    改写为 python3.14/lib-dynload/… (building/utils.py:95-101, #5604/#9204),
    .app 里靠 "python3.14 → python3__dot__14" 点名替换链接寻址, 链接一删
    struct 便 import 即死 (struct 由 bootloader 最先导入的
    pyimod01_archive 触发, 早于一切钩子, 无 Python 层可救);
    _tcl_data/_tk_data 数据镜像同理 (pyi_rth__tkinter 只认 MEIPASS 下
    目录)。链接照删 (拷贝均触 codesign 点名关), 补偿两件:
    (1) python3__dot__14/lib-dynload 的 .so 提升到 Frameworks 顶层 =
    sys.path 首项, FileFinder 自然命中 (_hoist_mac_lib_dynload);
    (2) Mac 运行时钩子把 TCL_LIBRARY/TK_LIBRARY 指回 Resources 真树
    (_write_mac_runtime_hook)。

    2026-09-29(h) 公证收口 (真机 notarytool Invalid, log 全部指向
    Contents/Resources/PIL/.dylibs/*.dylib: "The binary is not signed
    with a valid Developer ID certificate" + "no secure timestamp"):
    PyInstaller osx.py 的双树布局里, 二进制真树在 Frameworks、Resources
    侧只放**镜像链接** (目录镜像 Resources/PIL→../Frameworks/PIL, 顶层
    文件镜像 Resources/libwebp.dylib→…)。拍平时这些链接按"Resources 下
    普通链接照拷"落成了**真拷贝** —— 但外层 codesign --deep 只签代码位置
    (MacOS/Frameworks/…), Resources 下的散装 Mach-O 被当作"资源"只封哈希
    不签名, 于是这批镜像副本成了无主未签名二进制, 公证逐条拒。09-25 那次
    Accepted 是因为当时还是链接 (公证 zip 用 ditto 保链接, 服务端看到的是
    链接条目不是副本)。定谳规则: **Resources 下链接的最终目标落在
    Frameworks = 纯代码镜像, 一律删除** (运行时 MEIPASS=Frameworks, 无人
    从 Resources 装载代码 —— sys.path 首项/DYLD 解析/tcl 数据全不在
    Resources 侧), 同时吞掉 (g) 的 python3.14 特例。Resources 内部指向
    Resources 的数据链接照旧拷贝。

    is_symlink/readlink 可注入: Windows 测试机造不了真符号链接 (需开发者模式)。
    """
    fw_root = app / "Contents" / "Frameworks"
    res_root = app / "Contents" / "Resources"
    total = 0
    for _round in range(10):
        links = [p for p in app.rglob("*") if is_symlink(p)]
        if not links:
            # 链接删净后才扁平化: 重构只面对纯真文件树, 不用管链接语义
            total += _flatten_mac_frameworks(app)
            # 链接删净后把 lib-dynload 扩展提到顶层 (运行时寻址, 见函数注)
            total += _hoist_mac_lib_dynload(app)
            _assert_mac_frameworks_dirs_signable(app)
            _assert_mac_resources_code_free(app)
            return total
        # 先把每个链接解析到"最终具体目标" (成链 python3.14→python3.14→
        # python3__dot__14、途经 PIL/.dylibs 的引用都直接落到真身), 之后
        # 拷贝/删除互不依赖, 也避免"先删了目录链接, 后拍的链接目标失踪
        # 被误报断链"。
        raw: dict[Path, Path] = {}
        for p in links:
            t = Path(readlink(p))
            raw[p] = t if t.is_absolute() else p.parent / t
        final: dict[Path, Path] = {}
        for p in links:
            t, seen = raw[p], {p}
            while True:
                t = _resolve_via_dir_links(t, raw)
                if t not in raw:
                    break
                if t in seen:
                    raise BuildError(f".app 内符号链接循环: {p} -> {readlink(p)}")
                seen.add(t)
                t = raw[t]
            final[p] = t
            if not t.exists():
                raise BuildError(f".app 内符号链接断链: {p} -> {readlink(p)}")
        deletes: list[Path] = []
        copies: list[Path] = []
        for p in links:
            # (e) 判定。.framework 子树内链接一律删除: 框架规范
            # (osx.py:197-199) 要求 Versions/ 旁边只许站符号链接, 拍平成
            # 真目录/真文件 = 非规范结构 → codesign "bundle format is
            # ambiguous (could be app or framework)"。名字以 .framework
            # 结尾的链接 (Resources→Frameworks 的整框架镜像) 同删: 落成
            # 拷贝会在拷贝内部再造违规结构 (copytree 跟随内层链接)。
            # Frameworks 下其余目录链接仍是 (c)(d) 的删除规则。
            # (h) 补: Resources 下链接的最终目标落在 Frameworks = 纯代码
            # 镜像 (osx.py Step 2/Step 4: 二进制真树在 Frameworks,
            # Resources 侧只放镜像链接), 目录与文件链接一律删除 —— 落成
            # 拷贝 = 无主未签名 Mach-O, 外层 codesign --deep 只签代码位置
            # (MacOS/Frameworks), Resources 下的散装二进制只封哈希不签名,
            # 公证逐条拒 "not signed with a valid Developer ID
            # certificate" (2026-09-29 真机 Invalid 定谳)。运行时无人从
            # Resources 装载代码 (MEIPASS=Frameworks), 删了是纯减重。
            # 此规则同时覆盖 (g) 的 python3.14 Resources 侧镜像特例。
            rel = p.relative_to(app).parts
            in_framework = any(part.endswith(".framework") for part in rel[:-1])
            res_mirror_of_fw = (
                res_root in p.parents and fw_root in final[p].parents)
            if (in_framework or p.name.endswith(".framework")
                    or (final[p].is_dir() and fw_root in p.parents)
                    or res_mirror_of_fw):
                deletes.append(p)
            else:
                copies.append(p)
        progressed = False
        # 拷贝先行: 途经带点目录链接的顶层文件链接 (libXau.6.dylib→PIL/.dylibs/…)
        # 必须在目录链接被删除前落到真身内容。
        for p in sorted(copies, key=lambda q: len(q.parts)):
            if not is_symlink(p):
                continue  # 已被先前 copytree 跟随复制顺带拍平
            target = final[p]
            try:
                p.unlink()  # 真链接 (无论指向文件还是目录) 一律 unlink 可删
            except OSError:
                p.rmdir()  # 注入式测试的伪链接可能是空目录
            if target.is_dir():
                shutil.copytree(target, p)  # 默认跟随内层链接, 残留的下一轮再拍
            else:
                shutil.copy2(target, p)
            total += 1
            progressed = True
        for p in deletes:
            if not is_symlink(p):
                continue
            try:
                p.unlink()
            except OSError:
                p.rmdir()  # 注入式测试的伪链接是空目录
            total += 1
            progressed = True
        if not progressed:
            raise BuildError(
                ".app 符号链接拍平未收敛 —— 疑似循环链接或断链, 请检查产物")
    raise BuildError(".app 符号链接拍平 10 轮未收敛 —— 疑似循环链接, 请检查产物")


def _flatten_mac_frameworks(app: Path) -> int:
    """把每个 .framework 从版本化布局重构成扁平布局, 返回搬移条目数。

    2026-09-26(f): 版本化框架 (Versions/<ver>/{二进制, Resources/}) 的
    规范形态离不开符号链接 (Current + 根级别名), 而链接过不了 zip/exFAT
    中转; (e) 删光链接后的残架 codesign 报 unrecognized。扁平布局 =
    根级真二进制 + 根级 Resources/, 无 Versions/, 不触规范第 4 条
    (条件式: 有 Versions 才禁他物站旁边)。运行时无人在意框架内部布局
    (bootloader 走 Frameworks 顶层锚点真文件, 依赖全走 @rpath/<纯文件名>)。

    意外结构 (版本目录数 ≠1、搬移撞名) 一律熔断报路径 —— 宁可构建失败,
    不让 codesign 报天书。
    """
    fw_root = app / "Contents" / "Frameworks"
    if not fw_root.is_dir():
        return 0
    moved = 0
    for fw in sorted(p for p in fw_root.rglob("*.framework") if p.is_dir()):
        versions = fw / "Versions"
        if not versions.is_dir():
            continue  # 本来就是扁平布局 (部分 wheel 如此), 不动
        ver_dirs = [d for d in versions.iterdir() if d.is_dir()]
        if len(ver_dirs) != 1:
            raise BuildError(
                f".framework 版本目录异常 ({fw}): 期望唯一真身版本目录, "
                f"实得 {len(ver_dirs)} 个 —— 产物疑被外部动过, 请 rm -rf "
                f"dist build 重建")
        for entry in sorted(ver_dirs[0].iterdir()):
            dest = fw / entry.name
            if dest.exists():
                raise BuildError(
                    f".framework 扁平化撞名: {dest} 已存在 —— 产物疑被外部"
                    f"动过, 请 rm -rf dist build 重建")
            shutil.move(str(entry), str(dest))
            moved += 1
        shutil.rmtree(versions)
    return moved


def _hoist_mac_lib_dynload(app: Path) -> int:
    """把 pythonX__dot__Y/lib-dynload 的扩展模块提到 Frameworks 顶层, 返回搬移数。

    2026-09-26(g) 直跑事故 (真机 ModuleNotFoundError: No module named
    '_struct'): PyInstaller 把源自 lib-dynload 的扩展落盘名改写成
    python3.14/lib-dynload/… (building/utils.py:95-101, #5604/#9204), .app
    里靠 "python3.14 → python3__dot__14" 点名替换链接让导入器寻址, 拍平
    已删此链接 (拷贝成真目录 = codesign 点名关必拒)。死结在于时机:
    struct 由 bootloader 最先导入的 pyimod01_archive (loader/
    pyimod01_archive.py:18 import struct) 触发, 早于一切运行时钩子 ——
    无 Python 层可救, 唯一修法 = 把 .so 提到 MEIPASS 顶层 (= Contents/
    Frameworks 顶层, sys.path 首项), FileFinder 落盘探测自然命中。

    意外结构 (缺 lib-dynload 子目录、内含子目录、顶层撞名) 一律熔断报
    路径 —— 宁可构建失败, 不让运行时报一句难懂的 ModuleNotFoundError。
    """
    fw_root = app / "Contents" / "Frameworks"
    if not fw_root.is_dir():
        return 0
    moved = 0
    for d in sorted(fw_root.iterdir()):
        if not d.is_dir() or "__dot__" not in d.name:
            continue
        if _PYTHON_VER_DIR_RE.fullmatch(d.name.replace("__dot__", ".")) is None:
            continue
        lib_dynload = d / "lib-dynload"
        if not lib_dynload.is_dir():
            raise BuildError(
                f"lib-dynload 提升异常 ({d}): 缺 lib-dynload 子目录 —— 产物"
                f"疑被外部动过, 请 rm -rf dist build 重建")
        for entry in sorted(lib_dynload.iterdir()):
            if entry.is_dir():
                raise BuildError(
                    f"lib-dynload 提升异常 ({entry}): 意外子目录 —— 请同步"
                    f"最新代码后 rm -rf dist build 重建")
            dest = fw_root / entry.name
            if dest.exists():
                raise BuildError(
                    f"lib-dynload 提升撞名: {dest} 已存在 —— 产物疑被外部"
                    f"动过, 请 rm -rf dist build 重建")
            shutil.move(str(entry), str(dest))
            moved += 1
        shutil.rmtree(d)
    return moved


def _macho_scan(raw: bytes) -> tuple[str, list[str], list[tuple[int, int, str]]] | None:
    """解析 Mach-O 动态引用: 返回 (端序, LC_LOAD_DYLIB 名单, rpath 条目)。

    rpath 条目 = (命令偏移, 字符串文件内偏移, 字符串) —— 偏移供 _fix_mac_rpaths
    原位改写。非 Mach-O 返回 None; 载入命令越界/字符串未终结一律 BuildError:
    构建期宁可熔断, 不让 dyld 在收件人机器上报天书。
    """
    magic = raw[:4]
    if magic == b"\xcf\xfa\xed\xfe":
        e, hdr = "<", 32
    elif magic == b"\xce\xfa\xed\xfe":
        e, hdr = "<", 28
    elif magic == b"\xfe\xed\xfa\xcf":
        e, hdr = ">", 32
    elif magic == b"\xfe\xed\xfa\xce":
        e, hdr = ">", 28
    else:
        return None
    if len(raw) < hdr:
        raise BuildError(f"Mach-O 头截断: 仅 {len(raw)} 字节")
    ncmds = struct.unpack_from(e + "I", raw, 16)[0]
    dylibs: list[str] = []
    rpaths: list[tuple[int, int, str]] = []
    off = hdr
    for _ in range(ncmds):
        if off + 8 > len(raw):
            raise BuildError(f"Mach-O 载入命令截断于偏移 {off}")
        cmd, cmdsize = struct.unpack_from(e + "II", raw, off)
        if cmdsize < 8 or off + cmdsize > len(raw):
            raise BuildError(f"Mach-O 载入命令异常 (off={off}, size={cmdsize})")
        if cmd in (_LC_LOAD_DYLIB, _LC_RPATH):
            name_off = struct.unpack_from(e + "I", raw, off + _LC_STR_OFF)[0]
            start = off + name_off
            if name_off < _LC_STR_OFF + 4 or start >= off + cmdsize:
                raise BuildError(f"Mach-O lc_str 偏移异常 (off={off}, name_off={name_off})")
            try:
                end = raw.index(b"\x00", start)
            except ValueError as exc:
                raise BuildError(f"Mach-O lc_str 未终结 (off={off})") from exc
            if end >= off + cmdsize:
                raise BuildError(f"Mach-O lc_str 越过命令边界 (off={off})")
            name = raw[start:end].decode("utf-8", "replace")
            if cmd == _LC_LOAD_DYLIB:
                dylibs.append(name)
            else:
                rpaths.append((off, start, name))
        off += cmdsize
    return e, dylibs, rpaths


def _iter_mac_macho(root: Path) -> Iterator[tuple[Path, bytes]]:
    """遍历 root 下所有瘦 Mach-O 文件, 产出 (路径, 全文)。"""
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        with p.open("rb") as fh:
            head = fh.read(4)
        if head not in _MACHO_MAGICS:
            continue
        yield p, p.read_bytes()


def _fix_mac_rpaths(app: Path) -> int:
    """把 Contents/Frameworks 下 @loader_path rpath 归一化回 Frameworks 顶层,
    返回改写条数。必须在签名前执行 (改的是字节内容, 封条登记的就是改写后的树)。

    2026-09-29(i) 直跑解锁收口 (真机 AttributeError: module 'hashlib' has no
    attribute 'scrypt'): PyInstaller osx.py 的布局契约 = 二进制真身按自然深度
    落盘 (python3__dot__Y/lib-dynload/、PIL/__dot__dylibs/、pymupdf/…), 各自
    盖一张"按自身深度指向 Frameworks 顶层"的 rpath (@loader_path/../.. 或
    @loader_path/..), 再在顶层放别名链接让所有 @rpath/<纯文件名> 解析命中。
    拍平手术把顶层链接拷成真文件 ((c))、把真身提升到顶层 ((g)) —— 文件挪了
    位置, 字节里的 rpath 算术没人跟着改: 提升副本在顶层再上一层解析到
    Contents/, 依赖全灭。实测伤亡: _hashlib/_ssl 加载不起来 (hashlib 静默
    降级、scrypt 属性消失, 解锁派生即崩 —— sha256 走内置 _sha2 照常, 开窗/
    弹码框全不受影响, 极具迷惑性), _lzma/_decimal/_zstd、libmupdfcpp (PDF
    渲染)、libtiff/libwebp/libxcb (图像解码) 同病。

    修法 = 就地把走错楼的 rpath 改写回按当前真实位置指向 Frameworks 顶层;
    只缩短不增长 (原位 NUL 填充, 载入命令长度不变, 依赖声明本身不动)。
    原位真身 (PIL/__dot__dylibs 等深处的原件) rpath 本来就对, 自然零改动。
    """
    fw_root = (app / "Contents" / "Frameworks").resolve()
    patched = 0
    for p, raw in _iter_mac_macho(fw_root):
        scan = _macho_scan(raw)
        if scan is None:
            continue
        _e, _dylibs, rpath_cmds = scan
        rel = os.path.relpath(fw_root, p.parent.resolve())
        wanted = "@loader_path" if rel == "." else (
            "@loader_path/" + rel.replace(os.sep, "/"))
        dirty = False
        for _cmd_off, str_start, old in rpath_cmds:
            if not old.startswith("@loader_path"):
                continue  # @executable_path/绝对 rpath 不动, 由解析不变量把关
            rest = old[len("@loader_path"):].lstrip("/")
            base = (p.parent / rest).resolve() if rest else p.parent.resolve()
            if base == fw_root:
                continue  # 本来就指向顶层 (原位真身 / 顶层裸 @loader_path)
            if len(wanted) > len(old):
                raise BuildError(
                    f"rpath 归一化需增长 ({p.name}: {old!r} → {wanted!r}) —— "
                    f"无法原位改写, 请同步最新代码后 rm -rf dist build 重建")
            end = raw.index(b"\x00", str_start)
            payload = wanted.encode() + b"\x00" * (end - str_start + 1 - len(wanted))
            raw = raw[:str_start] + payload + raw[end + 1:]
            dirty = True
            patched += 1
        if dirty:
            p.write_bytes(raw)
    return patched


def _mac_main_executable(app: Path) -> Path | None:
    """按 Info.plist 的 CFBundleExecutable 定位主程序; 无/坏 plist 返回 None。"""
    try:
        exe_name = plistlib.loads(
            (app / "Contents" / "Info.plist").read_bytes()).get("CFBundleExecutable")
    except (OSError, ValueError):
        return None
    if not isinstance(exe_name, str) or not exe_name:
        return None
    return app / "Contents" / "MacOS" / exe_name


def _resolve_mac_dep(dep: str, loader_dir: Path,
                     rpath_bases: list[tuple[Path, str]],
                     macos_dir: Path) -> str | None:
    """模拟 dyld 解析一条依赖; 可解析返回 None, 否则返回一句话病因。

    rpath_bases = [(rpath 所属二进制所在目录, rpath)] —— 自身的 @loader_path
    相对自己、主程序的相对主程序, 不能混用。刻意比 dyld 更严: 绝对 rpath
    (/opt/homebrew/… 构机路径) 不算数 —— 交付必须自包含, "在构建机上碰巧
    能解析"正是要防的事故。
    """
    if dep.startswith("/System/") or dep.startswith("/usr/lib"):
        return None

    def join(base: Path, ref: str) -> Path:
        if "/" not in ref:
            return base
        return base / ref.split("/", 1)[1].lstrip("/")

    def expand(base: Path, rp: str, tail: str, out: list[Path]) -> None:
        if rp == "@loader_path":
            out.append(base / tail)
        elif rp.startswith("@loader_path/"):
            out.append(join(base, rp) / tail)
        elif rp == "@executable_path":
            out.append(macos_dir / tail)  # @executable_path 恒指主程序目录
        elif rp.startswith("@executable_path/"):
            out.append(join(macos_dir, rp) / tail)
        # 绝对 rpath 刻意不展开 (非自包含, 展开了也是只在构建机上成立)

    cands: list[Path] = []
    if dep.startswith("@loader_path"):
        cands.append(join(loader_dir, dep))
    elif dep.startswith("@executable_path"):
        cands.append(join(macos_dir, dep))
    elif dep.startswith("@rpath/"):
        tail = dep[len("@rpath/"):]
        for base, rp in rpath_bases:
            expand(base, rp, tail, cands)
    elif dep.startswith("/"):
        return "外部绝对路径, 仅构建机可解析"
    else:
        return "裸文件名依赖, 靠 dyld 兜底搜索 (非自包含)"
    if any(c.is_file() for c in cands):
        return None
    return "包内无解析目标"


def _assert_mac_dylib_resolution(app: Path) -> None:
    """签名前不变量: 包内每个 Mach-O 的每条依赖都必须可解析 —— @rpath/
    @loader_path/@executable_path 展开后命中包内真文件, 或属系统库。

    2026-09-29(i): (g) 提升打断 rpath 算术这类事故, "直跑看开窗"是验不到
    的 —— _hashlib 死了 hashlib 还会静默降级, 开窗/弹码框照常, 直到解锁
    派生 (scrypt) / PDF 渲染 (libmupdf) / 图像解码 (libtiff) 才爆; 必须
    构建期静态把关。@rpath 用"自身 rpath + 主程序 rpath"展开 (bootloader
    实测无 rpath, 全靠各二进制自持)。
    """
    contents = app / "Contents"
    if not contents.is_dir():
        return
    macos_dir = contents / "MacOS"
    main_exe_rpaths: list[str] = []
    exe = _mac_main_executable(app)
    if exe is not None:
        scan = _macho_scan(exe.read_bytes()) if exe.is_file() else None
        if scan is not None:
            main_exe_rpaths = [name for _off, _start, name in scan[2]]
    problems: list[str] = []
    for p, raw in _iter_mac_macho(contents):
        scan = _macho_scan(raw)
        if scan is None:
            continue
        _e, dylibs, rpath_cmds = scan
        rpath_bases = ([(p.parent, name) for _off, _start, name in rpath_cmds]
                       + [(macos_dir, name) for name in main_exe_rpaths])
        for dep in dylibs:
            note = _resolve_mac_dep(dep, p.parent, rpath_bases, macos_dir)
            if note:
                problems.append(f"{p.relative_to(app)} -> {dep} ({note})")
    if problems:
        raise BuildError(
            ".app 依赖解析不自包含:\n  " + "\n  ".join(problems)
            + "\n多为二进制搬移后 rpath 未归一化 —— 请同步最新代码后 "
            "rm -rf dist build 重建")


def _assert_mac_scrypt_ready(app: Path) -> None:
    """解锁链前置不变量 (2026-09-29(i)): 捆绑的 _hashlib 与 libcrypto 都必须
    带 scrypt。

    crypto.derive_data_key 走 hashlib.scrypt, 而标准库 scrypt 只由 _hashlib
    (背后 OpenSSL 3) 提供, 且 _hashlib 加载失败是**静默降级** —— hashlib
    照常 import、sha256 走内置 _sha2 照常, 唯独 scrypt 属性消失。真机表现
    = .app 能开窗、输码后才崩 AttributeError, 与盘/签名/公证全无关, 极易
    误诊。二进制里没有 "scrypt" 方法名字符串 = 编译期就没带 (LibreSSL 系
    Python), 静态可判。
    """
    fw_root = app / "Contents" / "Frameworks"
    if not fw_root.is_dir():
        return
    hashlibs = [p for p in fw_root.rglob("_hashlib*") if p.is_file()]
    if not hashlibs:
        raise BuildError(
            "捆绑产物缺 _hashlib 扩展 —— hashlib 静默降级丢 scrypt, 解锁链"
            "必崩。请同步最新代码后 rm -rf dist build 重建")
    for p in hashlibs:
        if b"scrypt" not in p.read_bytes():
            raise BuildError(
                f"捆绑的 {p.name} 不含 scrypt (编译期即无, 构建环境疑为 "
                f"LibreSSL 系 Python, 如 /usr/bin/python3) —— 请改用 "
                f"OpenSSL 3 系 Python (Homebrew/python.org 官方安装包) 重建"
                f" venv 后清场重建")
    cryptos = [p for p in fw_root.glob("libcrypto*.dylib") if p.is_file()]
    if not cryptos:
        raise BuildError(
            "捆绑产物缺 libcrypto*.dylib —— _hashlib 加载不起来, 解锁链必"
            "崩。请同步最新代码后 rm -rf dist build 重建")
    for p in cryptos:
        if b"scrypt" not in p.read_bytes():
            raise BuildError(
                f"捆绑的 {p.name} 不含 scrypt —— 请换用 OpenSSL 3 系构建环境"
                f" 后清场重建")


def _assert_mac_frameworks_dirs_signable(app: Path) -> None:
    """codesign 前置不变量: Contents/Frameworks 下不许有"名字带点的真目录"
    (唯一豁免 = .framework bundle 本身及其内部; PyInstaller 6 源码 osx.py 同规)。
    违反 = 拍平策略漏网或产物被外部动过 —— 当场熔断, 报出具体路径,
    别让 codesign 替我们报一句难懂的 `bundle format unrecognized`。
    """
    fw_root = app / "Contents" / "Frameworks"
    if not fw_root.is_dir():
        return
    for d in fw_root.rglob("*"):
        if not d.is_dir():
            continue
        rel = d.relative_to(fw_root).parts
        # .framework 目录本身允许带点; 其内部 (Versions/3.14 等) 一并豁免
        if any(part.endswith(".framework") for part in rel):
            continue
        if "." in d.name:
            raise BuildError(
                f"Contents/Frameworks 下存在名字带点的真目录 ({d}) —— codesign "
                f"必拒签 (bundle format unrecognized)。多为符号链接拍平策略漏网, "
                f"请同步最新代码后 rm -rf dist build 重建")


def _assert_mac_resources_code_free(app: Path) -> None:
    """公证前置不变量 (2026-09-29(h)): Contents/Resources 下不许有散装
    Mach-O (.dylib/.so 真文件)。外层 codesign --deep 只签代码位置
    (MacOS/Frameworks/…), Resources 下的散装二进制只封进封条哈希、
    **不当代码签名** → 公证逐条拒 "The binary is not signed with a valid
    Developer ID certificate" + "no secure timestamp"。PyInstaller 布局里
    Resources 侧对代码只放镜像链接 ((h) 规则已全删), 真身落在这 = 布局
    变了或产物被外部动过 —— 当场熔断, 别等公证被拒才发现。
    """
    res_root = app / "Contents" / "Resources"
    if not res_root.is_dir():
        return
    for p in res_root.rglob("*"):
        if p.is_file() and p.suffix in {".dylib", ".so"}:
            raise BuildError(
                f"Contents/Resources 下存在二进制 ({p}) —— codesign --deep "
                f"只签代码位置, 不会签它, 公证必拒。请同步最新代码后 "
                f"rm -rf dist build 重建")


def patch_mac_bundle_metadata(app: Path, name: str) -> None:
    """公证 (notarytool) 硬前提: Info.plist 必须有合法 CFBundleIdentifier
    (仅字母数字-连字符-点, 下划线不合法) 与版本号 —— PyInstaller CLI 无
    bundle_identifier 时落的是占位值, 直接交公证必被拒。必须在签名前写入
    (plist 属于被签内容, 签后再改 = 签名失效)。identifier 跨版本保持稳定,
    Apple 侧才视作同一个 App。
    """
    data = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    data["CFBundleIdentifier"] = "com.usbvault." + re.sub(r"[^A-Za-z0-9.-]", "-", name)
    data["CFBundleShortVersionString"] = VIEWER_VERSION
    data["CFBundleVersion"] = VIEWER_VERSION
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(data))


def sign_artifact(artifact: Path, identity: str | None = None) -> tuple[bool, str]:
    """代码签名: macOS=codesign (默认 ad-hoc "-"); Windows=signtool。

    Windows identity 传 .pfx 路径; 省略时 /a 自动选本机最佳证书
    (需先安装代码签名证书, 设计 14.2: 正式分发签名是硬要求)。
    macOS identity 传 "Developer ID Application: 单位名 (TeamID)" 字符串
    (security find-identity -v -p codesigning 的输出原文), 自动带公证硬前提。
    """
    if sys.platform == "darwin":
        argv = ["codesign", "--force", "--deep"]
        if identity:
            # Developer ID 正式签名: --options runtime (加固运行时, 公证硬前提)
            # + --timestamp (安全时间戳, Apple 对 Developer ID 的硬要求)。
            # ad-hoc 不加 —— 只求 Apple Silicon 可跑, 无公证诉求。
            argv += ["--options", "runtime", "--timestamp"]
        argv += ["--sign", identity or "-", str(artifact)]
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
