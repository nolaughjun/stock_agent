# usb_vault — U盘加密烧录 + 只读查看器 (Windows + macOS)

把资料加密"烧录"到普通 U 盘发给**外部人员**: 盘上全程密文, 收件人通过盘内
查看器只读查阅 —— 不可拷贝导出、不可另存、截屏工具拍不到窗口。
文件名也在加密清单内; 密文载体 (`DATA\` 等) 已加隐藏属性, 资源管理器默认
视图只剩两个查看器入口, 强行显示也只能看到无意义的 `<随机id>.enc`。

**一只盘同时服务 Windows 与 Mac 收件人** (盘格式须为 exFAT):
盘根同时携带 `图书阅览器`(Windows) 与 `图书阅览器.app`(macOS),
两端各自读自身所在盘的**引导扇区卷序列号**做同一密钥推导 ——
同一只盘两边都能读, 拷到别的盘两边都打不开。

> **定位 (必读)**: 本工具的目标是让"随手复制"变得不可行, **不是绝对 DRM**。
> 安全边界见下文"防得住 / 防不住"。

## 快速开始

```bash
# 0) 依赖 (一次)
uv pip install pymupdf pyinstaller pillow   # cryptography/pywin32 已在环境

# 1) 构建查看器 (首次; 生成 SEED 并内嵌)
python -m src.app.usb_vault.main build-viewer
#    Windows 产物: dist/图书阅览器/ (onedir 目录, 默认; 误报率低于单文件)
#    可选 --onefile 回退单文件 exe

# 1m) 需支持 Mac 收件人时, 在一台真 Mac 上构建 .app (四步, 每步都有坑):
#     ① 拷 src/app/usb_vault/ **完整目录**到 Mac 本机磁盘 —— 单目录即可独立运行
#        (无需 src/app 祖先链; 但必须完整: 含 viewer/ 子目录与全部 .py;
#        拷整个仓库也行)。别在 exFAT U盘上直接构建, .app 内的符号链接会被吞掉
#     ② 装 Python 3.12+ : python.org 安装包, 或 brew install python@3.12
#        python-tk@3.12 (Homebrew 的 python 默认不带 tkinter, 必须补)
#        —— 源码运行最低 3.10 (@dataclass slots= 语法), 入口有版本闸门把关;
#        Xcode 自带的 python3 就是 3.9 (已停止安全维护), 装好依赖也跑不了,
#        且别往它里面 pip install (升级它的 pip 可能弄坏 Xcode 工具链)。装完
#        3.12 须**重开终端**再确认: python3 --version 显示 3.12.x 才算数
#     ③ 确认 SEED 一致: shasum -a 256 secrets/seed.bin 与烧录机同值
#        (同 SEED 才解得开烧录机烧的盘; 不一致立即停, 找烧录机要 seed.bin。
#        注意: 若 Mac 目录里没有 seed.bin, build-viewer 会警告并生成全新 SEED
#        —— 那个 .app 必然解不开烧录机的盘!)
#     ④ 在 usb_vault 目录里执行 (完整仓库拷贝则用
#        python3 -m src.app.usb_vault.main build-viewer, 命令同理)。
#        Homebrew 的 Python 禁止全局 pip (PEP 668 "externally-managed"),
#        必须装进 venv:
python3 -m venv .venv
source .venv/bin/activate        # 每次开新终端都要重新执行这行
python -m pip install pymupdf pyinstaller pillow cryptography
python main.py build-viewer
#     构建完核对输出 "内嵌 SEED 指纹: xxxxxxxx" 与烧录机 burn 输出同值
#     产物 dist/vault_viewer.app (内部全 ASCII 文件名!) 拷回烧录机, 放进 dist/:
#       Mac 上: cd dist && zip -r vault_viewer.app.zip vault_viewer.app
#       拷 zip 到烧录机后任意工具解压都安全 (旧版中文名 .app 才有码页改写风险,
#       它只能用 exFAT U盘直插 Mac 访达整目录拷的方式中转)
#     (烧录时 burn 自动把它以中文名 图书阅览器.app 写到盘上, 收件人看到的名字
#      不变; 且会验 .app 骨架(Info.plist/主程序) + 读内嵌 SEED 与本机硬比对,
#      坏了/不一致都直接拒烧)
#
#     Mac 首跑排错:
#     需要 Python 3.10+ ...                   → 还在用 Xcode 自带 python3=3.9,
#                                             按②装 3.12 后重开终端 (闸门消息同)
#     TypeError: dataclass() got an
#       unexpected keyword argument 'slots'   → 同上 = Python 3.9 (旧拷贝无闸门时
#                                             才会见到; 新版入口会先拦下并给指引)
#     error: externally-managed-environment   → Homebrew Python 禁止全局 pip, 按④
#                                             的 venv 四行装依赖
#     No module named 'src.app.usb_vault.xxx' → 目录拷贝不完整 (缺 xxx.py), 重拷①
#     Mac 上 .app 双击无反应                  → 两种实锤, 终端直跑主程序区分:
#                                             /Volumes/盘名/图书阅览器.app/Contents/MacOS/vault_viewer
#                                             ①报 no such file / 码页乱码名
#                                             (璧勬枡鏌ョ湅鍣) = zip 传输打坏结构,
#                                             用新版 ASCII 产物 (vault_viewer.app)
#                                             重构重传重烧即根除;
#                                             ②报 No module named 'src' = 旧版构建器
#                                             在"单目录形态"(只拷 usb_vault 目录)
#                                             下构建, 冻结包缺整个包链 —— 新版构建器
#                                             已自动暂存包链 + 缺隐藏导入直接构建
#                                             失败, 用最新代码重构即可。双击"无反应"
#                                             其实是 --windowed 程序一闪即退
#                                             (traceback 无处显示)
#     Mac 上"仅限指定的U盘使用"               → 九成是 SEED 不一致: 比对两边
#                                             seed.bin 的 sha256; 指纹一致再来
#                                             排查卷序列号 (报错误文案给开发)
#     产物自证 (2026-09-18 起)               → ①窗口标题必须显示 v2.3.3,
#                                             不显示 = 旧产物, 停止排查先换部件;
#                                             ②一条命令体检: 图书阅览器.app/Contents/
#                                             MacOS/vault_viewer --selfcheck
#                                             (构建版本/tkinter/内嵌SEED/数据卷/序列号
#                                             通道, 全✔退出码0; 构建机出炉必跑+上盘抽查)
#     Mac"读取U盘信息失败"但验1-3全对         → 授权读取半路失败被静默吞掉
#                                             (v2.2.1 起): 终端启动查看器重跑一次,
#                                             stderr 的 [admin_read_serial] 行就是原图
#                                             (取消/密码放弃/sudo 报错/解析失败)
#     Mac 授权密码框是查看器自绘的 (v2.2.3 起) → 非系统样式: macOS 判死
#                                             osascript 系统授权框 (其 root shell
#                                             对 /dev 全面 EPERM), 改 sudo -S 后
#                                             密码框由本程序绘制, 密码走 stdin 不
#                                             进命令行; 交付时须向收件人声明此边界。
#                                             sudo 后仍"操作不允许" → 系统设置→
#                                             隐私与安全性→完全磁盘访问权限, 给
#                                             图书阅览器.app 打开开关后重试
#     输码后仍"解锁码错误或非本盘资料"        → ①码是旧的: --password 每次烧录
#                                             都生成全新码, 重烧过的盘须用
#                                             最近一次 burn 输出的码 (或归档
#                                             secrets/unlock_codes.jsonl 最后一行);
#                                             先在烧录机跑 inspect --code 验证;
#                                             ②烧录机能开 Mac 不能开 = 序列号
#                                             读取不一致, 双端各跑 inspect 对比
#                                             "卷序列号"行 (不同值报给开发)
#     Mac 报"读取U盘引导扇区权限不足"         → 2026-09-15 真机三通道排查定论:
#    (Permission denied: /dev/diskNsM)        ① 直读 /dev 恒被拒 (设备节点属
#                                             root:operator, GUI 会话也一样);
#                                             ② diskutil VolumeUUID 对 exFAT
#                                             实测不提供该键 (exFAT 无文件系统
#                                             级 UUID, 仅为 FAT/HFS+/APFS 合成);
#                                             ③ 唯一可靠出路 = 管理员授权直读:
#                                             新版查看器在双通道全断时弹一次
#                                             "授权"询问 → 系统原生密码框 →
#                                             root 代读 512 字节 → 与 Windows
#                                             同值 (收件人需有 Mac 管理员密码)。
#                                             命令行诊断等价操作: sudo 运行
#                                             inspect。同轮还修了 exFAT 序列号
#                                             引导扇区偏移 bug (0x67→0x64, 详见
#                                             boot_sector.py 注释) —— 旧版即使
#                                             有权限也会解出垃圾序列号; 旧 .app
#                                             还有静默闪退 bug (VolumeIdError
#                                             未接住), 都用最新代码重构 .app 即
#                                             根除。烧录端仍走精确单值 (宁死不猜)
#     读取卷序列号失败/无法定位U盘设备节点    → --drive 须是卷根本身
#                                             (/Volumes/盘名, 不是子目录); 盘名带
#                                             空格要整体加引号; diskutil list external
#                                             与 mount | grep /Volumes/ 看真实挂载名;
#                                             U盘须 exFAT。diskutil 拒识时程序已会
#                                             自动改走 mount 输出兜底, 仍失败才报错
#     No module named 'cryptography'          → 上面 pip install 没做
#     No module named 'tkinter'               → brew install python-tk@3.12

# 2) 烧录 (插好U盘 --- 须 exFAT; 假设盘符 E:)
python -m src.app.usb_vault.main burn --source "D:/项目资料" --drive E: \
    --title "图书资料" --expire 2026-12-31 --password --set-label
#    烧录前自动病毒扫描 (Windows=Defender, 其余=ClamAV; 详见"病毒扫描"节)
#    --password: 每盘生成独立解锁码, 终端打印 + 归档 (详见"每盘解锁码"节)
#    --title 是资料标题 (进加密 manifest, 查看器窗口显示), 交付盘必须用
#    正式名称 (如 图书资料) —— 默认"资料"/测试名"test1"属内部名, 出盘禁止
#    (行政单位对命名敏感, 2026-09-18 收件人反馈)。收件人插盘看到的**卷标**
#    (盘符名) 另须加 --set-label 才会同步成同名; 标题在加密清单里, 事后
#    无法只改名不重烧
#    需"Win 不见 .app / Mac 不见 .exe"时: 追加 --dual-entry (双分区,
#    首烧全盘清空, 须管理员终端; 默认 gpt 制式, 详见"双分区烧录"节)

# 3) 复检 (可选但建议; 密码模式的盘要带 --code)
python -m src.app.usb_vault.main verify --drive E: --code XXXX-XXXX

# 发件人本机预览查看器效果
python -m src.app.usb_vault.main view --drive E:
```

收件人:
- **Windows**: 插盘 → 进 `图书阅览器` 目录双击 `图书阅览器.exe`。
  无签名 exe 首次可能被 SmartScreen 拦截: **更多信息 → 仍要运行**。
- **macOS**: 插盘 → 双击 `图书阅览器.app`。首次右键→打开绕过 Gatekeeper
  (未购买开发者账号时为 ad-hoc 签名, 无法免提示)。
- 图片/PDF 查看支持连续缩放 (2026-09-18 起): 工具栏**滑杆直接拉动**
  (25%–400% 任选) 或 **Ctrl+滚轮**以鼠标位置为中心缩放; `-`/`+`/`100%`
  按钮与滚轮上下滚动翻看不变。巨幅页面超内存预算时自动压回安全比例,
  页码栏会标注实际生效百分比。渲染在后台线程进行 (2026-09-19 起):
  大部头扫描件连续缩放/翻页时窗口不冻结, 渲染中光标为转圈状态。

## 工作原理 (跨平台盘绑定)

```
烧录端 (你的机器)                          收件端 (Windows 或 macOS)
──────────────────                        ─────────────────────────
SEED(64B, secrets/seed.bin, 内嵌查看器)    查看器读自身所在盘的卷序列号
data_key = scrypt(SEED,                    ← 两平台读同一物理扇区:
    license_nonce ‖ 卷序列号)                 Win: GetVolumeInformationW
         ↓ AES-256-GCM                       mac: /dev/diskNsM 引导扇区
U:\图书阅览器\      (Windows 查看器)          exFAT serial @0x64 同字节
U:\图书阅览器.app\  (macOS 查看器)         manifest 的 GCM tag 自校验
U:\keyinfo.bin      (明文盐, 29B: +mode 字节) 盘不对 ⇒ 解不开 ⇒ 拒绝
U:\manifest.enc     (加密清单)
U:\DATA\<32hex>.enc (逐文件密文, 4MiB 分块)
```

- **密文载体自动隐藏**: `keyinfo.bin` / `manifest.enc` / `DATA\`(含全部密文) /
  `图书阅览器\_internal\` 烧录后自动打 **+H+S** 属性 —— 资源管理器默认视图
  与"显示隐藏的文件"都看不到 (要看到须再手动关掉"隐藏受保护的操作系统文件",
  双重门槛); macOS 挂载 exFAT 时 DOS hidden 位同样映射为 Finder 不可见。
  查看器入口 (`图书阅览器.exe` / `.app`) 保持可见可双击。隐藏失败仅告警不阻断
  —— 它是观感层第一道门, 加密与盘绑定才是安全边界。附带效果: 普通程序覆写
  已存在的隐藏/系统文件会被 Windows 直接拒绝 (`CreateFile(CREATE_ALWAYS)` 语义),
  篡改盘上密文须先手动剥属性 —— 又一道摩擦。
- **盘绑定**: 密钥参与派生的卷序列号换盘即变 ⇒ 整盘拷到别的U盘/硬盘都打不开
  (两平台同一结论, 因为读的是同一份引导扇区字节)。
- **Office 自动转 PDF** (收件端不装 Office 也能看): doc/docx/xls/xlsx/ppt/pptx
  烧录时转 PDF。后端: Windows=MS Office COM, macOS=Office for Mac AppleScript,
  两平台都可用 `--converter soffice` 走 LibreOffice (免费兜底);
  什么都没装用 `--no-convert` (此类文件仅显示元数据)。
- **内置渲染**: 文本/图片/PDF 都在查看器内显示; 其他类型仅显示元信息 ——
  **任何情况都不落盘明文、不调用外部程序** (这是防泄密的关键设计)。
- **只读化 (防改写/防覆盖)**: 烧录完成自动对全部产物加只读属性,
  **含两个端的阅读器入口整树** (Windows exe/onedir 与 macOS .app, 双入口时
  分区1、分区2 各自的入口都在内)。挡得住: 内容修改、程序覆写 (写打开直接
  被拒)、`cmd del` 与 PowerShell `Remove-Item` 不带强制参数的删除。
  **挡不住资源管理器删除** —— 2026-09-20 收件人真机实测 + SHFileOperationW
  探针双证: 资源管理器删除 (单文件/整目录/Shift+Del) 先剥只读属性再删,
  照样删得掉; 防误删须另靠备份/重烧 (见"防不住"表)。
  已烧过的盘免重烧补锁: `lock --drive E:`。
- **查看器复读校验**: 拷入盘后逐文件比对 sha256 (onedir/.app 是整棵目录树)。

## 每盘解锁码 (`--password`, 合同⑤)

烧录加 `--password`: 每盘生成 8 位随机解锁码 (字母表 30 字符, 去 0/1/I/L/O/U
易混淆字符, `XXXX-XXXX` 两节展示; ~39.3 bit 熵 = 30^8 ≈ 6.6e11 种), 密钥派生变为:

```
data_key = scrypt(解锁码 ‖ SEED,   ← 码与 SEED 缺一不可:
    license_nonce ‖ 卷序列号)        拿到盘+提取 SEED, 或只拿到码, 都解不开
```

> 熵说明 (诚实声明): 8 位低于旧版 16 位 (~79 bit), 是易用性换取的让步。
> 防护依赖三层: scrypt 内存硬度 (每次尝试 ~32MB, 离线穷举无法廉价并行)、
> 查看器连错 3 次熔断、以及**码与盘分渠道传递** —— 最后一条是制度前提。

查看器启动时弹输入框 (容忍小写/连字符/空格, 连错 3 次停止须重开程序;
输入框默认掩码显示, 右端**小眼睛**点击可切换明文, 方便对照纸质码),
非密码盘行为完全不变; 旧版 28B keyinfo 盘照常可读 (向后兼容)。

**交付与归档流程 (乙方操作规程)**:

1. 烧录成功后终端醒目打印本盘解锁码 `XXXX-XXXX`, 并追加写入
   `secrets/unlock_codes.jsonl` (gitignore, 新建即 0o600; `--code-archive` 可改路径)。
   归档发生在**复检通过之后** —— 烧废的码不进档案。
2. 归档写失败时烧录**报错并把码带进报错文案**, 绝不出现"盘好了码丢了"。
3. 打印解锁码 → 密封 → 随合同交甲方; **码与 U 盘分渠道传递**
   (不放在同一个包裹/同一封信里, 否则密码模式形同虚设)。
4. `jsonl` 归档由乙方按合同留存, **期限 ≤ 质保期届满**, 到期销毁并记录。

**风险须知**: 解锁码一旦遗失, 该盘**永久无法阅读** —— 包括乙方自己
(除非归档还在)。这是特性不是缺陷: 没有后门正是"每套U盘独立唯一解锁密码"
的含义。请把归档文件纳入备份。

## 双分区烧录 (`--dual-entry`)

需求: U盘插 Windows 看不到 Mac 查看器 (.app), 插 Mac 看不到 Windows
查看器。单 exFAT 分区做不到 (+H 两 OS 都认、点前缀只有 Mac 认、运行时
改属性会残留破坏另一 OS), 因此用双分区 + 两制式 (`--dual-scheme`):

> **P0 已结案 (2026-09-17 真机)**: 首版用 MBR 类型 0x17 藏分区2, 真机
> 实锤 **macOS 不自动挂载 0x17 (隐藏 NTFS) 分区** —— DiskArbitration 认
> 分区类型字节, 不按内容探测, Mac 收件人根本看不到 .app。本版起弃用
> 0x17; 两制式的分区2 对 macOS 都是标准形态 (exFAT + 0x07 / GPT 基本
> 数据分区), 按内容挂载。gpt 制式的 Mac 侧挂载仍需按手册走一次真机。

```
磁盘 N (--dual-scheme gpt, 默认 —— 交付级)
├─ 分区1 exFAT, 保留原盘符                  ← 双 OS 都挂载
│    ├─ keyinfo.bin / manifest.enc / DATA/    (加密链与单分区完全一致)
│    └─ .图书阅览器/                          (点前缀: Mac Finder 默认不显示)
└─ 分区2 exFAT, GPT 无盘符属性 0x8000...0000  ← 属性写盘上, 换 Windows 机也不分配盘符
     └─ 图书阅览器.app

磁盘 N (--dual-scheme mbr —— 快速验证制式)
├─ 分区1 同上
└─ 分区2 exFAT, 类型 0x07, 无盘符             ← 仅烧录机上可靠隐藏 (见下)
     └─ 图书阅览器.app
```

```bash
python -m src.app.usb_vault.main burn --source DIR --drive E: --dual-entry [--dual-scheme gpt|mbr] [--yes]
# 须管理员终端; 磁盘底层操作全在 disk_layout.py (diskpart 脚本, 绝不裸写扇区)
```

- **制式选择**: **交付用 gpt (默认)** —— GPT_BASIC_DATA_ATTRIBUTE_
  NO_DRIVE_LETTER 属性存在**盘上**, 任何 Windows 机插上都不给分区2 分配
  盘符 (OEM 恢复分区同机制, 微软文档明确"换机生效"), 且 finalize 会读回
  detail partition 语义后验属性位, 没落上就报错拒出仓。**mbr 仅用于快速
  验证** —— "摘盘符"只改烧录机的挂载管理器记录, Windows 对没见过的卷
  默认自动补盘符, 收件人 Windows 机首插分区2 可能现身 (.app 可见, 不
  影响加密链);
- **前置**: Windows 烧录机 + **管理员终端** + dist/ 里有 Mac 构建的
  `图书阅览器.app` (缺 .app 直接拒烧 —— 双入口就是为携带它存在的);
- **首烧全盘清空**: 终端先打印磁盘号/型号/容量/现有盘符, 输入字面 `yes`
  才继续 (`y`/`YES`/`是` 一律拒绝); `--yes` 是给批处理脚本的显式豁免;
  盘符→磁盘号经 PowerShell 与 diskpart **双通道交叉验证**, 非 USB 总线
  (内置盘!) 直接拒绝。**红线: 只在空置备用盘上实验, 绝不碰资料盘**;
- **重烧复用**: 检测到**同制式**双入口布局时只刷新分区2 的 .app, 绝不
  再 clean, 也无需再次确认 (探测全走 Get-Partition 结构化字段, 不猜);
  制式不同则按新制式全盘重建 (有清盘确认);
- **旧 0x17 盘迁移**: 2026-09-16 首版烧的盘 (分区2=0x17, Mac 打不开的
  元凶) 用 `--dual-scheme mbr` 重烧会自动把类型字节改回 0x07 (刷新 .app
  不清盘); 转 gpt 则重烧时按新布局全盘重建;
- **旧系统**: 需要 Win10 1703+ 的 diskpart exFAT/convert gpt 支持;
  更旧系统在 provision 后验阶段直接报错, 不会烧出半成品盘;
- **序列号绑定不变**: 分区1 重建产生新的 exFAT 序列号, 派生密钥用的
  就是它 —— 对旧单分区资料盘首烧等于全盘清空, 旧库随清空消灭。
- **2026-09-17 真机首烧修复**: 旧版分区1 的 `create partition primary`
  未限宽, diskpart 语义是吃满全盘剩余空间 → 分区2 的 create 在零可用
  空间上失败 (退出码 0x80070057 = 参数错误, 盘停在半重建态, 重跑即可
  —— provision 每次都以 clean 开头)。现两分区均显式 `size=` (分区1 =
  实测容量 − 分区2 − 8 MiB 余量); 且 diskpart 报错文案改取输出**尾部**
  400 字符 (报错行跟在成串成功行后, 旧版截头部把失败原因切掉了)。

**Mac 收件人侧** (新版查看器已内置): .app 启动时若自身所在卷没有
keyinfo (双入口布局), 自动按名序扫 `/Volumes` 兄弟卷, 逐候选 GCM 试解
仲裁出真正的数据盘; 序列号读取遇权限墙 (VolumeIdError) 时优先返回该
候选接管理员授权恢复。单分区盘候选序退化为自身, 行为不变。
**Mac 排障**: 若 Finder 未见分区2 卷, 终端 `diskutil list external` 找到
分区2 设备名 (如 disk4s2) 后 `sudo diskutil mountDisk /dev/disk4`, 仍不
挂再 `sudo mount_exfat /dev/disk4s2 /Volumes/MacViewer` (按内容强挂)。

**诚实声明**: 无盘符 + 点前缀是**整理性隐藏, 不是安全边界** —— 磁盘
管理/diskutil 能看到分区, 补个盘符/去掉点前缀都能让它现身。它挡的
是"普通收件人顺手点开另一个 OS 的查看器", 挡不住有意逆向; 内容防护
仍然全部依赖加密链与序列号绑定。

### 真机验证手册 (操作者执行 —— gpt 制式转正前须完整走一遍)

只在**空置备用盘**上操作 (绝不接 F: 等真资料盘):

1. **首烧**: 管理员终端 `burn --source <样例资料> --drive <备用盘>: --dual-entry`
   (默认 gpt 制式), 核对确认提示里的磁盘号/型号/容量后输入 yes;
2. **Windows 核验**: `Get-Partition -DiskNumber N` 见 2 个分区且分区2 无
   盘符、GptType 为基本数据 GUID; 资源管理器只见分区1, `.图书阅览器` 可
   双击打开; 密文载体默认视图不可见;
3. **Mac 核验**: 插盘看 Finder 是否挂载出第 2 个卷 (`diskutil list external`
   确认分区2 在列) → 双击 .app 走兄弟卷扫描开门; 若不挂载按上方
   "Mac 排障"强挂后回报现象 (gpt+exFAT 理应自动挂载, 不挂=新 P0);
4. **换 Windows 机核验 (gpt 制式决定性)**: 盘插**另一台** Windows, 确认
   分区2 依然无盘符不现身 (属性随盘走; 这正是 gpt 与 mbr 的分水岭);
5. **重烧复用**: 同盘再跑一次 `burn --dual-entry`, 确认无清盘确认、
   分区1 数据未动、分区2 .app 已更新;
6. **旧 0x17 盘迁移**: 对首版盘跑 `--dual-scheme mbr` 重烧, 确认出现
   "旧版 0x17 分区已迁移为 0x07" 提示且 Mac 能挂载分区2;
7. **换机仿真**: 盘插另一台 Windows / Mac 各验证一次查看器开门;
8. **干净 VM AV 观察**: 双入口盘在干净 Windows VM 静置观察 Defender;
9. **结论回填**: 把各步结果 (OS 版本 + 现象 + 截图) 记录回本节。

## CLI 一览

| 命令 | 作用 |
|---|---|
| `burn --source DIR --drive E: --title T [--expire D] [--max-opens N] [--password] [--code-archive P] [--no-convert] [--viewer-exe P] [--viewer-mac P] [--converter B] [--av-cmd T] [--skip-scan] [--set-label] [--dual-entry [--dual-scheme gpt\|mbr] [--yes]]` | 烧录 (含烧录前病毒扫描; `--dual-entry`=双分区, 见专节) |
| `lock --drive E:` | 对已烧录U盘补加/恢复 只读+隐藏 (免重烧) |
| `verify --drive E: [--code C]` | 全量复检 (密文哈希→解密→明文哈希) |
| `inspect --drive E: [--code C]` | 列出标题/有效期/扫描记录/文件清单 |
| `av-scan --target DIR [--av-cmd T]` | 独立病毒扫描 (预检明文目录) |
| `build-viewer [--fresh-seed] [--onefile] [--vt-build] [--sign [ID]]` | 构建查看器 (默认 onedir) |
| `new-seed` | 轮换 SEED |
| `view --drive E:` | 发件人本机预览 |

## 病毒扫描 (设计 13)

**密文是 BY DESIGN 不可扫描的** —— 若杀毒引擎能扫, 攻击者同样能扫。
因此支持点在唯一的明文窗口: **烧录前对源目录全量扫描**。

- **fail-closed**: 找不到可用引擎 (Defender 被第三方 AV 顶替 / 未装 ClamAV)
  或引擎执行出错 ⇒ **中止烧录**; 确认无风险时 `--skip-scan` 显式豁免。
- **检测不处置**: Defender 用 `-DisableRemediation` 只报不删, 绝不动源文档。
- 检出威胁 ⇒ 中止烧录并列出威胁名。
- **退出码 2 的双重语义** (2026-09-17 真机): MpCmdRun 故障也吐退出码 2
  (`CmdTool: Failed with hr = 0x80004005`, 用时 0.0s —— 扫描没跑);
  按输出分流为引擎故障 (AVScanError, 指向本机 Defender, 日志
  `%TEMP%\MpCmdRun.log`), 绝不误报成"源目录检出威胁"。
  当天实例: 第三方杀软把 Defender 顶成 `SxS Passive Mode`
  (`RealTimeProtectionEnabled: False`), MpCmdRun 拒扫。
- 自定义引擎: `--av-cmd '"C:/AV/scan.exe" --deep {path}"'` (`{path}` 为目标占位)。
- 扫描记录进加密清单 (`inspect` 可见: 引擎名 + 时间)。

## 查看器杀毒适配 (设计 14)

目标是**不被主流杀软误报、拦截、隔离、查杀**:

- **onedir 为默认** (Windows 目录版 / macOS .app): 单文件 onefile 的自解压
  落地行为天然接近释放器 (dropper), 是误报最大来源;
- **版本资源**: Windows 产物带公司名/产品名/描述/版本号 (合法 PE 的基本特征);
- **代码签名是硬要求**: `build-viewer --sign` (Windows signtool / macOS codesign)。
  无证书分发须走 SmartScreen "仍要运行"; 正式分发请采购 OV/EV 代码签名证书;
- **真机多引擎测试**: 发布前在干净 VM 过 Defender/360/火绒/管家/金山;
- **VirusTotal 只交假 SEED 构**: `build-viewer --vt-build` 用公开常量 SEED,
  仅用于长尾引擎误报评估 —— **VT 样本公开可下载, 真 SEED 构建绝不能上传**;
- 误报走厂商白名单申诉闭环 (360/火绒/Defender 均有提交通道)。

## 防得住 / 防不住 (诚实声明)

**防得住:**

| 场景 | 结果 |
|---|---|
| 资源管理器里根本看不到密文载体 | `keyinfo/manifest/DATA/_internal` 已 +H+S 隐藏, 默认视图无从选中 |
| 强行显示隐藏文件后对密文 Ctrl+C | 拷走的是无头密文: 换盘解不开 (序列号绑定), 无 SEED/解锁码永远解不开; 同盘粘贴出的只是随机名孤儿文件, 查看器不认 |
| 资源管理器把 DATA 文件拖到硬盘/发给别人 | 拿到的是无头密文, 无文件名无法解密 |
| 整盘内容拷到另一只U盘/硬盘目录再运行查看器 | "仅限指定U盘使用" (卷序列号不符, 解不开; Win/Mac 同) |
| 在查看器里 Ctrl+C/⌘C / 右键复制 / 另存 / 拖出 | 全部不存在该路径 |
| OBS/截图工具截查看器窗口 | Windows: 黑框 (SetWindowDisplayAffinity); macOS: 拍不到窗口 (NSWindowSharingNone) |
| PrtScr | 被吞掉 (窗口持焦点时; macOS 系统截图经 CGWindowList, 同样拍不到) |
| 收件人改系统时间续期 | 仅拖延有效期判断 (见下"防不住"末行) |
| 程序覆写/篡改盘上产物 (含阅读器文件) | 只读位生效: 改写与写打开被系统拒绝, `cmd del`/`Remove-Item` 不带强制参数删不动; **资源管理器删除除外** (剥属性再删, 见"防不住"表) |
| 篡改盘上密文 | GCM tag 校验失败, verify 报错 |
| U盘插到"另一个 OS"看到对端查看器 (Win 见 .app / Mac 见 .exe) | 双分区: 分区2 无盘符 (gpt 无盘符属性 / mbr 摘盘符), Windows 不分配盘符; 分区1 上 Windows 入口用点前缀目录名, Mac Finder 默认不显示 (整理性隐藏, 边界见下表) |

**防不住 (软件方案的物理边界):**

| 场景 | 说明 |
|---|---|
| **手机翻拍屏幕** | 无解。极高敏资料请配合"少量分发+水印+追责"管理手段 |
| **VolumeID 类工具伪造卷序列号后整盘克隆** | 序列号是公开值, 可被改成与原盘相同; 克隆盘即可打开。**例外: `--password` 盘克隆后仍需 8 位解锁码 (30^8≈6.6e11, 配合 scrypt 内存硬度离线穷举代价高昂; 但低于旧 16 位方案 —— 码渠道保密是前提)** |
| **逆向查看器提取 SEED** | PyInstaller 包可被解包。**`--password` 盘提取 SEED 仍解不开** (码不落在盘上/exe 里); 无码盘则可离线解密该 SEED 烧的所有盘 —— 提高门槛: 每批 `new-seed` 轮换、必要时 Nuitka 重打包 |
| **内存取证 / 进程注入** | 内容展示时明文必然短暂存在于内存 |
| **收件人误删阅读器 (资源管理器删文件/整目录/Shift+Del)** | **拦不住 (2026-09-20 收件人真机实测)**: 资源管理器删除先剥只读属性再删, SHFileOperationW 探针同证 (连单个只读文件也删得掉); 可移动介质无回收站, 删了即永久 —— 误删恢复只能靠盘上备份或发件方重烧; macOS Finder 删除行为同属此列 (待真机留档) |
| **管理员清除只读属性 / 格式化重烧** | 只读属性挡的是误改与顺手覆盖 (整理性防护, 非安全边界); 格式化拦不住, 删成功即永久 (应对: 重烧或换盘); 管理员拿到的仍是密文 (`--password` 盘还需解锁码) |
| **"右击文件→复制"这个动作本身** | 用户态拦不住 —— 收件用户能读就能拷, 任何文件系统/属性组合都一样 (连加密 DVD 都如此; 商业"U盘防拷贝系统"靠签名内核过滤驱动, 是另一类产品形态)。本工具的对应防线是**拷了也没用**: 密文 + 盘序列号绑定 + 默认视图不可见 (见上表前三行) |
| **打开次数上限** | 计数存收件人本机 (Win: `%LOCALAPPDATA%`; mac: `~/Library/Application Support`): 按机计数、可删、换机清零 —— 纯软约束, 默认不开启 |
| **改系统时间绕过有效期** | 回拨可绕过; 上限场景加 `--max-opens` 双保险 |
| **双分区的无盘符 / 点前缀隐藏** | 整理性隐藏非安全边界: 磁盘管理/diskutil 能看到分区, 补个盘符/去掉点前缀即现身; mbr 制式在其他 Windows 机首插可能自动补盘符; 内容防护仍全靠加密链与序列号绑定 (见"双分区烧录"节) |

**给发件人的实证测试** (第 7 条就是上表"伪造序列号"的演示):

1. `view --drive E:` 本机预览正常;
2. (先开"显示隐藏的文件") 把 `DATA` 里文件拷到桌面 → 打不开;
3. 整盘拷到另一只U盘运行查看器 → "仅限指定U盘使用";
4. 拷整个盘内容到 C: 运行 → 同上;
5. OBS 窗口截图 → 黑框; PrtScr → 无内容;
6. 改日期到过期后 → "资料已过期";
7. (知己知彼) 用 VolumeID 把克隆盘序列号改成原盘 → 能打开 ⇒ 这就是边界。
8. 资源管理器删 `图书阅览器` 目录 → **删得掉** (2026-09-20 真机已验: 资源
   管理器先剥只读属性再删) ⇒ 只读位不是防删手段, 误删恢复靠备份或重烧;
   Mac 端删 `.app` 同试留档。

## U 盘要求与恢复

- **格式**: exFAT (推荐, 双平台读写的唯一实用公共格式); NTFS 同样支持
  (macOS 端原生只读挂载)。FAT32 有 4GiB 单文件上限, 不建议。
  烧录时会检查并提示;
- **兼容性**: 任何可格式化为 exFAT/NTFS 的普通 U 盘都可以, 不挑硬件;
- **恢复正常盘**: 资料回收后快速格式化一次即得全新空白盘 (卷序列号会重新生成,
  旧数据不可恢复 —— 加密落盘的东西本就无处可寻)。

## SEED 保管 (重要)

- `secrets/seed.bin` 是根密钥, **已 gitignore, 永不入库**;
- **离线备份一份** (密码管理器/加密压缩包): 丢失 = 旧盘仍可读, 但无法再烧与现有查看器兼容的新盘;
- 怀疑泄露 → `new-seed` + `build-viewer` 轮换: 旧盘不受影响, 新盘用新查看器;
- 共享 SEED 模式下, 一旦查看器被提取 SEED, 该 SEED 烧的所有盘暴露 ——
  高敏感场景建议按收件方分批轮换。

## 测试

```bash
pytest src/app/usb_vault -q          # 密码学往返/篡改矩阵/烧录端到端/
                                      # 克隆仿真/引导扇区解析/扫描退出码矩阵/
                                      # 双查看器复读/CLI 接线
ruff check src/app/usb_vault
python -m mypy -p src.app.usb_vault
```

关键测试手法: 卷序列号通过 `serial_getter` 注入, 全程不碰真实硬件;
"克隆仿真"用例断言换序列号后 `open_manifest` 必须失败;
杀毒引擎与 Office 后端全部 mock (不依赖真装 Defender/Office);
macOS 模块 (mac_volume/mac_protection) 用平台 monkeypatch 在 Windows 上跑逻辑;
双入口的磁盘操作 (diskpart/PowerShell 输出、分区布局、provision 编排)
全部 fake 成内存状态机或字节回放, 单测零真硬件依赖。

## 目录结构

```
src/app/usb_vault/
├─ config.py           算法参数/魔数/扩展名映射/版本常量 (审计入口)
├─ crypto.py           scrypt/HKDF 派生 + 分块 AES-256-GCM 流格式
├─ boot_sector.py      引导扇区卷序列号解析 (exFAT/FAT32/NTFS, 平台中立)
├─ volume_id.py        卷序列号平台分派 (Win ctypes / mac→mac_volume)
├─ mac_volume.py       macOS: diskutil→设备节点→读引导扇区
├─ seed.py             SEED 生成/加载 (_MEIPASS 运行态)
├─ unlock_code.py      每盘解锁码: 生成/规范化/展示/归档行 (合同⑤)
├─ manifest.py         清单模型 + 加解密 (v1.1 起含 av_scanned 审计字段)
├─ inspector.py        盘读取/全量校验/异常分级 (NotAVault/WrongDrive/PasswordRequired)
├─ av_scan.py          烧录前病毒扫描 (Defender/ClamAV/自定义, fail-closed)
├─ office_convert.py   Office→PDF 后端注册表 (win32com/applescript/soffice)
├─ burner.py           烧录编排 + 双查看器产物 + 字节复读 + 只读化
├─ disk_layout.py      双入口分区服务层 (diskpart+PS 交叉验证; 仅 win32 用)
├─ main.py             CLI
├─ viewer/
│  ├─ app.py           GUI 主程序 (平台分支: 定位/键位/字体)
│  ├─ renderers.py     文本/图片/PDF/二进制 内置渲染
│  ├─ win_protection.py   Windows: DPI/防截屏/PrtScr
│  ├─ mac_protection.py   macOS: NSWindowSharingNone (ctypes 直调 objc)
│  ├─ state.py         有效期/打开次数 (状态目录按平台)
│  └─ build_viewer.py  PyInstaller 构建 (版本资源/签名)
├─ secrets/seed.bin    根密钥 (gitignore)
├─ secrets/unlock_codes.jsonl   解锁码归档 (gitignore, 留存≤质保期)
└─ tests/              单测
```

## 路线图

- `--per-disk-seed`: 按收件方分批 SEED (进一步缩小单 SEED 泄露的影响面);
- PDF 水印 (收件人标识印在渲染层, 震慑翻拍)。
