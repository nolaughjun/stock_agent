"""usb_vault 全局配置: 算法参数、魔数、扩展名映射、路径常量。

所有"会被安全设计引用"的数值集中在此, 便于审计。
"""

from __future__ import annotations

from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent

VIEWER_VERSION = "2.3.3"  # 查看器版本 (进 Windows 版本资源, 签名材料)
# 2.2.0 = 2026-09-18 真机循环后的自证版: mac 授权读 rdisk 优先 + 构建期
# tkinter 熔断 + --selfcheck 自检 + 窗口标题带版本号。标题/自检不显示
# 当前版本号的产物一律视为旧部件, 禁止上盘。
# 2.2.1 = admin_read_serial 每条失败路径的原图打 stderr (终端可见)。
# 2.2.2 = 授权 shell 改 mktemp 自述式 (DD_RC/DD_ERR 带回 root 下 dd 真实错误)。
# 2.2.3 = 管理员通道定案 sudo -S (osascript 载体被判死: 其 root shell 对
# /dev 全面 EPERM): 查看器自绘密码框, 密码走 stdin, 输错重问至多 3 次。
# 2.3.0 = 2026-09-18 收件人试点反馈三连: ①"资料查看器"全面改名"图书
# 阅览器" (含 exe/目录/.app/分区2卷标); ②连续缩放取代三档清晰度循环
# (滑杆 25-400% + Ctrl滚轮以鼠标为中心 + ±/100% 按钮, PDF 与图片同套);
# ③交付级收尾 (窗口居中; 图片查看与 PDF 统一画布深底)。卷标正式化
# (数据卷 图书资料) 属烧录操作项, 见 README。
# 2.3.1 = 2.3.0 首用即中: PDF 渲染 DPI 传 float 进 PyMuPDF SWIG 层炸
# TypeError, 且炸在 after 回调里被 tkinter 静默吞 = 收件人看到"打开的
# 内容为空"零症状。修: dpi_for_pct 返回 int; 首帧同步渲染 (异常上浮
# 成"打开失败"可见); 延迟回调 _safe_render 自兜把错误写上画布。
# 2.3.2 = 大书 (扫描件) "未响应/卡顿"根治, 分段计时定案: 单帧渲染全在
# UI 线程同步跑 (A3 扫描件@400% ≈1.7s), PNG 编解码占整帧 80%, 连续滚
# 轮串行数帧即冻结 >5s。修: ①渲染搬工作线程 (最新者胜 + seq 双层取消,
# 主线程只做 PhotoImage 与画布更新); ②PNG 绕路改 frombytes 直构 (实测
# 3.5 倍); ③滚轮与滑杆同走 120ms 防抖 + 爆发起点锚定; ④像素预算 36M
# →16M (A4@400% 全保, 主线程转换最坏 ~0.1s)。
# 2.3.3 = 布局微调 (收件人反馈): ①左栏导航再减半 (列宽 256+72 → 128+44,
# 合计 328→172, 大小列保 12.3MB 不裁字下限); ②默认窗口从固定 1536x1020
# 改为屏幕 3/4 自适配 (大屏自动放大, 小屏自动缩小, 仍居中+640x480 地板)。

# ---------------------------------------------------------------------------
# 密码学参数
# ---------------------------------------------------------------------------

SEED_SIZE = 64  # SEED 字节数 (构建期生成, 嵌入 viewer exe)
SEED_PATH = MODULE_DIR / "secrets" / "seed.bin"
# 每盘解锁码归档 (密码模式, 合同⑤; 同在 gitignore 的 secrets/ 下, 留存≤质保期)
CODE_ARCHIVE_PATH = MODULE_DIR / "secrets" / "unlock_codes.jsonl"

# scrypt: N=2^15, r=8 => 内存 ~32MiB/次; maxmem 放宽到 64MiB 供开销
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 67108864  # 64 MiB

# HKDF info 域 (域分离, 防止子密钥跨用途复用)
HKDF_INFO_FILE = b"usb_vault/file/v1"
HKDF_INFO_MANIFEST = b"usb_vault/manifest/v1"

# 分块加密
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB
GCM_TAG_SIZE = 16

# 魔数 (8 字节对齐, 便于 hexdump 肉眼识别)
MAGIC_KEYINFO = b"UVKI1\x00\x00\x00"  # 盘上明文盐载体
MAGIC_ENCFILE = b"UENC1\x00\x00\x00"  # DATA/<id>.enc
MAGIC_MANIFEST = b"UMFT1\x00\x00\x00"  # manifest.enc

MANIFEST_MAX_PLAINTEXT = 4 * 1024 * 1024  # 清单明文上限 4 MiB

# ---------------------------------------------------------------------------
# 盘上布局
# ---------------------------------------------------------------------------

DATA_DIR_NAME = "DATA"
KEYINFO_NAME = "keyinfo.bin"
MANIFEST_NAME = "manifest.enc"
VIEWER_EXE_NAME = "图书阅览器.exe"  # Windows 查看器 (onefile 产物)
VIEWER_DIR_NAME = "图书阅览器"  # Windows 查看器 (onedir 产物, 杀毒适配默认, 设计 14.2)
VIEWER_MAC_DIR_NAME = "图书阅览器.app"  # macOS 查看器 (onedir .app 目录树, 设计 D3)
# mac 构建产物基名 (ASCII!) —— 2026-09 真机: 中文名 .app 经 zip 中转到 Windows
# 时文件名被按本地码页改写 (旧名"资料查看器"→璧勬枡鏌ョ湅鍣), Info.plist 找不到主程序,
# Mac 上双击无反应。ASCII 内部名对任何解压工具免疫; burn 写盘时拷为上面的中文名。
VIEWER_MAC_BUILD_NAME = "vault_viewer"

