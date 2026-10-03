"""usb_vault 全局配置: 算法参数、魔数、扩展名映射、路径常量。

所有"会被安全设计引用"的数值集中在此, 便于审计。
"""

from __future__ import annotations

from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent

VIEWER_VERSION = "2.4.11"  # 查看器版本 (进 Windows 版本资源, 签名材料)
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
# 2.3.4 = 防截屏失效根治 (收件人真机实测曝光): v2.3.3 及之前 Windows 侧
# SetWindowDisplayAffinity 一直设在 Tk winfo_id() 返回的内层子 HWND 上,
# 系统静默拒绝、返回值又无人检查 → 防护从未生效, 微信截图/FSCapture 录屏
# 都能拍到阅览器内容, 手册"黑块"承诺落空。修: 经 GetAncestor(GA_ROOT)
# 取顶层窗口再设置; 失败打 stderr 不再静默。探针实证: 对顶层 HWND 设
# WDA_EXCLUDEFROMCAPTURE 后 GDI BitBlt 与 PrintWindow 采样均黑
# (GetWindowDisplayAffinity 回读 0x11 确认); 不设时采样为纯红。
# 2.4.0 = 双分区布局反转 (上级检查"盘插上看起来必须干净"): 分区1 (可见,
# 有盘符, 卷标 图书资料) = Windows + Mac 双阅览器; 分区2 (GPT 无盘符属性,
# Windows 不显示) = 加密链全套 (keyinfo/manifest/DATA, +H+S)。Windows 阅
# 览器经 \\?\Volume{GUID}\ 路径普通权限读分区2 (2026-09-22 探针: 无盘符
# NTFS 卷非提权 iterdir/读文件成功; EFI 分区的拒绝属微软引导分区专项加固,
# exFAT 数据分区不在其列)。旧版 (分区2=.app) 布局重烧时按内容判别自动全盘
# 重建。注意: 无盘符/GPT 属性是**整理性隐藏**, "不可读"由密文+序列号绑定+
# 解锁码保证 (安全边界不变); Mac 侧分区2仍会挂载 (卷标 资料存储, 内容为
# 密文), Windows 真机 F: 棒复测列入验证手册。
# 2.4.1 = PDF 页码跳转 (2026-09-23 用户需求, 大部头翻页刚需): 工具栏页码
# 框输页码回车/点"跳转"直达; 解析走 pdf_page_jump 纯函数 (中文输入法全角
# 数字 NFKC 归一, 越界不夹不猜, 只提示范围)。改了查看器行为必升版本号
# (2.4.0 未交付但已烧上测试棒, 同号不同物违反"版本号=产物身份"红线)。
# 2.4.2 = PDF 页面缩略图 (2026-09-23 用户需求, 依截图样例): PdfPager 内嵌
# 左侧缩略图面板 (单列卡片 + 第N页标签 + 当前页高亮联动 + 点击跳页 + 可
# 关闭, 工具栏"缩略图"勾选恢复)。缩略图渲染与主视图共用同一渲染线程的
# 后台通道 (doc 线程封闭不破, 前台翻页/缩放帧恒优先); 可视区懒加载 +
# 缓存上限淘汰, 大部头 700 页不炸内存。2.4.1 本未构建, 并号在逻辑上可行,
# 但无法排除期间已有 2.4.1 产物流出 —— 号便宜歧义贵, 一律升号。
# (2026-09-23 探针: Windows Tk canvas 20 万 px scrollregion 坐标准完好,
# 700 页 × ~204px ≈ 14 万 px 在界内; macOS 大坐标列入真机验证。)
# 2.4.3 = 高分屏 (Surface 类二合一, 150%~200% 缩放) 左栏不可用根治
# (2026-09-23 用户真机反馈"列表显示不完全, 且无法调整"): 根因 = DPI 感知
# 声明后字体随点数 ×DPI 放大, 而列宽/sash/滑杆全是裸像素不放大 —— 200%
# 下列宽 128px 只装 4 个汉字, sash 几像素触屏抓不住。修: ①全 UI 裸像素
# 统一乘 ui_scale (与字体同一 DPI 源 winfo_fpixels, 任何缩放等比);
# ②ttk.Panedwindow → tk.PanedWindow (sashwidth 随 scale 加宽, 可见可抓);
# ③左栏顶部 ◀/▶ 宽度按钮 (触屏目标够大, 步进 48×scale); ④左栏初宽按
# 最长文件名实测宽度自适应 (夹 [128,320]×scale)。缩略图面板同乘 scale,
# 缓存上限按 scale² 反压缩 (位图内存不随缩放翻倍)。
# 2.4.4 = 拷离U盘的阅览器谢幕文案 (2026-09-23 用户定稿): 阅览器被拷到
# 其他电脑/硬盘后双击, 旧版先扫全机卷、再拿自身卷硬开清单, 弹"该盘不是
# 资料盘: 缺少 keyinfo.bin"误导收件人。修: _resolve_data_drive 候选为空
# (全机无任何 keyinfo 载体) 即返回 None, 门禁直接给"脱离U盘介质，加密
# 书稿无法在该设备上打开查看" (原文照录), 且先于解锁码弹框。
# 2.4.5 = 单拷书名 exe 谢幕根治 (2026-09-23 用户复测: 把双入口目录内的
# 《欧阳竟无全集》《桂伯华全集》.exe 单独拷到别处后双击, 弹的是
# "Failed to load Python DLL ... 找不到指定模块" 而非中文谢幕文案)。
# 根因: onedir 主 exe 被单独拷离后死在 PyInstaller C 装载器层, Python
# 尚未启动, 2.4.4 的应用层门禁没有机会跑。修: 双入口目录内的书名 exe
# 改为微型自包含引导壳 (onefile, 仅 ctypes/subprocess 依赖): 目录完整
# (旁有真主程序 VIEWER_EXE_NAME + _internal) 时转交真主程序 (参数透传);
# 残缺时直接弹 DETACHED_MESSAGE。真主程序保持构建名不再改名 (引导壳按名
# 查找, 缺失即烧录熔断)。整目录拷贝仍走 2.4.4 应用层门禁不变。
# 2.4.6 = 真棒事故修复 (2026-09-23 18:15 烧录的交付棒, 双击书名 exe 竟弹
# "脱离U盘介质"): 真主程序 图书阅览器.exe 顶着**一代旧名**裸露在书名 exe
# 旁, 烧录后被当旧版残留清掉 (烧录时 _install_dual_launcher 校验过其在
# 盘, 事后消失; Defender 记录为空, 人工清理嫌疑最大)。修: ①双入口目录内
# 真主程序 +H+S 隐藏 (与 _internal 同待遇; 仅当书名引导壳在旁时才藏 ——
# 单分区 onedir 的 图书阅览器.exe 仍是唯一可见入口, 不藏); ②引导壳区分
# 谢幕语境: _internal 在而真主程序缺 = 盘上文件不完整, 弹
# BROKEN_INSTALL_MESSAGE 而非误导性的"脱离U盘介质"。此版推翻 v2.4.5
# "真主程序保持可见"的权衡: 被删是已发生的事故, dropper 启发式是未发生的
# 理论风险, 且正式交付走代码签名。
# 2.4.7 = Mac 侧观感隐藏 (用户问"MAC系统的加密分区部分可以隐藏嘛"): 分区
# 级"不挂载"在 Mac 无等效物 (阅览器靠 /Volumes 挂载点普通权限读分区2, 不
# 挂载=只能提权裸读; GPT 无盘符属性是 Windows 专属语义)。落两层点前缀:
# ①分区2卷标 资料存储→.资料存储 —— macOS 对点前缀挂载点不显示 (Finder
# 桌面/边栏), 但照常自动挂载, 阅览器扫 /Volumes 不看卷名照常命中; 卷标从
# 此是 Mac 侧藏匿机制, 烧录设置失败由"仅告警"升为熔断。②密文链文件名
# DATA/keyinfo.bin/manifest.enc → .DATA/.keyinfo.bin/.manifest.enc ——
# macOS 不认 DOS 隐藏位 (+H+S 只在 Windows 生效), 点前缀是 Mac 侧唯一
# 文件级观感隐藏。换名=换代: 复用判据只认新名 (旧名密文链重烧走全盘重建),
# 旧名残留由 _clear_own_artifacts 按 *_LEGACY 名清扫。
# 2.4.8 = 拷离介质闸门 (2026-09-29 用户实测曝光 v2.4.4 边界): 整目录拷走
# 阅览器到电脑后双击, 只要原盘同机插着, _resolve_data_drive 的全机卷扫描
# 仍能找到原盘加密链照常开门 —— 2.4.4 只拦"全机无载体"一种形态。修:
# _startup_gate 最前端加自身介质判据 (app._exe_on_removable_media), 仅对
# 冻结构建 (sys.frozen) 启用 (烧录机 view 源码预览不受影响): Windows =
# GetDriveType(自身盘)==removable; macOS = 自身卷挂在外置介质
# (mac_volume.is_on_external_media, diskutil Internal=false)。判不出按
# 不在可移动介质处理 (fail-closed 谢幕, 文案同 DETACHED_MESSAGE)。残余
# 边界: 拷到另一支可移动盘且原盘同机在场仍可读 (介质判据判不出, 堵它需
# 阅览器与加密链同物理盘校验, 未做)。前置红线: 换交付棒型必须先验
# DriveType=removable / diskutil Internal=false, 否则真棒会被误谢幕。
# 2.4.9 = 介质闸门 darwin 判据三级级联 (2026-09-30 真机事故: macOS 26 的
# NTFS 走 fskit 挂载, 卷级 diskutil info --plist 缺 Internal 键 —— 同一支
# 棒文本输出明明 Device Location: External / Removable, 旧单通道判据读不
# 出即 VolumeIdError, fail-closed 把真棒错杀, U盘上双击阅览器直接弹
# "脱离U盘介质")。修: is_on_external_media 改级联 (挂载点 plist 的
# Internal → 同源旁证 DeviceLocation=external → mount 反查设备节点问切片
# 与整盘), 全通道灭才抛; selfcheck 的 ✗ 文案同步带出各通道原因, 现场
# 截图即自诊断。收件人视角行为不变 (真棒照开、拷贝件照谢幕)。
# 2.4.10 = diskutil 探测双写法 (-plist/--plist) + 收敛单一探测点 (2026-10-01
# 真机复测推翻 2.4.9 的机制推断: 收件 Mac 上 v2.4.9 自检三通道死因全是
# "diskutil 退出码 1: Usage: diskutil info[rmation] [-plist]" —— 连纯 ASCII
# 的 /dev/disk4 都拒, 说明不是"plist 缺 Internal 键"、与卷名/编码无关, 是
# 该 macOS 26 的 diskutil 拒收双横线 --plist 写法 (其用法自述即单横线)。
# 级联三通道共用同一写法 = 公共单点故障全灭。修: 全模块唯一探测点
# _diskutil_plist 按 (-plist, --plist) 双写法探测 (单横线是该机用法文本
# 自载形态且历来各版通用, 失败补双横线兜旧机型); 介质判据级联 / 免特权
# UUID 通道 / 管理员授权框的设备定位 (_device_identifier, 原先靠 mount
# 反查兜底才没炸) 三处调用点全部改走它)。教训: 级联的通道必须真正独立,
# 共用同一底层调用的多通道只是同一个单点。
# 2.4.11 = macOS 防截屏根治 (2026-10-01 收件 Mac 微信截屏实锤穿透): 旧
# mac_protection 只把 mainWindow 设一次 NSWindowSharingNone + 4×400ms
# 重试预算 —— U盘冷启动慢于预算即静默裸奔, 解锁码框等 Toplevel 各有
# NSWindow 更罩不全, 失败无日志自检无核验 (v2.3.3 Windows 防截屏"静默
# 失效"事故换平台复发)。修: protect_all_windows 对 NSApp.windows 逐窗
# 设置+回读核验 (不信设置调用); start_window_protection 常驻每 2s 重刷
# (后开对话框自动纳入), 状态恶化打 stderr; selfcheck 加"防截屏"行在冻结
# 产物里实证核验。微信截屏走 CGWindowList 管线即被挡, 真机实测列入交付
# 验证 (README); 手机翻拍物理屏仍属方案边界 (与 Windows 同)。

