"""图书阅览器主程序 (tkinter GUI, PyInstaller 入口脚本)。

启动门禁: 定位自身所在盘 -> 推导密钥 -> 解清单 (失败=非授权盘) ->
有效期 -> 打开次数; 全部通过才进入阅读界面。

防拷贝原则 (软件层面能做的, 边界见 README):
- 内容只进内存、只上屏幕, 不写盘、不调外部程序、无另存/导出/拖出
- 文本禁选中复制, 窗口排除出截屏, PrtScr 吞掉
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    # 开发态注入仓库根 (src/app/*/main.py 同款约定); 打包后由 FrozenImporter 解析
    ROOT_DIR = Path(__file__).resolve().parents[4]
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk

from src.app.usb_vault import config, inspector
from src.app.usb_vault.crypto import CryptoError, decrypt_chunks
from src.app.usb_vault.manifest import FileEntry, Manifest
from src.app.usb_vault.seed import SeedError, load_embedded_seed
from src.app.usb_vault.volume_id import VolumeIdError
from src.app.usb_vault.viewer import mac_protection, renderers, state, win_protection

_MEDIA_ICON = {"text": "📄", "image": "🖼", "pdf": "📑", "binary": "🗃"}
_LOAD_HARD_CAP = 256 * 1024 * 1024  # 单文件载入内存硬上限
_UI_FONT = ("PingFang SC" if sys.platform == "darwin"
            else "Microsoft YaHei UI")  # 各平台中文字体
_CODE_MAX_TRIES = 3  # 解锁码连续错误上限 (防在线穷举)
_WIN_SCREEN_FRAC = 0.75  # 2026-09-19 收件人反馈: 默认窗口随屏幕自适配 (3/4 屏);
# 演进史: 1024×680 → ×1.5 固定 1536×1020 (2026-09-17) → 按屏比例 (大屏更大小屏更小)
_NAV_NAME_W = 128  # 左栏文件名列宽: 320→256 (2026-09-17 缩一档) →128 (2026-09-19 收件人反馈再减半)
_NAV_SIZE_W = 44  # 左栏大小列宽: 90→72 → 44 (保 "12.3MB" 不裁字的可读下限, 合计 328→172 ≈ 减半)

# 解锁码询问回调: 参数为错误提示 (None=首次), 返回用户输入或 None=取消
AskCode = Callable[[str | None], str | None]

# macOS 权限恢复回调: VolumeIdError 时尝试管理员授权读序列号,
# 返回精确序列号或 None (不可用 / 用户拒绝授权) —— 仅 darwin 门禁传入
AdminSerial = Callable[[Path], int | None]

# macOS 挂载根 (模块级常量 = 测试接缝, monkeypatch 指到 tmp 目录)
MAC_VOLUMES_ROOT = Path("/Volumes")


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
        siblings = sorted((p for p in MAC_VOLUMES_ROOT.iterdir() if p.is_dir()),
                          key=lambda p: p.name)
    except OSError:
        siblings = []  # /Volumes 不存在或不可读: 退化为单候选, 门禁照常报错
    candidates += [v for v in siblings
                   if v != own and (v / config.KEYINFO_NAME).is_file()]
    return candidates


def open_manifest_with_code(
    drive: Path, seed: bytes, ask_code: AskCode, *, max_tries: int = _CODE_MAX_TRIES,
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
            return inspector.open_manifest(drive, seed, serial_getter=serial_getter,
                                           unlock_code=code)
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
                return f"读取U盘信息失败: {exc} (管理员授权读取未完成, " \
                       "在终端里启动本程序重试可看到具体原因)"
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
    def _resolve_data_drive(own: Path, seed: bytes,
                            *,
                            serial_getter: inspector.SerialGetter | None = None,
                            ) -> Path:
        """macOS 双入口布局: 从候选卷里找回真正的数据盘 (分区1)。

        逐候选试解清单: 成功、或要求解锁码 (是资料盘且在等码) 即命中;
        非资料盘 / 盘不符 / 读失败换下一个; VolumeIdError (macOS 权限墙)
        记住首个此类候选 —— 全部落空时返回它而不是 own, 让既有的管理员
        授权恢复流程和报错都落在真数据盘上。最多几个 GCM 试解, 只读无副作用。
        """
        volume_id_cand: Path | None = None
        for cand in _mac_data_volume_candidates(own):
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
            except inspector.InspectorError:
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
        try:
            seed = load_embedded_seed()
        except SeedError as exc:
            return f"程序组件缺失, 无法运行: {exc}"
        if sys.platform == "darwin":
            # 双入口布局: .app 所在卷 (分区2) 无载体, 先找回真正的数据盘
            self.drive = self._resolve_data_drive(self.drive, seed)
        result = open_manifest_with_code(self.drive, seed, self._ask_code,
                                         admin_serial=self._mac_admin_serial())
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
        tk.Label(frame, text="此资料已加密锁定, 请输入 8 位解锁码",
                 font=(_UI_FONT, 11, "bold")).pack(anchor="w", pady=(0, 6))
        tk.Label(frame, text="格式: XXXX-XXXX (随U盘另附)", foreground="#666"
                 ).pack(anchor="w", pady=(0, 8))
        if hint:
            tk.Label(frame, text=hint, foreground="#b00").pack(anchor="w", pady=(0, 4))
        var = tk.StringVar()
        box = tk.Frame(frame)
        box.pack(fill="x", pady=(0, 14))
        entry = tk.Entry(box, textvariable=var, show="•", width=14, justify="center",
                         font=(_UI_FONT, 13))
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
        self.geometry("520x220")
        frame = tk.Frame(self, padx=24, pady=24)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="⚠ 无法打开资料", font=(_UI_FONT, 14, "bold")
                 ).pack(anchor="w", pady=(0, 12))
        tk.Label(frame, text=message, wraplength=460, justify="left").pack(anchor="w")
        tk.Button(frame, text="关闭", command=self.destroy).pack(anchor="e", pady=(16, 0))
        self.deiconify()
        self._apply_protections()

    def _apply_protections(self) -> None:
        self.update_idletasks()
        if sys.platform == "darwin":
            mac_protection.apply_window_sharing_protection(self)
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
        self.geometry(default_geometry(self.winfo_screenwidth(),
                                       self.winfo_screenheight()))
        self.minsize(820, 560)

        header = tk.Frame(self, padx=16, pady=10)
        header.pack(fill="x")
        tk.Label(header, text=m.title, font=(_UI_FONT, 16, "bold")
                 ).pack(anchor="w")
        expire_txt = m.expire if m.expire else "永久有效"
        opens_txt = ""
        if m.max_opens is not None:
            used = state.SoftLimitState(self._nonce_hex).load_opens()
            opens_txt = f"  |  剩余打开次数 {max(0, m.max_opens - used)}"
        tk.Label(header, text=f"共 {len(m.files)} 个文件  |  有效期至 {expire_txt}"
                              f"{opens_txt}", foreground="#666").pack(anchor="w")

        main = ttk.Panedwindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=4)

        # 左: 文件树
        tree_side = tk.Frame(main)
        tree = ttk.Treeview(tree_side, columns=("size",), show="tree headings", selectmode="browse")
        tree.heading("#0", text="文件", anchor="w")
        tree.heading("size", text="大小", anchor="e")
        tree.column("#0", width=_NAV_NAME_W, stretch=True)
        tree.column("size", width=_NAV_SIZE_W, stretch=False, anchor="e")
        sb = ttk.Scrollbar(tree_side, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._tree = tree
        self._build_tree(m)
        tree.bind("<Double-1>", lambda _e: self._open_selected())
        tree.bind("<Return>", lambda _e: self._open_selected())

        # 右: 内容区
        content_side = tk.Frame(main)
        self._content = tk.Frame(content_side)
        self._content.pack(fill="both", expand=True)
        self._show_placeholder()

        main.add(tree_side, weight=1)
        main.add(content_side, weight=3)

        status = tk.Label(self, text="双击左侧文件查看  |  资料仅限本查看器阅读, 不支持导出/复制",
                          anchor="w", foreground="#888", padx=12)
        status.pack(fill="x")
        self._status = status

    def _build_tree(self, m: Manifest) -> None:
        dir_nodes: dict[str, str] = {}
        for entry in m.files:
            self._entries[entry.rel_path] = entry
            parts = entry.rel_path.split("/")
            parent = ""
            for i, part in enumerate(parts[:-1]):
                dpath = "/".join(parts[: i + 1])
                if dpath not in dir_nodes:
                    dir_nodes[dpath] = self._tree.insert(parent, "end", text=f"📁 {part}", open=True)
                parent = dir_nodes[dpath]
            icon = _MEDIA_ICON.get(renderers.renderer_kind(entry.media), "🗃")
            label = parts[-1]
            if entry.converted_from:
                label += f" ({entry.converted_from}→PDF)"
            self._tree.insert(parent, "end", iid=entry.rel_path,
                              text=f"{icon} {label}", values=(_human(entry.size),))

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
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            self._render_entry(entry)
        except CryptoError:
            self._mount(tk.Label(self._content,
                                 text="解密失败: 该文件可能已被篡改或损坏。",
                                 foreground="#b00", padx=16, pady=12))
        except Exception as exc:  # noqa: BLE001 UI 兜底, 不能让单文件错误炸掉程序
            self._mount(tk.Label(self._content, text=f"打开失败: {exc}",
                                 foreground="#b00", padx=16, pady=12))
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
            self._mount(renderers.PdfPager(self._content, data))
        note = f"  |  {len(data) / 1e6:.1f}MB" if len(data) > config.LOAD_BYTES_WARN else ""
        self._status.configure(text=f"{entry.rel_path}{note}")

    def _load_bytes(self, entry: FileEntry) -> bytes:
        buf = bytearray()
        enc_path = self.drive / config.DATA_DIR_NAME / f"{entry.id}.enc"
        with open(enc_path, "rb") as fh:
            for chunk in decrypt_chunks(self._data_key, fh):
                buf += chunk
                if len(buf) > _LOAD_HARD_CAP:
                    raise CryptoError(f"文件过大 (超过 {_LOAD_HARD_CAP // 1048576} MiB)")
        return bytes(buf)

    def _mount(self, widget: tk.Widget) -> None:
        widget.pack(fill="both", expand=True)

    def _clear_content(self) -> None:
        for child in self._content.winfo_children():
            child.destroy()

    def _show_placeholder(self) -> None:
        tk.Label(self._content, text="在左侧选择并双击文件开始阅读",
                 foreground="#999").pack(expand=True)


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