# ---------------------------------------------------------------------------
# 双入口分区布局 (--dual-entry 双分区烧录; Windows 烧录机专用, 见 README)
# ---------------------------------------------------------------------------
# 点前缀目录名: Mac Finder 默认不显示点前缀项 => 分区1上的 Windows 入口对
# Mac 收件人不可见; Windows 资源管理器不按点前缀隐藏 => 对 Windows 收件人是
# 正常目录, 双击照常。与 VIEWER_MAC_BUILD_NAME 同一模式: 产物名不变, 落盘改名。
VIEWER_WIN_DIR_NAME_DOTTED = ".图书阅览器"
VIEWER_EXE_NAME_DOTTED = ".图书阅览器.exe"  # 双入口模式下的 onefile 兼容名
# 双入口制式: gpt=正式交付 (默认), mbr=快速验证。2026-09-17 真机 P0 结论:
# macOS **不自动挂载** MBR 0x17 (隐藏 NTFS) 分区 —— 旧版用 0x17 藏分区2 导致
# Mac 收件人根本看不到 .app, 本版起弃用 0x17 藏分区。两制式 Mac 侧都是
# 标准形态 (exFAT + 0x07/GPT 基本数据分区), 按内容探测挂载。
DUAL_SCHEME_GPT = "gpt"
DUAL_SCHEME_MBR = "mbr"
DUAL_SCHEMES: tuple[str, ...] = (DUAL_SCHEME_GPT, DUAL_SCHEME_MBR)
# MBR 制式: 分区2 保持 exFAT 标准类型 0x07, 靠"无盘符"隐藏 (整理性, 非安全
# 边界; 仅烧录机上可靠 —— Windows 对没见过的卷默认自动补盘符, 见 README)
PART2_TYPE_EXFAT = 0x07
# 旧版 (2026-09-16 首版双入口) 藏分区用的类型字节: 保留仅用于识别旧盘并迁移回
# 0x07 (mbr 制式重烧时自动修复, 见 disk_layout.refresh_partition2)
PART2_TYPE_HIDDEN_LEGACY = 0x17
# GPT 制式: 分区2 = Microsoft 基本数据分区 + 无盘符属性。属性写**在盘上**,
# 任何 Windows 机都遵守 (OEM 恢复分区同机制, 微软文档明确"换机生效");
# macOS 不认微软属性位, 按 GPT+exFAT 内容照常挂载。
GPT_BASIC_DATA_TYPE = "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"  # Get-Partition GptType
PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER = 0x8000_0000_0000_0000  # GPT_BASIC_DATA_ATTRIBUTE_NO_DRIVE_LETTER
PART2_SIZE_MIN_MIB = 256  # 第2分区容量下限 (MiB)
PART2_SIZE_MARGIN_RATIO = 1.25  # .app 体积放大系数 (框架升级余量)
PART2_SIZE_MARGIN_MIB = 32  # 额外固定余量 (exFAT 结构开销)
PART2_FREE_MARGIN_MIB = 16  # 复用布局刷新 .app 前的空间预检余量 (MiB)
PART2_LABEL = "图书阅览器Mac"  # 第2分区卷标 (区别于分区1默认"资料", 避免 Mac 双卷重名挂载)

# ---------------------------------------------------------------------------
# 查看器限额 (内存与解码安全)
# ---------------------------------------------------------------------------

TEXT_CAP = 2 * 1024 * 1024  # 文本最多渲染前 2 MiB
IMAGE_MAX_PIXELS = 64_000_000  # Pillow 解码像素上限 (防解压炸弹)
IMAGE_MAX_SIDE = 2560  # 渲染前 thumbnail 上限 (px)
LOAD_BYTES_WARN = 64 * 1024 * 1024  # 单文件载入内存超过此值给出提示

# ---------------------------------------------------------------------------
# 连续缩放 (2026-09-18 收件人反馈: 三档清晰度循环改成任意比例自选)
# ---------------------------------------------------------------------------

ZOOM_MIN_PCT = 25  # 缩放下限 (%)
ZOOM_MAX_PCT = 400  # 缩放上限 (%)
ZOOM_STEP_PCT = 10  # 按钮与 Ctrl+滚轮的步进 (%)
ZOOM_SLIDER_RES_PCT = 5  # 滑杆最小刻度 (%)
PDF_BASE_DPI = 96  # PDF 100% 缩放对应的 DPI 基线 (即旧首档, 习惯连续)
# 单帧渲染像素预算 (PDF 位图/图片缩放共用, 约 4K×4K)。2026-09-19 由 36M
# 收紧: 预算只封"渲染线程的栅格 + 主线程的 PhotoImage 转换"上界, 16M 已
# 全保 A4@400% (14.3M), A3 类大页自动压到 ~300% —— 宁可少放大, 不让收件
# 人电脑卡死。
RENDER_MAX_PIXELS = 16_000_000

# ---------------------------------------------------------------------------
# 扩展名 -> 查看媒体类型
# ---------------------------------------------------------------------------

_TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".py", ".ini", ".xml", ".yaml", ".yml"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
_PDF_EXTS = {".pdf"}
# Office 文档: 烧录端转换为 PDF 后按 pdf 渲染; 未转换则按 binary 处理
OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

EXTENSION_MEDIA_MAP: dict[str, str] = {**{e: "text" for e in _TEXT_EXTS},
                                       **{e: "image" for e in _IMAGE_EXTS},
                                       **{e: "pdf" for e in _PDF_EXTS}}

MEDIA_TYPES = frozenset({"text", "image", "pdf", "binary"})


def classify_media(suffix: str) -> str:
    """按扩展名返回查看媒体类型; 未知类型返回 "binary" (仅显示元数据)。"""
    return EXTENSION_MEDIA_MAP.get(suffix.lower(), "binary")