# ---------------------------------------------------------------------------
# 查看器通用文案 / 书名引导壳 (v2.4.5)
# ---------------------------------------------------------------------------

# 拷离介质谢幕文案 (2026-09-23 用户原文照录): 应用层门禁 (app.py) 与书名
# 引导壳 (launcher_entry.py) 共用 —— 单一事实源, 两处文案永不漂移。
DETACHED_MESSAGE = "脱离U盘介质，加密书稿无法在该设备上打开查看"
# 盘上阅览器文件不完整 (书名引导壳在、依赖树在、真主程序被删/被杀毒隔离):
# 这是坏盘不是拷离, 文案必须可分辨, 否则误导排障方向 (v2.4.6 真棒事故)。
BROKEN_INSTALL_MESSAGE = "阅览器文件不完整，请联系发件方重新烧录U盘"
# 书名引导壳构建产物基名 (ASCII, 与 VIEWER_MAC_BUILD_NAME 同一模式):
# 壳是 onefile 自包含 exe, burn 时拷入双入口目录并改书名
# (VIEWER_INNER_EXE_NAME_DUAL); 中文名产物同样有跨机码页改写风险。
VIEWER_LAUNCHER_BUILD_NAME = "vault_launcher"

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

# v2.4.7 起点前缀: macOS 不认 DOS 隐藏位, +H+S 只在 Windows 生效; 点前缀
# 是 Mac 侧唯一的文件级观感隐藏 (Finder/ls 默认不显示)。Windows 侧观感不
# 变 (本就 +H+S)。阅览器/烧录/检视全链走本组常量, 换名即换代。
DATA_DIR_NAME = ".DATA"
KEYINFO_NAME = ".keyinfo.bin"
MANIFEST_NAME = ".manifest.enc"
# v2.4.0~v2.4.6 的明文名: 不再落盘, 仅作清扫 (单分区旧盘重烧不留残链) 与
# 复用判别 (见新名对) 用 —— 同查看器历史名 *_DOTTED_LEGACY 的治理模式。
DATA_DIR_NAME_LEGACY = "DATA"
KEYINFO_NAME_LEGACY = "keyinfo.bin"
MANIFEST_NAME_LEGACY = "manifest.enc"
VIEWER_EXE_NAME = "图书阅览器.exe"  # Windows 查看器 (onefile 产物)
VIEWER_DIR_NAME = "图书阅览器"  # Windows 查看器 (onedir 产物, 杀毒适配默认, 设计 14.2)
VIEWER_MAC_DIR_NAME = "图书阅览器.app"  # macOS 查看器 (onedir .app 目录树, 设计 D3)
# mac 构建产物基名 (ASCII!) —— 2026-09 真机: 中文名 .app 经 zip 中转到 Windows
# 时文件名被按本地码页改写 (旧名"资料查看器"→璧勬枡鏌ョ湅鍣), Info.plist 找不到主程序,
# Mac 上双击无反应。ASCII 内部名对任何解压工具免疫; burn 写盘时拷为上面的中文名。
VIEWER_MAC_BUILD_NAME = "vault_viewer"
# 2026-10-02 双架构: Intel 侧构建产物基名。两次构建 (arm64 原生 / arch -x86_64
# 转译) 的 PyInstaller 产物同名 vault_viewer.app, Intel 侧在 Mac 上公证装订后
# 改名 vault_viewer_intel.app 再 zip —— 同一空目录解压两个 zip 不互撞, 烧录
# 机 dist/ 两个产物并存可辨。改名发生在 staple 之后 (目录名不进签名封条, 无害)。
VIEWER_MAC_BUILD_NAME_INTEL = "vault_viewer_intel"

