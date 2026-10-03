"""图书阅览器主程序 (tkinter GUI, PyInstaller 入口脚本)。

启动门禁: 定位自身所在盘 -> 推导密钥 -> 解清单 (失败=非授权盘) ->
有效期 -> 打开次数; 全部通过才进入阅读界面。

防拷贝原则 (软件层面能做的, 边界见 README):
- 内容只进内存、只上屏幕, 不写盘、不调外部程序、无另存/导出/拖出
- 文本禁选中复制, 窗口排除出截屏, PrtScr 吞掉
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    # 开发态注入仓库根 (src/app/*/main.py 同款约定); 打包后由 FrozenImporter 解析
    ROOT_DIR = Path(__file__).resolve().parents[4]
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont, messagebox, simpledialog, ttk

from src.app.usb_vault import config, inspector
from src.app.usb_vault.crypto import CryptoError, decrypt_chunks
from src.app.usb_vault.manifest import FileEntry, Manifest
from src.app.usb_vault.seed import SeedError, load_embedded_seed
from src.app.usb_vault.volume_id import VolumeIdError, get_drive_type
from src.app.usb_vault.viewer import loadtimer, mac_protection, renderers, state, win_protection

_MEDIA_ICON = {"text": "📄", "image": "🖼", "pdf": "📑", "binary": "🗃"}
_LOAD_HARD_CAP = 256 * 1024 * 1024  # 单文件载入内存硬上限
_UI_FONT = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"  # 各平台中文字体
_CODE_MAX_TRIES = 3  # 解锁码连续错误上限 (防在线穷举)
_WIN_SCREEN_FRAC = 0.75  # 2026-09-19 收件人反馈: 默认窗口随屏幕自适配 (3/4 屏);
# 演进史: 1024×680 → ×1.5 固定 1536×1020 (2026-09-17) → 按屏比例 (大屏更大小屏更小)
_NAV_NAME_W = 128  # 左栏文件名列宽基准: 320→256 (2026-09-17 缩一档) →128 (2026-09-19 收件人
# 反馈再减半)。v2.4.3 起此值仅为 dpi_scale=1 的基准: 实际宽 = 按最长文件名
# 实测自适应 (renderers.nav_name_width), 不再赌定值 —— Surface 200% 缩放下
# 128px 装不下 4 个汉字 (2026-09-23 真机反馈"列表显示不完全"的根因)。
_NAV_SIZE_W = 44  # 左栏大小列宽基准: 90→72 → 44 (保 "12.3MB" 不裁字; 实际 ×ui_scale)

# 解锁码询问回调: 参数为错误提示 (None=首次), 返回用户输入或 None=取消
AskCode = Callable[[str | None], str | None]

# macOS 权限恢复回调: VolumeIdError 时尝试管理员授权读序列号,
# 返回精确序列号或 None (不可用 / 用户拒绝授权) —— 仅 darwin 门禁传入
AdminSerial = Callable[[Path], int | None]

# macOS 挂载根 (模块级常量 = 测试接缝, monkeypatch 指到 tmp 目录)
MAC_VOLUMES_ROOT = Path("/Volumes")

# 阅览器脱离配套U盘执行 (全机找不到任何 keyinfo 载体) 的门禁文案 (v2.4.4,
# 用户定稿原文) —— 拷到本机硬盘/其他盘后双击即见此句, 取代旧版先扫全机
# 卷、再拿自身卷硬开清单后弹出的"该盘不是资料盘: 缺少 keyinfo.bin"。
# v2.4.5 起单一事实源在 config: 书名引导壳 (launcher_entry.py, 无 tkinter
# 环境) 与此处共用同一句, 两处文案永不漂移。
DETACHED_MESSAGE = config.DETACHED_MESSAGE


def _mac_data_volume_candidates(own: Path) -> list[Path]:
    """数据卷候选序: 自身卷优先, 其余按名序补含载体的兄弟卷。

    双入口布局下 .app 在分区2, 载体 (keyinfo.bin) 在分区1 —— 自身卷没有
    keyinfo, 必须扫 /Volumes 兄弟卷。单分区布局自身卷就有, 候选序退化为
    [自身] 开头, 行为与旧版一致 (向后兼容)。判据只看 keyinfo 是否存在,
    不预解析内容 —— 真假数据盘的仲裁交给 _resolve_data_drive 的 GCM 验证。
    """
    candidates: list[Path] = []
    if (own / config.KEYINFO_NAME).is_file():
        candidates.append(own)
    try:
        siblings = sorted(
            (p for p in MAC_VOLUMES_ROOT.iterdir() if p.is_dir()), key=lambda p: p.name
        )
    except OSError:
        siblings = []  # /Volumes 不存在或不可读: 退化为单候选, 门禁照常报错
    candidates += [v for v in siblings if v != own and (v / config.KEYINFO_NAME).is_file()]
    return candidates


def _exe_on_removable_media() -> bool:
    """v2.4.8 拷离介质闸门判据: 自身 exe 是否运行在可移动介质上。

    背景 (2026-09-29 用户实测曝光 v2.4.4 边界): 整目录拷走阅览器到电脑
    后双击, 只要原盘同机插着, _resolve_data_drive 的全机卷扫描仍能找到
    原盘加密链照常开门 —— 旧门禁只拦"全机无载体"一种形态。故判据不看
    载体在不在, 只看"自己运行在哪": 电脑硬盘上的副本一律谢幕, 原盘在
    不在场都一样。

    仅冻结构建 (sys.frozen) 启用: 源码运行 = 烧录机 view 预览 (在固定盘
    上), 恒放行。Windows = GetDriveType(自身盘)==removable; macOS =
    自身卷挂在外置介质 (mac_volume.is_on_external_media, diskutil
    Internal=false)。判据读不出按 False (fail-closed: 宁可在真棒排障,
    不在电脑硬盘上开门)。其余平台的冻结构建不存在, 保守拒绝。
    """
    if not getattr(sys, "frozen", False):
        return True  # 源码运行 = 烧录机 dev 预览 (view 命令), 不设卡
    try:
        if sys.platform == "win32":
            return get_drive_type(sys.executable) == "removable"
        if sys.platform == "darwin":
            from src.app.usb_vault import mac_volume

            return mac_volume.is_on_external_media(sys.executable)
    except Exception:  # noqa: BLE001 —— 判据失败 = fail-closed 谢幕
        return False
    return False


def _win_data_volume_candidates(own: Path) -> list[Path]:
    """Windows 数据卷候选序 (v2.4.0 反转布局): 自身卷 → 无盘符卷 (GUID 根)
    → 其余带盘符卷, 只收 keyinfo 命中的 (与 macOS 候选同一契约)。

    自身卷有载体 = 单分区盘, 退化为 [自身], 行为与旧版一致; 自身卷无载体
    (双入口盘: 阅览器在分区1, 加密链在无盘符分区2) 才扫全机卷。GUID 枚举
    与逐卷 OSError 容错 (如 ESP 拒访) 在 win_volumes 内消化。
    """
    if sys.platform != "win32":
        return []
    from src.app.usb_vault import win_volumes

    return win_volumes.data_volume_candidates(own)


def open_manifest_with_code(
    drive: Path,
    seed: bytes,
    ask_code: AskCode,
    *,
    max_tries: int = _CODE_MAX_TRIES,
    serial_getter: inspector.SerialGetter | None = None,
    admin_serial: AdminSerial | None = None,
) -> tuple[Manifest, bytes] | str:
    """带解锁码重试的清单打开 (启动门禁的核心, 独立成函数便于单测)。

    非密码盘不会触发询问; 密码盘: 码错/盘不对在解密层不可区分, 统一
    按"码可能错"重试, 连续 max_tries 次失败后停止。
    admin_serial (macOS 专用): 读盘失败且用户授权管理员权限时拿到精确
    序列号, 换上后 continue 重试 —— 已输入的解锁码原样复用, 不再问一次。
    返回 (manifest, data_key) 或给用户看的失败文案。
    """
    code: str | None = None
    tries = 0
    while True:
        try:
            if serial_getter is None:
                return inspector.open_manifest(drive, seed, unlock_code=code)
            return inspector.open_manifest(
                drive, seed, serial_getter=serial_getter, unlock_code=code
            )
        except inspector.PasswordRequiredError:
            code = ask_code(None)
            if code is None:
                return "未输入解锁码, 无法打开资料。"
        except inspector.WrongDriveError as exc:
            if code is not None and tries < max_tries:
                tries += 1
                code = ask_code("解锁码错误或非本盘资料, 请重试")
                if code is None:
                    return "已取消输入, 解锁失败。"
                continue
            if code is not None:
                return f"解锁码连续验证未通过 ({max_tries} 次): {exc}"
            return "仅限指定的U盘使用: 此盘内容与授权不符 (拷贝到其他U盘/硬盘无法阅读)。"
        except inspector.NotAVaultError as exc:
            return f"该盘不是资料盘: {exc}"
        except VolumeIdError as exc:
            # VolumeIdError 不是 InspectorError 子类, 不接住会带着堆栈穿出
            # __init__ —— --windowed 下进程直接消失, 表现成"输码后闪退"。
            # macOS 权限墙 (2026-09-15 真机: /dev EPERM + exFAT 无 VolumeUUID)
            # 的兜底: 用户点授权、输一次管理员密码, 拿到精确序列号后重试
            if admin_serial is not None:
                recovered = admin_serial(drive)
                if recovered is not None:

                    def _getter(_p: Path) -> int:
                        return recovered

                    serial_getter = _getter
                    continue
                # 走到这 = 授权读取未成功 (取消/密码放弃/osascript 报错),
                # 具体原因 admin_read_serial 已打到 stderr —— 终端启动可见
                return (
                    f"读取U盘信息失败: {exc} (管理员授权读取未完成, "
                    "在终端里启动本程序重试可看到具体原因)"
                )
            return f"读取U盘信息失败: {exc}"
        except inspector.InspectorError as exc:
            return f"读取资料失败: {exc}"


def default_geometry(screen_w: int, screen_h: int) -> str:
    """默认窗口几何: 屏幕的 3/4, 居中弹出 (2026-09-19 起随屏幕自适配)。

    大屏自动放大、小屏自动缩小, 不再是固定 1536x1020; 仍留边距给
    任务栏/菜单栏, 极小屏落回地板值 640x480, 坐标夹 0 不出屏。
    """
    w = int(screen_w * _WIN_SCREEN_FRAC)
    h = int(screen_h * _WIN_SCREEN_FRAC)
    w = max(640, min(w, screen_w - 60))
    h = max(480, min(h, screen_h - 120))
    x = max(0, (screen_w - w) // 2)
    y = max(0, (screen_h - h) // 2 - 20)  # 略上移: 给底部任务栏/Dock 让位
    return f"{w}x{h}+{x}+{y}"


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _entry_label(entry: FileEntry) -> str:
    """清单条目在左栏的显示名 (v2.4.3 抽出: 树构建与列宽自适应共用口径)。"""
    label = entry.rel_path.rsplit("/", 1)[-1]
    if entry.converted_from:
        label += f" ({entry.converted_from}→PDF)"
    return label


class ViewerApp(tk.Tk):
    """只读图书阅览器。"""

    def __init__(self, drive: Path | None = None) -> None:
        super().__init__()
        self.drive = self._locate_drive(drive)
        self._entries: dict[str, FileEntry] = {}
        self._manifest: Manifest | None = None
        self._data_key = b""
        self._nonce_hex = ""
        # 以下三个在 _build_ui 里创建, 此处仅声明类型
        self._tree: ttk.Treeview
        self._content: tk.Frame
        self._status: tk.Label
        # Phase 0 装载计时探针窗 (USBVAULT_LOAD_TIMER=1 时首次开书才建)
        self._probe_text: tk.Text | None = None

        self.withdraw()  # 门禁通过前不露窗口内容
        fail = self._startup_gate()
        if fail is not None:
            self._show_fatal(fail)
            return
        assert self._manifest is not None
        self._build_ui()
        self.deiconify()
        self._apply_protections()
        # 打开次数软限制: 门禁已过, 现在计数
        if self._manifest.max_opens is not None:
            st = state.SoftLimitState(self._nonce_hex)
            st.record_open()

    # ------------------------------------------------------------------ 门禁

    @staticmethod
    def _locate_drive(drive: Path | None) -> Path:
        """定位自身所在卷根 (绝不能用 _MEIPASS, 那在收件人本机临时目录)。

        Windows: exe 锚点即盘根; macOS: .app 树在 /Volumes/<卷名> 下, 沿
        父目录上溯找到 /Volumes 的直接子目录 (设计 D2)。
        """
        if drive is not None:
            return Path(drive)
        if sys.platform == "darwin":
            exe = Path(sys.executable)  # .app 内可执行文件路径本身就是绝对真路径
            for parent in exe.parents:
                if parent.parent == Path("/Volumes"):
                    return parent
            return Path(exe.anchor or "/")
        return Path(Path(sys.executable).anchor or ".")

    @staticmethod
    def _resolve_data_drive(
        own: Path,
        seed: bytes,
        *,
        serial_getter: inspector.SerialGetter | None = None,
    ) -> Path | None:
        """双入口布局: 从候选卷里找回真正的数据盘 (加密链所在卷)。

        逐候选试解清单: 成功、或要求解锁码 (是资料盘且在等码) 即命中;
        非资料盘 / 盘不符 / 读失败换下一个; VolumeIdError (macOS 权限墙)
        记住首个此类候选 —— 全部落空时返回它而不是 own, 让既有的管理员
        授权恢复流程和报错都落在真数据盘上。单分区盘首个候选即自身卷,
        直接命中, 行为与旧版一致。最多几个 GCM 试解, 只读无副作用。
        候选来源按平台: macOS = /Volumes 兄弟卷, Windows = win_volumes
        的 GUID 枚举 (无盘符分区2 的正主); OSError (如 ESP 拒访) 同样
        跳过换下一个。
        返回 None (v2.4.4) = 全机找不到任何 keyinfo 载体 —— 阅览器被
        拷离了配套U盘, 门禁直接给"脱离U盘介质"文案, 不再拿自身卷硬开
        清单 (旧版会弹"该盘不是资料盘: 缺少 keyinfo.bin"误导收件人)。
        """
        if sys.platform == "darwin":
            candidates = _mac_data_volume_candidates(own)
        else:
            candidates = _win_data_volume_candidates(own)
        if not candidates:
            return None  # 脱离介质: 本机没有任何卷携带加密链载体
        volume_id_cand: Path | None = None
        for cand in candidates:
            try:
                if serial_getter is None:
                    inspector.open_manifest(cand, seed)
                else:
                    inspector.open_manifest(cand, seed, serial_getter=serial_getter)
                return cand
            except inspector.PasswordRequiredError:
                return cand  # 是资料盘且等码: 命中, 后续输码流程接管
            except VolumeIdError:
                if volume_id_cand is None:
                    volume_id_cand = cand
            except (inspector.InspectorError, OSError):
                continue  # 非资料盘 / 序列号不符 / 读失败: 换下一个候选
        return volume_id_cand if volume_id_cand is not None else own

    def _mac_admin_serial(self) -> AdminSerial | None:
        """macOS 权限恢复通道 (仅 darwin 启用): 自绘密码框 → sudo -S 代读。

        2026-09-15 真机结论: 收件人账户读 /dev 恒 EPERM, exFAT 又拿不到
        diskutil VolumeUUID —— 管理员授权是唯一可靠序列号通道。
        2026-09-18 四连探针判死 osascript 系统授权框 (其 root shell 对
        /dev 全面 EPERM), 改走与终端 sudo 完全相同的机制: 本窗口自绘
        密码框 (文案解释用途), 密码经 stdin 喂 sudo, 不进命令行。
        密码输错最多重问 3 次; 点取消 = 放弃, 返回 None 走原报错文案。
        """
        if sys.platform != "darwin":
            return None
        from src.app.usb_vault.mac_volume import (
            AdminAuthRejected,
            admin_read_serial,
        )

        def _reader(drive: Path) -> int | None:
            for _attempt in range(3):
                password = simpledialog.askstring(
                    "需要管理员密码",
                    "macOS 规定: 读取U盘硬件序列号需要管理员权限。\n\n"
                    "请输入本机的管理员密码 (仅用于读取U盘信息,\n"
                    "不会修改U盘和电脑上的任何内容)。",
                    show="•",
                    parent=self,
                )
                if password is None:
                    return None  # 用户取消
                try:
                    serial = admin_read_serial(drive, password)
                except AdminAuthRejected as exc:
                    messagebox.showerror(
                        "密码未通过",
                        f"sudo 未接受该密码: {exc}\n请重试 (或点取消停止)。",
                        parent=self,
                    )
                    continue
                return serial
            return None

        return _reader

    def _startup_gate(self) -> str | None:
        """返回 None=通过; 否则返回给用户看的失败文案。"""
        # v2.4.8 拷离介质闸门放最前: 拷到电脑的整目录副本即使钥匙齐全、
        # 原盘同机在场, 也直接谢幕, 不进全机扫描 (v2.4.4 只拦"全机无
        # 载体", 原盘插着时拷贝件照样开门 —— 2026-09-29 用户实测曝光)。
        if not _exe_on_removable_media():
            return DETACHED_MESSAGE
        try:
            seed = load_embedded_seed()
        except SeedError as exc:
            return f"程序组件缺失, 无法运行: {exc}"
        # 双入口布局 (v2.4.0 反转): 自身卷 (阅览器分区) 无载体时先找回真正
        # 的数据盘 (无盘符分区2); 单分区盘首个候选即自身卷, 直接命中不变。
        # 找不到任何载体 (v2.4.4) = 阅览器被拷离配套U盘执行, 直接谢幕
        resolved = self._resolve_data_drive(self.drive, seed)
        if resolved is None:
            return DETACHED_MESSAGE
        self.drive = resolved
        result = open_manifest_with_code(
            self.drive, seed, self._ask_code, admin_serial=self._mac_admin_serial()
        )
        if isinstance(result, str):
            return result
        manifest, data_key = result

        from datetime import date

        if state.check_expiry(manifest.expire, date.today()):
            return f"资料已过期 (有效期至 {manifest.expire}), 无法查看。"

        nonce = b""
        if manifest.max_opens is not None:
            try:
                nonce = inspector.read_keyinfo(self.drive).nonce
            except inspector.InspectorError:
                nonce = b""  # 打开次数是软限制, nonce 拿不到就退化为不计数
            st = state.SoftLimitState(nonce.hex())
            if st.load_opens() >= manifest.max_opens:
                return f"已达打开次数上限 ({manifest.max_opens} 次)。"
        self._manifest = manifest
        self._data_key = data_key
        self._nonce_hex = nonce.hex()
        return None

    def _ask_code(self, hint: str | None = None) -> str | None:
        """解锁码输入对话框; 返回原始输入 (规范化/校验交给 open_manifest) 或 None=取消。"""
        dlg = tk.Toplevel(self)
        dlg.title("解锁")
        dlg.resizable(False, False)
        dlg.grab_set()
        frame = tk.Frame(dlg, padx=24, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text="此资料已加密锁定, 请输入 8 位解锁码", font=(_UI_FONT, 11, "bold")
        ).pack(anchor="w", pady=(0, 6))
        tk.Label(frame, text="格式: XXXX-XXXX (随U盘另附)", foreground="#666").pack(
            anchor="w", pady=(0, 8)
        )
        if hint:
            tk.Label(frame, text=hint, foreground="#b00").pack(anchor="w", pady=(0, 4))
        var = tk.StringVar()
        box = tk.Frame(frame)
        box.pack(fill="x", pady=(0, 14))
        entry = tk.Entry(
            box, textvariable=var, show="•", width=14, justify="center", font=(_UI_FONT, 13)
        )
        entry.pack(fill="x")

        # 小眼睛 (悬在输入栏右端): 默认掩码, 点击切换明文 —— 对照纸质码输入用
        def toggle_eye(_event: tk.Event) -> str:
            shown = entry.cget("show") == ""
            entry.configure(show="" if not shown else "•")
            eye.configure(text="🙈" if not shown else "👁")
            return "break"

        eye = tk.Label(box, text="👁", cursor="hand2", font=(_UI_FONT, 11))
        eye.place(relx=1.0, rely=0.5, x=-8, anchor="e")
        eye.lift()
        eye.bind("<Button-1>", toggle_eye)
        entry.focus_set()
        result: list[str | None] = [None]

        def ok() -> None:
            result[0] = var.get()
            dlg.destroy()

        def cancel() -> None:
            result[0] = None
            dlg.destroy()

        btns = tk.Frame(frame)
        btns.pack(anchor="e")
        tk.Button(btns, text="取消", command=cancel).pack(side="left", padx=4)
        tk.Button(btns, text="解锁", command=ok).pack(side="left", padx=4)
        dlg.bind("<Return>", lambda _e: ok())
        dlg.bind("<Escape>", lambda _e: cancel())
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        self.wait_window(dlg)
        return result[0]

    # ------------------------------------------------------------------ UI

    def _show_fatal(self, message: str) -> None:
        # 标题带版本: 双击后一眼可辨产物新旧 (不显示当前版本号 = 旧部件, 禁止再排查)
        self.title(f"图书阅览器 v{config.VIEWER_VERSION}")
        s = renderers.ui_scale(self)  # v2.4.3: 失败对话框同随 DPI (高分屏可读)
        self.geometry(f"{round(520 * s)}x{round(220 * s)}")
        frame = tk.Frame(self, padx=24, pady=24)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="⚠ 无法打开资料", font=(_UI_FONT, 14, "bold")).pack(
            anchor="w", pady=(0, 12)
        )
        tk.Label(frame, text=message, wraplength=round(460 * s),
                 justify="left").pack(anchor="w")
        tk.Button(frame, text="关闭", command=self.destroy).pack(anchor="e", pady=(16, 0))
        self.deiconify()
        self._apply_protections()

    def _apply_protections(self) -> None:
        self.update_idletasks()
        if sys.platform == "darwin":
            # v2.4.11: 全窗口 + 常驻重刷 (旧版只护 mainWindow 一次,
            # 微信截屏实锤穿透; 见 mac_protection 模块头)
            mac_protection.start_window_protection(self)
            seqs = renderers.COPY_SWALLOW_SEQS_MAC
        else:
            win_protection.apply_window_protection(self.winfo_id())
            win_protection.suppress_printscreen(self)
            seqs = renderers.COPY_SWALLOW_SEQS
        # 全局吞复制键 (renderers 里对具体控件还有一层); 个别序列在特定平台
        # Tk 不识别时只跳过 —— 防护降级好过查看器崩启动 (2026-09-14 真机事故)
        for seq in seqs:
            try:
                self.bind_all(seq, lambda _e: "break")
            except tk.TclError:
                continue

    def _build_ui(self) -> None:
        assert self._manifest is not None
        m = self._manifest
        self.title(f"图书阅览器 v{config.VIEWER_VERSION} - {m.title}")
        self.geometry(default_geometry(self.winfo_screenwidth(), self.winfo_screenheight()))
        # v2.4.3: 所有裸像素尺寸随 ui_scale (与字体同一 DPI 源)。高分屏
        # (Surface 类 150%~200%) 下字体随点数放大而定值像素不变, 是"左栏
        # 列表显示不完全"的根因; 960×560 为 dpi_scale=1 基准。
        s = renderers.ui_scale(self)
        self._ui_s = s
        self.minsize(round(960 * s), round(560 * s))

        header = tk.Frame(self, padx=16, pady=10)
        header.pack(fill="x")
        tk.Label(header, text=m.title, font=(_UI_FONT, 16, "bold")).pack(anchor="w")
        expire_txt = m.expire if m.expire else "永久有效"
        opens_txt = ""
        if m.max_opens is not None:
            used = state.SoftLimitState(self._nonce_hex).load_opens()
            opens_txt = f"  |  剩余打开次数 {max(0, m.max_opens - used)}"
        tk.Label(
            header,
            text=f"共 {len(m.files)} 个文件  |  有效期至 {expire_txt}{opens_txt}",
            foreground="#666",
        ).pack(anchor="w")

        # v2.4.3: ttk.Panedwindow → tk.PanedWindow —— ttk 的 sash 只有几
        # 像素且无样式抓手, 触屏 (约 1mm 物理宽) 根本抓不住 = "无法调整";
        # tk 版 sashwidth 随 scale 加宽且凸起可见。stretch=never/always:
        # 窗口增宽全给正文, 左栏恒保持用户选定的宽度
        main = tk.PanedWindow(self, orient="horizontal",
                              sashwidth=max(6, round(6 * s)),
                              sashrelief="raised", sashpad=1)
        main.pack(fill="both", expand=True, padx=8, pady=4)
        self._main = main

        # 左: 文件树 (顶部加 ◀/▶ 宽度按钮 —— 触屏第一通道, sash 是补充)
        tree_side = tk.Frame(main)
        self._tree_side = tree_side
        nav_bar = tk.Frame(tree_side)
        nav_bar.pack(side="top", fill="x", pady=(0, 2))
        tk.Label(nav_bar, text="资料目录", foreground="#666").pack(
            side="left", padx=(6, 2))
        tk.Button(nav_bar, text="◀", width=2,
                  command=lambda: self._nav_nudge(-1)).pack(side="right")
        tk.Button(nav_bar, text="▶", width=2,
                  command=lambda: self._nav_nudge(1)).pack(side="right")
        tree_wrap = tk.Frame(tree_side)
        tree_wrap.pack(side="top", fill="both", expand=True)
        tree = ttk.Treeview(tree_wrap, columns=("size",), show="tree headings", selectmode="browse")
        tree.heading("#0", text="文件", anchor="w")
        tree.heading("size", text="大小", anchor="e")
        # 列宽: 名字列按最长文件名实测自适应 (夹 [128,320]×scale), 大小列
        # 保 "12.3MB" 不裁字; 都乘 ui_scale —— 定值列宽在高分屏下装不下
        # 已放大的字体, 硬裁断正是本次真机反馈
        base_font = tkfont.nametofont("TkDefaultFont")
        longest = max(
            (base_font.measure(f"{_MEDIA_ICON.get(renderers.renderer_kind(e.media), '🗃')}"
                               f" {_entry_label(e)}") for e in m.files),
            default=0,
        )
        name_w = renderers.nav_name_width(longest, s)
        size_w = round(_NAV_SIZE_W * s)
        tree.column("#0", width=name_w, minwidth=round(96 * s), stretch=True)
        tree.column("size", width=size_w, stretch=False, anchor="e")
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._tree = tree
        self._nav_w = name_w + size_w + round(34 * s)  # + 滚动条与边距
        self._build_tree(m)
        tree.bind("<Double-1>", lambda _e: self._open_selected())
        tree.bind("<Return>", lambda _e: self._open_selected())

        # 右: 内容区
        content_side = tk.Frame(main)
        self._content = tk.Frame(content_side)
        self._content.pack(fill="both", expand=True)
        self._show_placeholder()

        # (探针实证 Tk 8.6.15 的 add 不支持 -pad 选项, 留白靠 sashpad)
        main.add(tree_side, width=self._nav_w, minsize=round(140 * s),
                 stretch="never")
        main.add(content_side, minsize=round(420 * s), stretch="always")

        status = tk.Label(
            self,
            text="双击左侧文件查看  |  资料仅限本查看器阅读, 不支持导出/复制",
            anchor="w",
            foreground="#888",
            padx=12,
        )
        status.pack(fill="x")
        self._status = status

    def _nav_nudge(self, direction: int) -> None:
        """左栏宽度步进调节 (◀/▶): 触屏第一通道 —— sash 在触屏上难抓,
        按钮目标尺寸随字体放大, 任何缩放下都好点。夹在 [140, 520]×scale。"""
        s = renderers.ui_scale(self)
        step = round(48 * s)
        lo, hi = round(140 * s), round(520 * s)
        new = min(hi, max(lo, self._nav_w + direction * step))
        if new == self._nav_w:
            return
        self._nav_w = new
        self._main.paneconfigure(self._tree_side, width=new)  # type: ignore[no-untyped-call]

    def _build_tree(self, m: Manifest) -> None:
        dir_nodes: dict[str, str] = {}
        for entry in m.files:
            self._entries[entry.rel_path] = entry
            parts = entry.rel_path.split("/")
            parent = ""
            for i, part in enumerate(parts[:-1]):
                dpath = "/".join(parts[: i + 1])
                if dpath not in dir_nodes:
                    dir_nodes[dpath] = self._tree.insert(
                        parent, "end", text=f"📁 {part}", open=True
                    )
                parent = dir_nodes[dpath]
            icon = _MEDIA_ICON.get(renderers.renderer_kind(entry.media), "🗃")
            self._tree.insert(
                parent,
                "end",
                iid=entry.rel_path,
                text=f"{icon} {_entry_label(entry)}",
                values=(_human(entry.size),),
            )

    # ------------------------------------------------------------------ 打开文件

    def _open_selected(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        rel = sel[0]
        entry = self._entries.get(rel)
        if entry is None:
            return  # 目录节点
        self._clear_content()
        if loadtimer.enabled():
            self._ensure_probe_window()
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            self._render_entry(entry)
        except CryptoError:
            self._mount(
                tk.Label(
                    self._content,
                    text="解密失败: 该文件可能已被篡改或损坏。",
                    foreground="#b00",
                    padx=16,
                    pady=12,
                )
            )
        except Exception as exc:  # noqa: BLE001 UI 兜底, 不能让单文件错误炸掉程序
            self._mount(
                tk.Label(
                    self._content, text=f"打开失败: {exc}", foreground="#b00", padx=16, pady=12
                )
            )
        finally:
            self.configure(cursor="")

    def _render_entry(self, entry: FileEntry) -> None:
        kind = renderers.renderer_kind(entry.media)
        if kind == "binary":
            self._mount(renderers.render_binary_meta(self._content, entry))
            self._status.configure(text=f"{entry.rel_path} — 此类型不支持打开, 仅显示信息")
            return
        data = self._load_bytes(entry)
        if kind == "text":
            text, truncated = renderers.truncate_text(data)
            self._mount(renderers.render_text(self._content, text, truncated=truncated))
        elif kind == "image":
            self._mount(renderers.render_image(self._content, data))
        else:  # pdf
            self._mount(
                renderers.PdfPager(self._content, data,
                                   probe_label=_entry_label(entry))
            )
        note = f"  |  {len(data) / 1e6:.1f}MB" if len(data) > config.LOAD_BYTES_WARN else ""
        self._status.configure(text=f"{entry.rel_path}{note}")

    def _load_bytes(self, entry: FileEntry) -> bytes:
        # Phase 0 探针: TimedReader 只包 read, 把读盘耗时/吞吐从"读+解密"
        # 循环总时长里拆出来 (关 = 走原句柄, 零额外开销)
        enc_path = self.drive / config.DATA_DIR_NAME / f"{entry.id}.enc"
        raw = open(enc_path, "rb")
        reader = loadtimer.TimedReader(raw) if loadtimer.enabled() else None
        buf = bytearray()
        t_loop0 = time.perf_counter()
        with (reader if reader is not None else raw) as fh:
            for chunk in decrypt_chunks(self._data_key, fh):
                buf += chunk
                if len(buf) > _LOAD_HARD_CAP:
                    raise CryptoError(f"文件过大 (超过 {_LOAD_HARD_CAP // 1048576} MiB)")
        t_loop = time.perf_counter() - t_loop0
        t_copy0 = time.perf_counter()
        data = bytes(buf)
        if reader is not None:
            loadtimer.log(
                f"[{_entry_label(entry)}] 装载 "
                f"读盘 {reader.read_s:.2f}s "
                f"({reader.read_bytes / 1048576:.1f}MB, {reader.mbps():.1f}MB/s) | "
                f"解密+拼接 {t_loop - reader.read_s:.2f}s | "
                f"整本拷贝 {time.perf_counter() - t_copy0:.2f}s"
            )
        return data

    def _ensure_probe_window(self) -> None:
        """探针窗 (首次开书时建一次): 追加 loadtimer 输出行, 只显示不计时。

        行内容只有耗时/体积元数据, 无书内容; 复制键已被全局 bind_all 吞。
        关闭 = 隐藏不销毁 (Text 永远活着, sink 回调不会再触碰已销毁部件)。
        """
        if self._probe_text is not None:
            return
        win = tk.Toplevel(self)
        win.title("装载计时 (Phase 0 探针)")
        win.geometry(f"{round(860 * renderers.ui_scale(self))}x{round(200 * renderers.ui_scale(self))}")
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        body = tk.Frame(win)
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, state="disabled", wrap="none",
                      font=("Consolas" if sys.platform == "win32" else "Menlo", 10))
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def append(line: str) -> None:
            txt.configure(state="normal")
            txt.insert("end", line + "\n")
            txt.see("end")
            txt.configure(state="disabled")

        for prev in loadtimer.history():
            append(prev)
        loadtimer.set_sink(append)
        self._probe_text = txt

    def _mount(self, widget: tk.Widget) -> None:
        widget.pack(fill="both", expand=True)

    def _clear_content(self) -> None:
        for child in self._content.winfo_children():
            child.destroy()

    def _show_placeholder(self) -> None:
        tk.Label(self._content, text="在左侧选择并双击文件开始阅读", foreground="#999").pack(
            expand=True
        )


def run(drive: str | Path | None = None) -> None:
    """入口: run() 供 CLI 调用; 打包 exe 直接执行 __main__ 块。"""
    win_protection.set_dpi_awareness()
    app = ViewerApp(None if drive is None else Path(drive))
    app.mainloop()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        from src.app.usb_vault.viewer.selfcheck import run_selfcheck

        raise SystemExit(run_selfcheck())
    run()
