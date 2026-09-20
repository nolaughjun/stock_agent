"""usb_vault CLI 入口 (烧录端命令; 收件人只用打包好的查看器)。

用法 (两种运行方式任选其一):
    A. 完整仓库 —— 在**仓库根目录**运行:
    python -m src.app.usb_vault.main burn --source DIR --drive E: --title "项目资料" [--password]
    python -m src.app.usb_vault.main verify --drive E: [--code XXXX-XXXX]
    python -m src.app.usb_vault.main inspect --drive E:
    python -m src.app.usb_vault.main av-scan --target DIR
    python -m src.app.usb_vault.main build-viewer [--onedir默认] [--sign] [--vt-build]
    python -m src.app.usb_vault.main new-seed
    python -m src.app.usb_vault.main view --drive E:   # 发件人本机预览

    B. 单目录模式 —— 只把 src/app/usb_vault/ **完整目录**拷到别的机器
       (如 Mac 构建机, 不需要 src/app 祖先链), 在该目录里运行:
    python3 main.py build-viewer        (其余命令同理)

    需要 Python 3.10+ (推荐 3.12+; Mac Xcode 自带的 python3=3.9 不行,
    入口有版本闸门, 会直接给出安装指引)。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

_PKG_DIR = Path(__file__).resolve().parent

_MIN_PY = (3, 10)  # @dataclass(slots=) 等语法下限; 推荐现场用 3.12+


def _python_version_error(v: tuple[int, ...]) -> str | None:
    """版本闸门: 低版本会在 manifest.py 深处炸出莫名的
    TypeError: dataclass() got an unexpected keyword argument 'slots'
    (2026-09 Mac 真机: Xcode 自带 python3=3.9, 依赖装齐也过不了这堵墙)。
    返回 None = 版本达标, 否则返回退出消息。"""
    if v[:2] >= _MIN_PY:
        return None
    cur = ".".join(str(x) for x in v[:3])
    return (
        f"[usb_vault] 需要 Python {_MIN_PY[0]}.{_MIN_PY[1]}+, 当前 {cur} ({sys.executable})。\n"
        "  低版本会在深处报: TypeError: dataclass() got an unexpected keyword argument 'slots'\n"
        "  Mac 注意: Xcode/开发者工具自带的 python3 就是 3.9 (已停止安全维护),\n"
        "  别往它里面装包 (升级它的 pip 还可能弄坏 Xcode 工具链)。\n"
        "  装 3.12 后重开终端, 再装依赖运行 (Homebrew Python 禁止全局 pip,\n"
        "  依赖须装进 venv, 详见下方 ④):\n"
        "    brew install python@3.12 python-tk@3.12\n"
        "    python3 --version    # 须显示 3.12.x; 仍显示 3.9 = 终端没重开\n"
        "    python3 -m venv .venv && source .venv/bin/activate\n"
        "    python -m pip install pymupdf pyinstaller pillow cryptography\n"
        "    python main.py build-viewer\n"
        "  (或从 python.org 下载安装包, 装好后的命令是 python3.13)"
    )


_vi = sys.version_info
_verr = _python_version_error((_vi[0], _vi[1], _vi[2]))
if _verr:  # pragma: no cover - 开发机/CI 版本达标, 现场旧解释器才触发
    sys.exit(_verr)


def _install_standalone_imports() -> None:
    """单目录模式自举: 只拷了 usb_vault/ 一层时 (2026-09 Mac 真机案例),
    把当前目录挂到 sys.modules 的 src.app.usb_vault 包位上 —— 包内所有模块
    的绝对 import (from src.app.usb_vault.X import Y) 无需改动即可照常工作。"""
    if "src.app.usb_vault" in sys.modules:  # 真实包链在场时让位, 绝不遮蔽
        return
    src_mod = types.ModuleType("src")
    src_mod.__path__ = []  # 占位包, 无真实子目录
    app_mod = types.ModuleType("src.app")
    app_mod.__path__ = []
    sys.modules["src"] = src_mod
    sys.modules["src.app"] = app_mod

    spec = importlib.util.spec_from_file_location(
        "src.app.usb_vault", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)])
    if spec is None or spec.loader is None:  # 防御: __init__.py 丢失 = 拷贝残缺
        raise SystemExit(f"[usb_vault] 目录损坏: 找不到 {_PKG_DIR / '__init__.py'}")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["src.app.usb_vault"] = pkg  # 先注册再 exec, __init__ 内可自引用
    spec.loader.exec_module(pkg)


def _import_error_hint(exc: ModuleNotFoundError) -> str:
    """把自举后仍失败的 ImportError 翻译成人话 (缺文件 / 缺 tkinter / 缺依赖)。"""
    name = exc.name or ""
    if name.startswith("src.app.usb_vault."):
        missing = name.rpartition(".")[2]
        return (
            f"usb_vault 目录拷贝不完整: 缺 {missing}.py (或 viewer/{missing}.py)。\n"
            "  本目录可独立运行, 但必须完整拷贝 src/app/usb_vault/ 的全部内容\n"
            "  (含 viewer/ 子目录与全部 .py 文件)。"
        )
    if name in ("src", "src.app", "src.app.usb_vault"):
        return (
            "包结构不可用 (自举未生效, 正常不应出现)。\n"
            "  两种运行方式任选:\n"
            "  a) 完整仓库, 在仓库根目录:  python3 -m src.app.usb_vault.main <命令>\n"
            "  b) 只拷 usb_vault/ 目录 (须完整), 在该目录里:  python3 main.py <命令>"
        )
    if name == "tkinter":
        return (
            "缺少 tkinter (macOS Homebrew Python 默认不带)。\n"
            "  brew install python-tk@<大版本>   (与你装的 python@<大版本> 对应)"
        )
    return (
        f"缺少第三方依赖: {name}\n"
        "  Homebrew Python 须装进 venv (全局 pip 会被 PEP 668 拒绝):\n"
        "    python3 -m venv .venv && source .venv/bin/activate\n"
        "    python -m pip install pymupdf pyinstaller pillow cryptography"
    )


try:
    from src.app.usb_vault import config, inspector, unlock_code  # noqa: E402
    from src.app.usb_vault.burner import (  # noqa: E402
        BurnError,
        BurnOptions,
        burn,
        burn_dual,
        seed_fingerprint,
    )
    from src.app.usb_vault.office_convert import ConvertError  # noqa: E402
    from src.app.usb_vault.seed import (  # noqa: E402
        SeedError,
        create_seed,
        get_or_create_seed,
        load_seed,
    )
except ModuleNotFoundError:  # 开发机依赖齐全; 仅现场首跑触发, 自举一次再重试
    _install_standalone_imports()  # pragma: no cover
    try:
        from src.app.usb_vault import config, inspector, unlock_code  # noqa: E402
        from src.app.usb_vault.burner import (  # noqa: E402
            BurnError,
            BurnOptions,
            burn,
            seed_fingerprint,
        )
        from src.app.usb_vault.office_convert import ConvertError  # noqa: E402
        from src.app.usb_vault.seed import (  # noqa: E402
            SeedError,
            create_seed,
            get_or_create_seed,
            load_seed,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover
        sys.exit(f"[usb_vault] {exc}\n{_import_error_hint(exc)}")

if TYPE_CHECKING:  # 注解专用, 不进 CLI 的运行时导入图
    from src.app.usb_vault.disk_layout import DiskSummary


def _load_seed_arg(path: str | None):  # type: ignore[no-untyped-def]
    try:
        return load_seed(Path(path)) if path else get_or_create_seed()
    except SeedError as exc:
        sys.exit(f"[seed] {exc}")


def _ask_wipe_confirmation(summary: DiskSummary, *, assume_yes: bool) -> bool:
    """--dual-entry 首烧的清盘确认门: 展示盘面全貌, 输入字面 yes 才放行。

    --yes 是给重复烧录脚本用的显式豁免 (同样打印盘面信息留痕); y/YES/是
    一类"看起来像"的变体一律拒绝 —— 这是全盘清空前的最后一道闸。
    """
    print("即将重新分区, 全盘清空!")
    print(f"  磁盘 {summary.disk_number}: {summary.model} "
          f"({summary.size_bytes / 1024 ** 3:.0f}GB, {summary.bus_type})")
    print(f"  现有盘符: {', '.join(summary.letters) or '无'} —— 该盘全部数据将被删除")
    if assume_yes:
        print("  (--yes 已豁免交互确认)")
        return True
    answer = input('确认清空请输入 "yes" (其他任何输入=取消): ').strip()
    if answer != "yes":
        print("已取消: 盘上数据未做任何修改。")
        return False
    return True


def cmd_burn(args: argparse.Namespace) -> int:
    expire: date | None = None
    if args.expire:
        try:
            expire = date.fromisoformat(args.expire)
        except ValueError:
            sys.exit(f"--expire 格式应为 YYYY-MM-DD, 收到: {args.expire}")
    if args.dual_scheme != config.DUAL_SCHEME_GPT and not args.dual_entry:
        sys.exit("--dual-scheme 仅在 --dual-entry 双分区模式下有意义")
    seed = _load_seed_arg(args.seed)
    print(f"SEED 指纹: {seed_fingerprint(seed)} (须与构建查看器机器的 build-viewer 输出同值)")
    opts = BurnOptions(
        title=args.title,
        expire=expire,
        max_opens=args.max_opens,
        convert_office=not args.no_convert,
        viewer_exe=Path(args.viewer_exe) if args.viewer_exe else None,
        viewer_mac=Path(args.viewer_mac) if args.viewer_mac else None,
        converter=args.converter,
        av_cmd=args.av_cmd,
        skip_scan=args.skip_scan,
        set_label=args.set_label,
        use_password=args.password,
        code_archive=Path(args.code_archive) if args.code_archive else None,
        dual_scheme=args.dual_scheme,
    )
    try:
        if args.dual_entry:
            report = burn_dual(Path(args.source), Path(args.drive), seed, opts,
                               confirm_wipe=lambda s: _ask_wipe_confirmation(
                                   s, assume_yes=args.yes))
        else:
            report = burn(Path(args.source), Path(args.drive), seed, opts)
    except (BurnError, ConvertError) as exc:
        sys.exit(f"[burn] 烧录失败: {exc}")

    print(f"✔ 烧录完成: {report.file_count} 个文件 -> {report.drive}")
    if report.converted:
        print(f"  Office->PDF 转换 {len(report.converted)} 个: {', '.join(report.converted)}")
    if report.office_left_binary:
        print(f"  未转换 (仅元数据展示): {', '.join(report.office_left_binary)}")
    print(f"  明文 {report.total_plain / 1e6:.1f}MB -> 密文 {report.total_enc / 1e6:.1f}MB")
    if report.av_engine:
        secs = report.av_duration_s if report.av_duration_s is not None else 0.0
        print(f"  病毒扫描: {report.av_engine} ({secs:.1f}s, 未检出威胁)")
    else:
        print("  ⚠ 未做烧录前病毒扫描 (--skip-scan 豁免, 自担风险)")
    print(f"  复检: {'全部通过' if report.verify_ok else '未通过!'}")
    if report.viewer_exe_used:
        kind = "目录版" if report.viewer_exe_used.is_dir() else "单文件版"
        if report.dual_entry:  # 分区1 上用点前缀名 (Mac Finder 不显示)
            name = (config.VIEWER_WIN_DIR_NAME_DOTTED if kind == "目录版"
                    else config.VIEWER_EXE_NAME_DOTTED)
        else:
            name = config.VIEWER_DIR_NAME if kind == "目录版" else config.VIEWER_EXE_NAME
        print(f"  Windows 查看器: {name} ({kind}, 已写入分区1)")
    else:
        print("  ⚠ 未写入 Windows 查看器 (收件人将无法阅读!)")
    if report.viewer_mac_used:
        if report.dual_entry:
            where = ("分区2, GPT 无盘符属性 —— 仅 macOS 侧可见"
                     if opts.dual_scheme == config.DUAL_SCHEME_GPT
                     else "分区2, 已摘盘符 (MBR 快速验证制式)")
        else:
            where = "已写入盘根"
        print(f"  macOS 查看器: {config.VIEWER_MAC_DIR_NAME} ({where})")
    for w in report.warnings:
        print(f"  ⚠ {w}")
    if report.unlock_code:
        code_disp = unlock_code.display(report.unlock_code)
        print("=" * 56)
        print("  本盘解锁码 (打印密封交甲方, 码与盘分渠道传递):")
        print(f"      {code_disp}")
        if report.code_archive:
            print(f"  乙方归档: {report.code_archive} (留存≤质保期)")
        print("=" * 56)
        print("  ⚠ 码若遗失, 该盘永久无法阅读 (含乙方, 除非归档仍在)。")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    try:
        from src.app.usb_vault.burner import BurnError, lock_drive  # noqa: E402
    except ModuleNotFoundError as exc:  # pragma: no cover
        sys.exit(f"[usb_vault] {exc}\n{_import_error_hint(exc)}")
    try:
        warnings, count = lock_drive(Path(args.drive))
    except BurnError as exc:
        sys.exit(f"[lock] {exc}")
    print(f"✔ 已锁定 {count} 个文件: 只读属性"
          + " (Windows 端; 密文载体同时隐藏)")
    for w in warnings:
        print(f"  ⚠ {w}")
    print("  提示: 右键盘内文件 -> 属性, 应看到\"只读\"已勾选。")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    seed = _load_seed_arg(args.seed)
    drive = Path(args.drive)
    try:
        report = inspector.verify_drive(drive, seed, unlock_code=args.code)
    except inspector.InspectorError as exc:
        sys.exit(f"[verify] {exc}")
    print(f"《{report.title}》 共 {len(report.results)} 个文件")
    bad = 0
    for r in report.results:
        if r.ok:
            print(f"  ✔ {r.rel_path}")
        else:
            bad += 1
            print(f"  ✘ {r.rel_path}: {r.error or '哈希不符'}")
    print("全部通过 ✔" if report.all_ok else f"{bad} 个文件未通过 ✘")
    return 0 if report.all_ok else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    seed = _load_seed_arg(args.seed)
    # 诊断行 (2026-09 跨平台排障): 密钥绑定卷序列号, 双端各跑一次 inspect
    # 对比此行 —— 不同值 = Mac 与 Windows 读序列号不一致, 解密层恒失败
    # (码再对也没用); Mac /dev 权限被拒时自动降级 diskutil 免特权通道,
    # 显示为两个候选 (GCM 逐一试解, 无需 sudo)
    from src.app.usb_vault import volume_id

    try:
        cands = volume_id.get_volume_serial_candidates(args.drive)
        shown = " 或 ".join(f"{c:#010x}" for c in cands)
        via = "" if len(cands) == 1 else " (diskutil 免特权候选)"
        print(f"卷序列号: {shown}{via} (双端须同值)")
    except volume_id.VolumeIdError as exc:
        print(f"卷序列号: 读取失败 — {exc}")
    try:
        manifest, _ = inspector.open_manifest(Path(args.drive), seed,
                                              unlock_code=args.code)
    except inspector.InspectorError as exc:
        sys.exit(f"[inspect] {exc}")
    print(f"标题: {manifest.title}")
    print(f"创建: {manifest.created_at}")
    print(f"有效期: {manifest.expire or '永久'}")
    print(f"打开次数上限: {manifest.max_opens or '不限'}")
    av = manifest.av_scanned
    print(f"烧录前扫描: {av['engine']} @ {av['time']}" if av
          else "烧录前扫描: 无记录 (--skip-scan 豁免或旧版烧录)")
    print(f"文件 ({len(manifest.files)}):")
    for f in manifest.files:
        origin = f" (源 {f.converted_from})" if f.converted_from else ""
        print(f"  {f.rel_path}  {f.size:,}B  [{f.media}]{origin}")
    return 0


def cmd_av_scan(args: argparse.Namespace) -> int:
    """独立扫描 (设计 13.1): 烧录前对明文目录预检, 也可排查资料是否带毒。"""
    from src.app.usb_vault import av_scan

    target = Path(args.target)
    if not target.is_dir():
        sys.exit(f"[av-scan] 目标必须是目录: {target}")
    try:
        outcome = av_scan.scan_tree(target, av_cmd=args.av_cmd, timeout=args.timeout)
    except av_scan.AVUnavailable as exc:
        sys.exit(f"[av-scan] 无可用引擎 (fail-closed): {exc}")
    except av_scan.AVScanError as exc:
        sys.exit(f"[av-scan] {exc}")
    print(f"引擎: {outcome.engine}  用时 {outcome.duration_s:.1f}s")
    if outcome.clean:
        print("✔ 未检出威胁")
        return 0
    print("✘ 检出威胁:")
    for t in outcome.threats:
        print(f"  - {t}")
    return 2


def cmd_build_viewer(args: argparse.Namespace) -> int:
    from src.app.usb_vault.viewer import build_viewer

    try:
        if args.vt_build:
            seed = build_viewer.VT_FAKE_SEED
            name = args.name or (build_viewer.default_artifact_name() + "-VT自检")
            print("⚠ --vt-build: 使用公开假 SEED, 仅供提交杀毒厂商检测;")
            print("  该构建绝不能用于烧录 (任何人都能解密其盘内容)!")
        else:
            name = args.name
            if args.fresh_seed:
                seed = create_seed(Path(args.seed) if args.seed else None, replace=True)  # type: ignore[arg-type]
                print("已生成新 SEED (注意: 旧 exe 不再能烧兼容盘)")
            else:
                seed_path = Path(args.seed) if args.seed else config.SEED_PATH
                if not seed_path.exists():
                    # 跨机静默新生成 = SEED 不一致元凶 (2026-09 真机: Mac 构建的
                    # .app 解不开烧录机的盘, burn 时才会被内嵌 SEED 比对拦下)
                    print("⚠ 本机没有现成 seed.bin, 将生成全新 SEED ——")
                    print("  若烧录机另有 SEED, 此构建解不开它烧的盘! 先把 seed.bin")
                    print(f"  拷到 {seed_path}, 两边 sha256 核对同值后再构建。")
                seed = _load_seed_arg(args.seed)
        artifact = build_viewer.build(Path(args.out), seed, onefile=args.onefile,
                                      name=name)
    except (SeedError, build_viewer.BuildError) as exc:
        sys.exit(f"[build-viewer] {exc}")
    print(f"✔ 查看器已构建: {artifact}")
    # 自证标记: 用户双机工作流靠 U盘来回拷代码, Mac 侧常是旧拷贝 ——
    # 没这行 = 跑的是旧构建器, 产物必坏; 日期不到 2026-09-17 = 不含 mac
    # 授权读 rdisk 加固 (挂载卷块节点 EBUSY → 收件人开门失败)
    print("  构建器: 2026-09-17 rdisk 加固版")
    print(f"  内嵌 SEED 指纹: {seed_fingerprint(seed)} (烧录端 burn 输出须同值)")
    if sys.platform == "darwin" and not args.vt_build:
        print("  传输到烧录机: cd dist && zip -r vault_viewer.app.zip vault_viewer.app")
        print("  (内部全 ASCII 文件名, 任何解压工具都安全)")
        print(f"  烧录时会自动以中文名 {config.VIEWER_MAC_DIR_NAME} 写到盘上")
    if not args.vt_build and args.sign is not None:
        ok, note = build_viewer.sign_artifact(artifact, args.sign or None)
        if not ok:
            sys.exit(f"[build-viewer] 签名失败: {note}")
        print(f"  签名: {note}")
    if not args.vt_build and args.sign is None:
        print("  (未签名: 正式分发前建议 --sign —— 代码签名是杀毒误报治理的硬要求, 设计 14.2)")
    if args.vt_build:
        print("  VT 自检版: 上传 VirusTotal 评估长尾引擎误报; 真机多引擎测试仍用正式构建")
    return 0


def cmd_new_seed(args: argparse.Namespace) -> int:
    path = Path(args.seed) if args.seed else config.SEED_PATH
    try:
        create_seed(path, replace=True)
    except SeedError as exc:
        sys.exit(f"[new-seed] {exc}")
    print(f"✔ 新 SEED 已写入 {path}")
    print("  请重新 build-viewer; 旧盘不受影响, 但旧 exe 烧不了兼容新盘。")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    from src.app.usb_vault.viewer import app as viewer_app

    viewer_app.run(args.drive)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="usb_vault", description="U盘加密烧录 + 只读查看器")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("burn", help="加密烧录到U盘")
    b.add_argument("--source", required=True, help="源资料目录")
    b.add_argument("--drive", required=True, help="目标卷根, 如 E:")
    b.add_argument("--title", default="资料", help="资料标题 (显示在查看器)")
    b.add_argument("--expire", default=None, help="有效期 YYYY-MM-DD")
    b.add_argument("--max-opens", type=int, default=None, help="打开次数上限 (软限制)")
    b.add_argument("--no-convert", action="store_true", help="不转 Office 为 PDF")
    b.add_argument("--viewer-exe", default=None,
                   help="指定 Windows 查看器 (exe=单文件 或 目录=onedir)")
    b.add_argument("--viewer-mac", default=None, help="指定 macOS 查看器 .app 目录")
    b.add_argument("--converter", default=None,
                   choices=("win32com", "applescript", "soffice"),
                   help="Office->PDF 转换后端 (默认按平台自动选)")
    b.add_argument("--av-cmd", default=None,
                   help="自定义病毒扫描引擎命令模板, {path} 为目标占位")
    b.add_argument("--skip-scan", action="store_true",
                   help="跳过烧录前病毒扫描 (fail-closed 的显式豁免, 自担风险)")
    b.add_argument("--seed", default=None, help="指定 SEED 文件")
    b.add_argument("--set-label", action="store_true", help="把卷标设为标题")
    b.add_argument("--password", action="store_true",
                   help="密码模式: 每盘生成随机解锁码 (打印密封交甲方, 乙方归档; 合同⑤)")
    b.add_argument("--code-archive", default=None, metavar="PATH",
                   help=f"解锁码归档文件 (默认 {config.CODE_ARCHIVE_PATH})")
    b.add_argument("--dual-entry", action="store_true",
                   help="双分区模式: 分区2只放 Mac 查看器并隐藏盘符, "
                        "需管理员终端; 首烧全盘清空, 交互确认")
    b.add_argument("--dual-scheme", choices=config.DUAL_SCHEMES,
                   default=config.DUAL_SCHEME_GPT, metavar="SCHEME",
                   help="双分区制式: gpt (默认, 交付级 —— 无盘符属性写盘上, "
                        "换 Windows 机也不分配盘符) / mbr (快速验证 —— 仅烧录机"
                        "上可靠隐藏, 其他 Windows 机首插可能自动补盘符)")
    b.add_argument("--yes", action="store_true",
                   help="配合 --dual-entry: 跳过清盘的交互确认 (危险: 全盘清空)")
    b.set_defaults(func=cmd_burn)

    lk = sub.add_parser("lock", help="对已烧录U盘补加/恢复 只读+隐藏 (免重烧)")
    lk.add_argument("--drive", required=True, help="目标盘卷根, 如 E: 或 /Volumes/盘名")
    lk.set_defaults(func=cmd_lock)

    v = sub.add_parser("verify", help="全量复检盘内容")
    v.add_argument("--drive", required=True)
    v.add_argument("--seed", default=None)
    v.add_argument("--code", default=None, help="解锁码 (密码模式的盘)")
    v.set_defaults(func=cmd_verify)

    i = sub.add_parser("inspect", help="查看盘上清单")
    i.add_argument("--drive", required=True)
    i.add_argument("--seed", default=None)
    i.add_argument("--code", default=None, help="解锁码 (密码模式的盘)")
    i.set_defaults(func=cmd_inspect)

    avs = sub.add_parser("av-scan", help="独立病毒扫描 (烧录前预检明文目录)")
    avs.add_argument("--target", required=True, help="要扫描的目录")
    avs.add_argument("--av-cmd", default=None, help="自定义引擎模板 ({path} 占位)")
    avs.add_argument("--timeout", type=float, default=1800.0, help="超时秒数")
    avs.set_defaults(func=cmd_av_scan)

    bv = sub.add_parser("build-viewer", help="构建查看器 (默认 onedir, 杀毒适配更好)")
    bv.add_argument("--out", default=str(config.MODULE_DIR / "dist"))
    bv.add_argument("--seed", default=None)
    bv.add_argument("--fresh-seed", action="store_true", help="同时轮换新 SEED")
    bv.add_argument("--onefile", action="store_true",
                    help="单文件 exe (仅 Windows; 启动慢且误报率高, 不建议)")
    bv.add_argument("--name", default=None,
                    help="产物名 (默认: Windows=图书阅览器, macOS=vault_viewer/ASCII名)")
    bv.add_argument("--vt-build", action="store_true",
                    help="假 SEED 自检构建, 仅用于提交杀毒厂商, 绝不烧盘")
    bv.add_argument("--sign", default=None, nargs="?", const="", metavar="IDENTITY",
                    help="构建后签名: Win=signtool (给 .pfx 或省略自动选); Mac=codesign (默认 ad-hoc)")
    bv.set_defaults(func=cmd_build_viewer)

    ns = sub.add_parser("new-seed", help="轮换 SEED")
    ns.add_argument("--seed", default=None)
    ns.set_defaults(func=cmd_new_seed)

    vw = sub.add_parser("view", help="本机预览查看器 (发件人自测)")
    vw.add_argument("--drive", required=True)
    vw.set_defaults(func=cmd_view)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