# ---------------------------------------------------------------------------
# 双入口分区布局 (--dual-entry 双分区烧录; Windows 烧录机专用, 见 README)
# ---------------------------------------------------------------------------
# 双入口落盘名 (2026-09-23 用户三次定稿): 书名前缀 + 全角（适配Windows系统/
# 适配MAC系统）后缀 —— 收件人打开分区1一眼可辨两个入口各属哪个平台、装的
# 是什么内容。命名已三代更替 (点前缀 → 半角 (WIN)/(MAC) → 全角（WIN/MAC）
# → 本版), 历史名见下方 *_LEGACY。产物名不变, 落盘改名 (与
# VIEWER_MAC_BUILD_NAME 同一模式)。
# 注: 用户指令原文 .app 名带尾随空格, 落盘名不保留 —— Windows Win32 路径层
# 会剥掉尾随空格/点, "看着叫这名实际叫别的"是歧义之源。
VIEWER_WIN_DIR_NAME_DUAL = "《欧阳竟无全集》《桂伯华全集》图书阅览器（适配Windows系统）"
VIEWER_EXE_NAME_DUAL = "《欧阳竟无全集》《桂伯华全集》图书阅览器（适配Windows系统）.exe"  # onefile 兼容名
# 双入口盘 (目录版) 目录内主程序名 (同日用户定稿): 目录名 = 书名+阅览器,
# 目录内可执行程序 = 纯书名。构建产物内主程序名恒为 VIEWER_EXE_NAME,
# burn 落盘后改名 (onedir 内 exe 可自由改名, 不影响 PyInstaller 运行)。
VIEWER_INNER_EXE_NAME_DUAL = "《欧阳竟无全集》《桂伯华全集》.exe"
# 2026-10-02 双架构交付 (烧录侧, 查看器不升号): Mac 入口按芯片一分为二 ——
# Apple 芯片构建机产 arm64 瘦构建, Intel 版用 python.org universal2 Python
# 经 `arch -x86_64` 起的 venv 产 x86_64 瘦构建 (双构建均为瘦二进制, 拍平/
# rpath/封条手术链零改动 —— _MACHO_MAGICS 本就含两种架构)。收件人按
# 「关于本机 → 芯片/处理器」对号入座; 名字待用户定稿可换 (config 一行改,
# 落盘名烧录侧赋予, 查看器无需重构)。
VIEWER_MAC_DIR_NAME_DUAL_APPLE = "《欧阳竟无全集》《桂伯华全集》图书阅览器（适配MAC-Apple芯片）.app"
VIEWER_MAC_DIR_NAME_DUAL_INTEL = "《欧阳竟无全集》《桂伯华全集》图书阅览器（适配MAC-Intel芯片）.app"
# 旧版双入口名: 保留仅用于清扫/回收旧盘遗留 (同 PART2_TYPE_HIDDEN_LEGACY
# 模式), 绝不再作为新落盘名。
VIEWER_MAC_DIR_NAME_DUAL_LEGACY = "《欧阳竟无全集》《桂伯华全集》图书阅览器（适配MAC系统）.app"  # 2026-10-02 双架构拆分前的单 Mac 入口名
VIEWER_WIN_DIR_NAME_FULLWIDTH_LEGACY = "《欧阳竟无全集》《桂伯华全集》图书阅览器（WIN）"  # 二代 全角后缀
VIEWER_EXE_NAME_FULLWIDTH_LEGACY = "《欧阳竟无全集》《桂伯华全集》图书阅览器（WIN）.exe"
VIEWER_MAC_DIR_NAME_FULLWIDTH_LEGACY = "《欧阳竟无全集》《桂伯华全集》图书阅览器（MAC）.app"
VIEWER_WIN_DIR_NAME_SUFFIXED_LEGACY = "图书阅览器(WIN)"  # 2026-09-23 第一版 半角后缀
VIEWER_EXE_NAME_SUFFIXED_LEGACY = "图书阅览器(WIN).exe"
VIEWER_MAC_DIR_NAME_SUFFIXED_LEGACY = "图书阅览器(MAC).app"
VIEWER_WIN_DIR_NAME_DOTTED_LEGACY = ".图书阅览器"  # 2026-09-22 及之前 点前缀
VIEWER_EXE_NAME_DOTTED_LEGACY = ".图书阅览器.exe"
# 双入口制式: gpt=正式交付 (默认), mbr=快速验证。2026-09-17 真机 P0 结论:
# macOS **不自动挂载** MBR 0x17 (隐藏 NTFS) 分区 —— 旧版用 0x17 藏分区2 导致
# Mac 收件人根本看不到 .app, 本版起弃用 0x17 藏分区。两制式 Mac 侧都是
# 标准形态 (NTFS + 0x07/GPT 基本数据分区; 2026-09-28 起卷为 NTFS), 按内容
# 探测挂载。
DUAL_SCHEME_GPT = "gpt"
DUAL_SCHEME_MBR = "mbr"
DUAL_SCHEMES: tuple[str, ...] = (DUAL_SCHEME_GPT, DUAL_SCHEME_MBR)
# MBR 制式: 分区2 保持 IFS 标准类型 0x07 (exFAT/NTFS 通用同一字节), 靠"无盘符"
# 隐藏 (整理性, 非安全边界; 仅烧录机上可靠 —— Windows 对没见过的卷默认自动补
# 盘符, 见 README)。2026-09-28 起卷内容为 NTFS, 类型字节不变。
PART2_TYPE_EXFAT = 0x07
# 旧版 (2026-09-16 首版双入口) 藏分区用的类型字节: 保留仅用于识别旧盘并迁移回
# 0x07 (mbr 制式重烧时自动修复, 见 disk_layout.refresh_partition2)
PART2_TYPE_HIDDEN_LEGACY = 0x17
# GPT 制式: 分区2 = Microsoft 基本数据分区 + 无盘符属性。属性写**在盘上**,
# 任何 Windows 机都遵守 (OEM 恢复分区同机制, 微软文档明确"换机生效");
# macOS 不认微软属性位, 按 GPT 基本数据分区内容照常挂载 (2026-09-28 起
# 卷为 NTFS, macOS 原生只读挂载 —— 收件机表现待真机核验)。
GPT_BASIC_DATA_TYPE = "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}"  # Get-Partition GptType
PART2_GPT_ATTRIBUTE_NO_DRIVE_LETTER = (
    0x8000_0000_0000_0000  # GPT_BASIC_DATA_ATTRIBUTE_NO_DRIVE_LETTER
)
# 分区1 (阅览器分区) 容量参数: 由双阅览器体积推算, v2.4.0 反转布局起
# 分区1只装阅览器、分区2 (剩余全部) 装加密链 —— 旧名 PART2_SIZE_*。
VIEWER_PART_MIN_MIB = 1024  # 分区1容量下限 (MiB) —— 2026-09-22 用户定稿: 固定 1 GiB 底线, 框架升级余量直接给足
VIEWER_PART_MARGIN_RATIO = 1.25  # 阅览器体积放大系数 (框架升级余量)
VIEWER_PART_MARGIN_MIB = 32  # 额外固定余量 (exFAT 结构开销)
VIEWER_PART_FREE_MARGIN_MIB = 16  # 复用布局重写阅览器前的空间预检余量 (MiB)
PART2_LABEL = ".资料存储"  # 分区2 (加密链) 卷标: v2.4.7 起点前缀 —— macOS 对
# 点前缀挂载点不显示 (Finder 桌面/边栏), 但照常自动挂载在 /Volumes/.资料存储,
# 阅览器扫 /Volumes 不看卷名照常命中; Windows 侧因无盘符属性本就不显示。
# 卷标从此是 Mac 侧藏匿机制: 落空则 Mac 桌面多出一个 Untitled 空卷,
# burn_dual 里设置失败不再是"仅告警"而是熔断 (待 Mac 真机核验 Finder 行为)。

# ---------------------------------------------------------------------------
# NTFS 防误删 deny ACE 配方 (2026-09-28 用户评审通过, 探针矩阵定案)
# ---------------------------------------------------------------------------
# 交付棒 2026-09-28 起 P1+P2 全转 NTFS: 防误删不再靠 +R 只读位 (2026-09-20
# 探针实证 Explorer/SHFileOperationW 会剥只读位再删), 改靠内核访问检查层的
# deny ACE。Windows 删除的充要条件 = 文件 DELETE **或** 父目录
# FILE_DELETE_CHILD, 得一即可 —— 故必须两级都拒 (探针实测: 只拒文件时六路
# 删除全穿, 父通道兜底删掉; 文件干净+仅拒父目录时文件通道照删)。
# *S-1-1-0 = Everyone 的 SID 形态 (免本地化组名), ACE 落盘为卷元数据, 换机
# 免配置生效。只用 DE/DC 两个 specific 位, 绝不碰 generic 位 (2026-09-15
# --acl 教训: generic 位连读带执行一起拒)。施加/清障/回读在 ntfs_guard.py。
DEL_DENY_SID = "*S-1-1-0"
DEL_DENY_TREE_SPEC = "*S-1-1-0:(OI)(CI)(DE,DC)"  # 交付树根: 继承罩全树 (后落盘文件也罩住)
DEL_DENY_CHILDREN_SPEC = "*S-1-1-0:(DC)"  # 分区根: 封"删根下子项/整树"的父目录通道
DEL_DENY_FILE_SPEC = "*S-1-1-0:(DE)"  # 分区根下散文件: keyinfo/manifest/散 exe

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
# PDF 页面缩略图 (v2.4.2: 左侧面板, 懒加载 + 有界缓存; 数值集中便于审计)
# ---------------------------------------------------------------------------

THUMB_WIDTH_PX = 120  # 缩略图目标宽 (渲染 DPI 按页宽反推, 见 renderers.thumb_dpi)
THUMB_PANEL_W = 150  # 面板总宽 (含滚动条; 面板宽钉死不随内容撑变)
THUMB_GAP_PX = 10  # 相邻缩略图卡片的纵向间距
THUMB_SLOT_EXTRA_PX = 30  # 卡片内图片上/下留白 + "第N页"标签高度
THUMB_PREFETCH = 4  # 可视区外上下各预渲染的页数 (拖动时渐进填充)
THUMB_CACHE_MAX = 500  # 缩略图缓存上限 (张): 超出按"离可视区最远"淘汰
# (700 页大部头全缓存 ≈ 57MB 位图, 上限 500 张把单文档缩略图内存钉在 ~41MB)

# ---------------------------------------------------------------------------
# 扩展名 -> 查看媒体类型
# ---------------------------------------------------------------------------

_TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".py", ".ini", ".xml", ".yaml", ".yml"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
_PDF_EXTS = {".pdf"}
# Office 文档: 烧录端转换为 PDF 后按 pdf 渲染; 未转换则按 binary 处理
OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}

EXTENSION_MEDIA_MAP: dict[str, str] = {
    **{e: "text" for e in _TEXT_EXTS},
    **{e: "image" for e in _IMAGE_EXTS},
    **{e: "pdf" for e in _PDF_EXTS},
}

MEDIA_TYPES = frozenset({"text", "image", "pdf", "binary"})


def classify_media(suffix: str) -> str:
    """按扩展名返回查看媒体类型; 未知类型返回 "binary" (仅显示元数据)。"""
    return EXTENSION_MEDIA_MAP.get(suffix.lower(), "binary")
