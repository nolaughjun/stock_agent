# usb_vault — U盘加密烧录 + 只读查看器 (Windows + macOS)

把资料加密"烧录"到普通 U 盘发给**外部人员**: 盘上全程密文, 收件人通过盘内
查看器只读查阅 —— 不可拷贝导出、不可另存、截屏工具拍不到窗口。
文件名也在加密清单内; 密文载体 (`\.DATA\` 等) Windows 侧 +H+S 隐藏、
macOS 侧点前缀名隐藏 (v2.4.7), 资源管理器/Finder 默认视图只剩查看器入口,
强行显示也只能看到无意义的 `<随机id>.enc`。

**一只盘同时服务 Windows 与 Mac 收件人** (交付形态 2026-09-28 起
双分区均 NTFS, 见"NTFS 交付形态与防误删"节):
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
#     ⑤ Developer ID 签名+公证 (2026-09-24 起, 收件人 Mac 双击零提示的正式路线):
#        【完整分阶段操作手册 + 故障速查 → docs/Mac签名公证操作方案.md】
#        前提: Apple Developer Program 账号 + "Developer ID Application" 证书
#        已装入编译机钥匙串 (证书+私钥成对, security find-identity -v -p
#        codesigning 能列出才算装好); Xcode 命令行工具已装 (codesign/notarytool)。
#        一次性: 到 appleid.apple.com 生成 App 专用密码, 然后
#          xcrun notarytool store-credentials usbvault-notary \
#            --apple-id 账号 --team-id TEAMID --password 专用密码
#        每次构建 (构建命令加 --sign, 工具自动带 --options runtime+--timestamp,
#        并在签名前写合法 CFBundleIdentifier/版本号 —— 公证两硬前提):
#          python3 main.py build-viewer --sign "Developer ID Application: 单位名 (TEAMID)"
#        公证+装订+终验 (提交的是 ditto 打的 zip, 不能 zip -r):
#          ditto -c -k --keepParent dist/vault_viewer.app nv.zip
#          xcrun notarytool submit nv.zip --keychain-profile usbvault-notary --wait
#          xcrun stapler staple dist/vault_viewer.app
#          spctl -a -vv dist/vault_viewer.app   # 须显示 source=Notarized Developer ID
#        之后照旧 zip 拷回烧录机烧盘 (烧录改中文名目录不影响签名/票据)。
#        边界: 公证会把 .app 上传 Apple 扫描 —— 该二进制本就要发给每位收件人,
#        信任边界不变; 不触"真 SEED 不上 VirusTotal"红线 (那是公开样本库)。
#
#     Win7 双击阅览器报缺 api-ms-win-core-*.dll /
#       "Failed to load python314.dll"        → 不是缺陷: 内嵌 Python 3.14
#                                             仅支持 Win10+ (Python 3.9 起
#                                             弃 Win7), 换电脑是唯一解,
#                                             详见"支持的操作系统"节
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
#     产物自证 (2026-09-18 起)               → ①窗口标题必须显示 v2.4.8,
#                                             不显示 = 旧产物, 停止排查先换部件;
#                                             ②一条命令体检: 图书阅览器.app/Contents/
#                                             MacOS/vault_viewer --selfcheck
#                                             (构建版本/自身介质/tkinter/内嵌SEED/数据卷/
#                                             序列号通道, 全✔退出码0; 构建机出炉必跑+上盘抽查;
#                                             v2.4.8 起自身介质行兼作换棒红线核验通道)
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
#                                             U盘须 exFAT/NTFS (NTFS 只读挂载
#                                             同样可读序列号)。diskutil 拒识时程序已会
#                                             自动改走 mount 输出兜底, 仍失败才报错
#     No module named 'cryptography'          → 上面 pip install 没做
#     No module named 'tkinter'               → brew install python-tk@3.12

# 2) 烧录 (插好U盘; 双分区交付由工具自建 NTFS, 单分区烧到 exFAT/FAT32 盘
#    也可但无防误删 ACL; 假设盘符 E:)
python -m src.app.usb_vault.main burn --source "D:/项目资料" --drive E: \
    --title "图书资料" --expire 2026-12-31 --password --set-label
#    烧录前自动病毒扫描 (Windows=Defender, 其余=ClamAV; 详见"病毒扫描"节)
#    烧录收尾自动施加 NTFS 防误删 ACL (详见"NTFS 交付形态与防误删"节)
#    --password: 每盘生成独立解锁码, 终端打印 + 归档 (详见"每盘解锁码"节)
#    --title 是资料标题 (进加密 manifest, 查看器窗口显示), 交付盘必须用
#    正式名称 (如 图书资料) —— 默认"资料"/测试名"test1"属内部名, 出盘禁止
#    (行政单位对命名敏感, 2026-09-18 收件人反馈)。收件人插盘看到的**卷标**
#    (盘符名) 另须加 --set-label 才会同步成同名; 标题在加密清单里, 事后
#    无法只改名不重烧
#    需"加密链藏进不可见分区 (盘插上看起来干净)"时: 追加 --dual-entry
#    (双分区, 首烧全盘清空, 须管理员终端; 默认 gpt 制式, 详见"双分区烧录"节)

# 3) 复检 (可选但建议; 密码模式的盘要带 --code)
python -m src.app.usb_vault.main verify --drive E: --code XXXX-XXXX

# 发件人本机预览查看器效果
python -m src.app.usb_vault.main view --drive E:
```

收件人:
- **Windows**: 插盘 → 进 `图书阅览器` 目录双击 `图书阅览器.exe`。
  无签名 exe 首次可能被 SmartScreen 拦截: **更多信息 → 仍要运行**。
- **macOS**: 插盘 → 双击 `图书阅览器.app`。Developer ID 签名+公证的产物
  (2026-09-24 起) 双击零提示直开; ad-hoc 时代的旧产物首次需右键→打开
  (macOS 15 无右键旁路, 须 系统设置→隐私与安全性→仍要打开)。
- 图片/PDF 查看支持连续缩放 (2026-09-18 起): 工具栏**滑杆直接拉动**
  (25%–400% 任选) 或 **Ctrl+滚轮**以鼠标位置为中心缩放; `-`/`+`/`100%`
  按钮与滚轮上下滚动翻看不变。巨幅页面超内存预算时自动压回安全比例,
  页码栏会标注实际生效百分比。渲染在后台线程进行 (2026-09-19 起):
  大部头扫描件连续缩放/翻页时窗口不冻结, 渲染中光标为转圈状态。
- PDF 支持页码跳转 (v2.4.1 起, 2026-09-23 用户需求): 工具栏翻页按钮旁
  **页码框输页码回车 (或点"跳转") 直达**; 大部头逐页翻太慢的刚需。输入
  解析在 `pdf_page_jump` 纯函数: 中文输入法全角数字自动归一, 越界**不夹
  不猜** (输 999 想跳末页与手误无法区分), 只提示有效范围 1~N。
- PDF 页面缩略图 (v2.4.2 起, 2026-09-23 用户需求, 依截图样例): 打开 PDF
  后正文左侧出现**单列页面缩略图** (卡片 + "第N页"标签), 当前页蓝框高亮
  且面板跟页滚动, **点击任一缩略图直达该页**; 面板 × 关闭, 工具栏
  "缩略图"勾选框恢复。大部头对策: 渲染走后台通道 (与主视图同一渲染线程,
  doc 线程封闭不破, 翻页/缩放帧恒优先), 只渲可视区 ± 预取 (懒加载),
  缓存超 500 张按"离可视区最远"淘汰 —— 防泄密原则不变: 缩略图只进内存
  只上屏幕, 不落盘。2026-09-23 探针: Windows Tk 20 万 px scrollregion
  坐标完好; macOS 大坐标列入真机验证第 8 步。
- 高分屏 / 二合一平板适配 (v2.4.3 起, 2026-09-23 用户 Surface 真机反馈
  "左侧列表显示不完全, 且无法调整"): 根因 = DPI 感知声明后字体随点数
  ×DPI 放大而列宽/sash/滑杆是裸像素不放大, 200% 缩放下左栏 128px 只装
  4 个汉字、ttk 分隔条几像素触屏抓不住。修: ①全 UI 裸像素统一乘
  `ui_scale` (与字体同一 DPI 源 `winfo_fpixels`, 探针实证字体宽度与
  系数 1.99:2.00 同步联动); ②换 `tk.PanedWindow` (sashwidth 随缩放
  加宽, 可见可抓); ③左栏顶部 **◀/▶ 宽度按钮** (触屏目标够大); ④左栏
  初宽按最长文件名实测自适应 (夹 [128,320]×scale)。缩略图面板同乘
  scale, 缓存上限按 scale² 反压缩 (位图内存不随缩放翻倍)。
  **旧产物 (≤v2.4.2) 在 150%+ 缩放机器上左栏不可用属已知缺陷, 交付前
  双平台重构须显示 v2.4.3。**
- 拷离U盘的阅览器谢幕文案 (v2.4.4 起, 2026-09-23 用户定稿): 阅览器被拷
  到其他电脑/硬盘后双击, 直接提示 **"脱离U盘介质，加密书稿无法在该设备
  上打开查看"** —— 判据 = 全机找不到任何 keyinfo 载体 (`_resolve_data_drive`
  候选为空返回 None), 先于解锁码弹框。旧版行为是拿自身卷硬开清单后弹
  "该盘不是资料盘: 缺少 keyinfo.bin", 对收件人有误导性。注意区分: 整盘
  内容 (含 keyinfo) 拷到别的盘再运行, 走的仍是"仅限指定U盘使用" (载体
  在但卷序列号不符); 两句文案分别对应"拷阅览器"与"拷整盘"两种情形。
  边界 (v2.4.4 时为设计内, **2026-09-29 用户实测后 v2.4.8 关闭**): 拷走
  的阅览器在**原盘仍插着**的同一台机器上运行, 全机卷扫描仍能经 GUID 卷
  找到分区2 开门。修: `_startup_gate` 最前端加**自身介质闸门**
  (`_exe_on_removable_media`, 仅冻结构建启用 —— 烧录机 `view` 源码预览
  不受影响): Windows = `GetDriveType(自身盘)==removable`, macOS = 自身卷
  挂在外置介质 (`mac_volume.is_on_external_media`, diskutil
  `Internal=false`); 判不出按不在可移动介质处理 (fail-closed 谢幕, 文案
  同上句)。**换交付棒型必须先验** DriveType=removable / Internal=false
  (个别 USB-SSD 类棒报 fixed, 硬闸会误伤真棒)。新残余边界: 拷到**另一支
  可移动盘**且原盘同机在场仍可读 (介质判据判不出, 堵它需阅览器与加密链
  同物理盘校验, 未做)。
- 单拷书名 exe 谢幕根治 (v2.4.5 起, 2026-09-23 用户复测曝光): v2.4.4 门禁
  只在 Python 启动后生效, 而把双入口目录内的 `《欧阳竟无全集》《桂伯华
  全集》.exe` **单独**拷走再双击, onedir 主 exe 死在 PyInstaller C 装载器
  层 ("Failed to load Python DLL … 找不到指定模块"), 应用层文案根本没机
  会跑。修: 收件人双击的书名 exe 改为**自包含引导壳** (onefile, 仅
  ctypes/subprocess 依赖, 实构建 9.2MB、解包亚秒) —— 目录完整 (旁有真主
  程序 `图书阅览器.exe` + `_internal`) 时转交真主程序且命令行参数原样透传
  (`--selfcheck` 等自检路径不受影响), 目录残缺时直接弹同一句谢幕文案
  (探针实证: 单拷壳 2 秒仍存活阻塞在谢幕框; 完整目录转交时参数逐字到达
  子进程)。真主程序保持构建名不再改名 (`_rename_dual_inner_exe` 退役,
  改为 `_install_dual_launcher`; 真主程序缺失 = 转交链断裂, 烧录熔断)。
  构建: build-viewer 在 Windows 目录版时自动连带产出 `dist/vault_launcher.exe`
  (ASCII 产物名); Mac 烧录机跨机烧录须 `--launcher-exe` 指定。
  边界 (设计内): ① 单拷目录内的**真主程序** `图书阅览器.exe` 仍会弹英文
  DLL 错误 (onedir 本质如此) —— 手册只教双击书名 exe; 该文件 v2.4.6 起
  +H+S 隐藏 (详见下条真棒事故), 单分区 onedir 的同名 exe 是唯一可见入口
  不在藏匿之列; ② 壳是 onefile 形态,
  首次运行解包到 `%TEMP%\_MEIxxxx` (亚秒级, 退出由 PyInstaller 自清); 杀毒
  误报治理按既有路线 (代码签名 + 必要时 VT 送检) 走, 壳不带 tkinter/PyMuPDF
  大签名面。
- 盘上真主程序消失事故 + 可分辨文案 (v2.4.6, 2026-09-23 真棒复测曝光):
  18:15 烧录核验过的双入口盘, 收件人双击**盘上**书名 exe 也弹"脱离U盘介
  质"——排查确认烧录产物在烧录时刻完整 (burn 不变量 + 清扫仅根级均验证),
  是真主程序 `图书阅览器.exe` **事后**从盘上消失: 它顶着一代旧名裸露在
  书名 exe 旁, 被当旧版残留清掉 (嫌疑最大) 或被杀毒隔离。两修: ① 双入口
  目录内真主程序 +H+S 隐藏至与 `_internal` 同待遇 (判据 = 书名引导壳在旁,
  单分区 onedir 的唯一入口不藏; burn_dual 与 lock_drive 两处藏匿段同步);
  这推翻了 v2.4.5 "保持可见" 的 dropper 特征顾虑——删除是已发生的事故,
  dropper 启发式是理论风险, 且交付走代码签名。② 引导壳三态文案:
  `_internal` 在而真主程序缺 = 坏盘, 弹 BROKEN_INSTALL_MESSAGE
  ("阅览器文件不完整，请联系发件方重新烧录U盘"), 与谢幕文案可分辨, 不再
  把坏盘误报成拷离、误导排障方向 (config 单源, 三态探针实证弹框逐字)。
- Mac 侧观感隐藏: 分区2 卷标 + 密文链文件名全部点前缀 (v2.4.7, 2026-09-24
  用户问"MAC系统的加密分区部分可以隐藏嘛"): 分区级"不挂载"在 Mac **没有
  等效物** —— 阅览器靠 `/Volumes` 挂载点普通权限读分区2 (不挂载 = 只能提权
  裸读 /dev, v2.2.x 权限墙老路); Windows 的 GPT 无盘符属性是 Windows 专属
  语义, macOS 对 GPT 数据分区按内容识别照常挂载; 把类型改成 Mac 不认的
  GUID 则 Windows 也不再枚举该卷, 两头都得管理员裸读 (设计内"得不偿失")。
  落地两层: ①分区2 卷标 `.资料存储` —— 照常自动挂载, 阅览器扫 /Volumes
  不看卷名照常命中; **卷标设置失败由"仅告警"升为熔断** (落空 = Mac 桌面
  多一个 Untitled 空卷)。②密文链文件名 `.keyinfo.bin`/`.manifest.enc`/
  `.DATA` —— macOS 不认 DOS 隐藏位 (旧 README"exFAT hidden 位映射 Finder
  不可见"系无实证断言已撤回), 点前缀是 Mac 侧唯一文件级观感隐藏。
  **换名=换代**: 复用判据只认新名 (v2.4.0~2.4.6 明文名密文链重烧走全盘
  重建), 旧名残留由 _clear_own_artifacts 按 `*_LEGACY` 名清扫。边界:
  磁盘工具/diskutil 里分区条目仍可见 (同 Windows 磁盘管理, 整理性隐藏
  非安全边界)。
- **2026-09-29 跨平台实机裁决 (点前缀卷隐藏证伪 + 属性自愈)**: 收件人
  Mac 真机插盘, 桌面/边栏**照样显示出 `.资料存储` 卷** (点前缀对卷级
  Finder 显示在实机不成立 —— 文件级点前缀隐藏不受影响, 卷内打开仍只见
  空白); 且 macOS 改写 GPT 分区表时**剥掉了微软专属的无盘符属性位**,
  盘插回 Windows 后分区2 被补上盘符现形。两项均为双分区形态的固有边界:
  Mac 对可识别分区必挂载、且无盘上机制可携带 Windows 属性 (用户评审
  定案: 保持双分区 + 烧录机侧自愈, 不退单分区)。落地三件: ① `verify`
  增分区2隐藏态检查 (Get-Partition 读 GptAttributes/DriveLetter, 普通权限
  可跑; 属性丢失/盘符在挂 = 报警退非零); ② 新子命令 `heal` (免重烧恢复:
  摘盘符 + 重落属性位 + detail partition 读回后验, 须管理员; 单分区形态
  直接告知无需处理); ③ 复烧路径本就经 finalize 重落属性, 重烧即自愈。
  收件侧 Windows 即使属性被剥, 分区2 里密文仍 +H+S 双门槛 + 拒删 ACL,
  现形的是"空卷"; Mac 侧现形的同样是打不开的空卷 (NTFS 只读 + 点前缀
  文件不可见)。

## 工作原理 (跨平台盘绑定)

```
烧录端 (你的机器)                          收件端 (Windows 或 macOS)
──────────────────                        ─────────────────────────
SEED(64B, secrets/seed.bin, 内嵌查看器)    查看器读自身所在盘的卷序列号
data_key = scrypt(SEED,                    ← 两平台读同一物理扇区:
    license_nonce ‖ 卷序列号)                 Win: GetVolumeInformationW
         ↓ AES-256-GCM                       mac: /dev/diskNsM 引导扇区
U:\图书阅览器\      (Windows 查看器)          NTFS serial @0x48 同字节
U:\图书阅览器.app\  (macOS 查看器)         (2026-09-28 前 exFAT @0x64)
U:\图书阅览器.app\  (macOS 查看器)         manifest 的 GCM tag 自校验
U:\.keyinfo.bin      (明文盐, 29B: +mode 字节) 盘不对 ⇒ 解不开 ⇒ 拒绝
U:\.manifest.enc     (加密清单)
U:\.DATA\<32hex>.enc (逐文件密文, 4MiB 分块)
```

- **密文载体自动隐藏**: `.keyinfo.bin` / `.manifest.enc` / `.DATA\`(含全部密文) /
  `图书阅览器\_internal\` 烧录后自动打 **+H+S** 属性 —— 资源管理器默认视图
  与"显示隐藏的文件"都看不到 (要看到须再手动关掉"隐藏受保护的操作系统文件",
  双重门槛); macOS 侧**不依赖 DOS hidden 位** (旧版 README 曾断言"exFAT
  hidden 位映射 Finder 不可见", 无真机实证已撤回): v2.4.7 起载体全部
  **点前缀名**, Finder/ls 默认不显示 —— 两平台各自用最稳的机制, Mac 真机
  核验列入验证手册。
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
  **只读位挡不住资源管理器删除** (2026-09-20 收件人真机实测 + SHFileOperationW
  探针双证: 先剥只读属性再删) —— 防误删由 NTFS deny ACL 接管 (下节)。
  已烧过的盘免重烧补锁: `lock --drive E:`。
- **查看器复读校验**: 拷入盘后逐文件比对 sha256 (onedir/.app 是整棵目录树)。

## NTFS 交付形态与防误删 (2026-09-28)

交付棒 **P1+P2 双分区均转 NTFS** (用户评审定案), 防误删从"+R 只读位"
(2026-09-20 探针实证资源管理器删除会剥只读位, 形同虚设) 升级为
**NTFS deny ACE —— 内核访问检查层强制的拒绝, 资源管理器剥不掉**。

**配方 (两级都拒, 缺一即穿)**: Windows 删除的充要条件 = 文件 DELETE
**或** 父目录 FILE_DELETE_CHILD, 得一即可删 —— 探针矩阵 (六路删除 ×
三种变体, 2026-09-28 本机 NTFS 实测) 定案:

| 层 | ACE | 封什么通道 |
|---|---|---|
| 分区根 | `deny *S-1-1-0:(DC)` | 整树删除 / 删根下子项 (父目录通道) |
| 交付树根 (阅览器目录 / .DATA) | `deny *S-1-1-0:(OI)(CI)(DE,DC)` | 继承罩全树, **后落盘文件也罩住** |
| 分区根下散文件 (keyinfo/manifest/散 exe) | `deny *S-1-1-0:(DE)` | 单文件删除 (文件通道) |

实测效果 (ship 配方全绿): `os.remove` / `cmd del` / `cmd del /f` /
PowerShell `Remove-Item -Force` / 资源管理器右键删进回收站 / Shift+Del
永久删 (含整树) **全部被拒** (winerror 5); 读取/复制/正常阅读不受影响
(deny 只含 DE/DC 两个 specific 位, 不碰读/写/执行 —— 2026-09-15 `--acl`
教训: generic 位连读带执行一起拒, 绝不用)。半套配方的反面实证: 只拒文件
→ 父目录通道六路全穿; 只拒父目录 → 文件通道照删。

**副作用 (如实声明)**: 改名/移动同被拒 (与删除同需 DELETE 位) —— 收件人
想改名须先理解这是保护; 手册已声明"删除/改名被拒属正常"。

**边界 (防误删不防蓄意)**: 所有者/管理员 `icacls <盘> /remove:d *S-1-1-0 /t`
整树摘除后可删可改; 格式化 / diskpart clean 是卷级操作, ACL 无从干涉
(复烧的 clean 重建因此不受影响)。防蓄意删除从来不是目标 (拷了也没用才是)。

**工具链落位**:
- `provision` 双分区 format 一律 `fs=ntfs` (序列号锚点 NTFS@0x48,
  `boot_sector` 已支持; 分区类型字节 0x07 不变);
- `burn` 收尾 `_apply_delete_denial` 施加配方 (分区2 在临时盘符窗口内
  施加完毕再 finalize 隐藏); **NTFS 卷施加失败 = 熔断拒出仓**;
  exFAT 单分区烧录是合法场景, 记警告缺席不阻断;
- 重烧/refresh 前清障 `strip_delete_denial`: 交付树**整树 `/t`** —— 实证坑:
  icacls 单文件 `/remove:d` 对继承来的 deny **返回成功但实际没摘**;
  **分区根例外走非递归** (根上只有非继承 `(DC)` 一条; 且从分区根 `/t`
  递归必踩 `System Volume Information` —— 管理员亦无权, rc=5,
  2026-09-29 真机事故), 树/散文件由 `_clear_own_artifacts` 名单逐项摘;
  校验必须回读 ACL (`deny_rights` 走 GetNamedSecurityInfoW + SID 比对,
  免本地化、不信任何退出码);
- `verify` 新增 ACL 在位检查 (回读配方是否齐全, 缺失逐条报 ✘ 退出码 1);
  非 NTFS 卷提示旧形态建议重烧;
- `lock` 同集补施 (幂等, 失败仅告警);
- **换格式=换代** (同"换名=换代"先例): 复用判据新增 —— 任一分区非 NTFS
  (exFAT 旧形态, 无 ACL) → 不复用, 清盘重建为 NTFS 交付形态 (过确认)。
  **2026-09-28 前烧的所有交付棒重烧时都会自动换代**。

**Mac 侧 (待真机核验, 不行即回退)**: macOS 原生对 NTFS 只读挂载 ——
P2 挂载照常但本就删不了写不了 (比 deny 更强); P1 只读挂载下 .app 能否
**运行** (DMG 同机理但无先例) 待真机验证; 脏位 NTFS 可移动盘会被 Mac
拒绝挂载 (2026-09-24 事件) —— Windows 侧异常拔盘写脏位的风险交由
"安全弹出"流程约束并列入手册。

**真机核验清单 (交付前必走)**: ①资源管理器手删阅览器目录/Shift+Del →
须弹拒绝; ②收件样机 (无管理员权限账户) 同试; ③Defender 对 ACL 棒的反应;
④Mac 插盘: P1 挂载 + .app 运行 + P2 表现; ⑤`verify` 输出 ACL 齐全行。

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

## 双分区烧录 (`--dual-entry`, v2.4.0 反转布局)

需求 (2026-09-22 定案): **上级检查时盘插上看起来必须干净** —— Windows
阅览器和 Mac 阅览器同放一个可见分区 (双入口体验不变), 加密链全套
(keyinfo/manifest/DATA) 挪到另一个 **Windows 不可见** 的隐藏分区; 同时
阅览器仍要普通权限读得到它。单 exFAT 分区做不到 (加密链与阅览器同卷
必然同显隐), 因此用双分区 + 两制式 (`--dual-scheme`)。
**2026-09-29 跨平台实机裁决**: 分区2 在 Mac 侧会**显示** (点前缀对卷级
Finder 无效, 实机证伪), macOS 还会剥掉无盘符属性位令 Windows 侧重现 ——
双分区的隐藏保证以 Windows 为主场, Mac 侧现形为"空卷", 跨平台往返后
烧录机 `verify` 报警 + `heal` 恢复 (见功能段"跨平台实机裁决")。

> **地基探针已结案 (2026-09-22)**: GPT 无盘符属性的卷仍被系统挂载,
> **非提权**进程经 `\\?\Volume{GUID}\` 路径可列目录/读文件 (本机无盘符
> NTFS 恢复卷实测通过; EFI 分区的拒绝访问属微软**引导分区专项加固**
> (防 rootkit), exFAT 数据分区不在其列 —— 阅览器按卷吞 OSError 绕开)。
> F: 备用棒烧录后复测列入验证手册步骤 2b。
> **历史 P0 (2026-09-17 真机)**: 首版用 MBR 0x17 藏分区, macOS 不自动
> 挂载 —— 已弃用; 现两制式的分区2 对 macOS 都是标准可挂载形态
> (2026-09-28 起 NTFS, macOS 原生只读挂载, 表现待真机核验)。

```
磁盘 N (--dual-scheme gpt, 默认 —— 交付级)
├─ 分区1 NTFS, 保留原盘符, 卷标 图书资料     ← 双 OS 都挂载, "看起来干净"
│    ├─ 《欧阳竟无全集》《桂伯华全集》图书阅览器（适配Windows系统）/
│    │        ├─ 《欧阳竟无全集》《桂伯华全集》.exe   (Windows 入口; 书名引导壳, v2.4.5: 自包含 onefile)
│    │        ├─ 图书阅览器.exe                        (真主程序, 保持构建名; 壳按名转交; v2.4.6: +H+S 隐藏)
│    │        └─ _internal/                            (依赖树, +H+S 隐藏)
│    ├─ 《欧阳竟无全集》《桂伯华全集》图书阅览器（适配MAC-Apple芯片）.app/ (Mac 入口·Apple 芯片; Windows 显示为普通文件夹)
│    └─ 《欧阳竟无全集》《桂伯华全集》图书阅览器（适配MAC-Intel芯片）.app/ (Mac 入口·Intel 芯片; 双架构交付 2026-10-02)
└─ 分区2 NTFS, GPT 无盘符属性 0x8000...0000, 卷标 .资料存储   ← 属性写盘上, 换 Windows 机也不分配盘符
     ├─ .keyinfo.bin / .manifest.enc / .DATA/  (加密链全套, +H+S; 点前缀=Mac 侧文件隐藏, v2.4.7)
     └─ Windows 资源管理器不显示; 阅览器经 \\?\Volume{GUID}\ 普通权限读取;
        Mac 照常自动挂载在 /Volumes/.资料存储, 桌面/边栏会显示该卷 (2026-09-29
        实机证伪点前缀卷隐藏), 但打开只见空白 (点前缀文件 Finder 不显示);
        ⚠ macOS 改写 GPT 会剥掉无盘符属性位 → 盘回 Windows 后分区2 可能带盘符
        现形 (内容仍 +H+S 双门槛 + 拒删 ACL) —— 烧录机 verify 报警, heal 恢复
(2026-09-28 起双分区均 NTFS + 防误删 deny ACL, 见"NTFS 交付形态与防误删"节;
 此前 exFAT 形态重烧时自动换代)

磁盘 N (--dual-scheme mbr —— 快速验证制式)
├─ 分区1 同上
└─ 分区2 NTFS, 类型 0x07, 无盘符             ← 仅烧录机上可靠隐藏 (见下)
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
  默认自动补盘符, 收件人 Windows 机首插分区2 可能现身 (内容是密文 +
  +H+S, 不影响加密链; 阅览器候选序第三序"其余带盘符卷"兜住);
- **前置**: Windows 烧录机 + **管理员终端** + dist/ 里有 Mac 构建的
  `vault_viewer.app` (Apple 芯片) **与 `vault_viewer_intel.app` (Intel 芯片)**
  (2026-10-02 双架构起缺任一 .app 直接拒烧 —— 不烧只有一半 Mac 收件人
  能打开的盘; 单入口 Apple 版缺失仍只警告可烧 —— 单分区盘自担兼容);
- **分区1 尺寸**: 按双阅览器总体积定宽 (`max(1GiB, 体积×1.25+32MiB)` —
  2026-09-22 用户定稿: 1 GiB 底线, 分区1是唯一拔插可见可读的常驻入口,
  余量直接给足), 分区2 = 剩余全部 —— 加密链有整个盘身可用;
- **首烧全盘清空**: 终端先打印磁盘号/型号/容量/现有盘符, 输入字面 `yes`
  才继续 (`y`/`YES`/`是` 一律拒绝); `--yes` 是给批处理脚本的显式豁免;
  盘符→磁盘号经 PowerShell 与 diskpart **双通道交叉验证**, 非 USB 总线
  (内置盘!) 直接拒绝。**红线: 只在空置备用盘上实验, 绝不碰资料盘**;
- **重烧复用 (内容判别)**: 表签名无法区分新旧布局 (都是"无盘符
  分区2"), 复用判据是**内容** —— 临时挂出分区2, 见 .keyinfo (v2.4.7 新名)
  = 本工具反转布局 → 复用, 绝不 clean, 无需再确认; 见 .app/旧明文名密文/
  其他 = 旧版布局 (含 v2.4.0~2.4.6 明文名链, 换名=换代) 或异构 → 全盘重建
  (有清盘确认)。制式不同也直接重建; **2026-09-28 换格式=换代**: 任一分区
  非 NTFS (exFAT 旧形态无防误删 ACL) 同样判全盘重建 (复用判据之三
  `_fs_not_ntfs`, 与分区1底线判据同链);
- **旧版双分区盘 (分区2=.app) 与旧 0x17 盘**: 重烧一律全盘重建为反转
  布局 (过确认) —— 旧分区2 的尺寸/内容装不下加密链, 迁移无意义;
- **序列号绑定锚点转移**: 派生密钥用的序列号现在取自**分区2** (加密链
  所在卷, burn 内部自动); 对阅览器行为无感, 但复检/verify 时要对着
  分区2 临时盘符根做。首烧等于全盘清空, 旧库随清空消灭;
- **补锁 (`lock`) 的对象**: 反转布局的 `lock --drive` 要指到**分区2** ——
  实操: 重跑一次 `burn --dual-entry` (复用布局不清盘, 收尾自动补全
  只读+隐藏), 或在磁盘管理给分区2 临时盘符后 `lock --drive X:`;
- **旧系统**: 需要 Win10 1703+ 的 diskpart exFAT/convert gpt 支持;
  更旧系统在 provision 后验阶段直接报错, 不会烧出半成品盘;
- **2026-09-17 真机首烧修复** (仍然适用): 两分区均显式 `size=`
  (分区1 按阅览器定宽, 分区2 = 实测容量 − 分区1 − 8 MiB 余量), 裸
  `create partition primary` 吃满全盘的坑不会再踩;
- **2026-09-22 真机首烧修复 (同日两轮, 竞态终版)**: 白牌慢速棒
  (Generic STORAGE DEVICE 115GB) 上 `create partition` 返回时卷对象
  尚未枚举 —— assign 和 format 都需要卷焦点, 同一脚本里紧跟哪条都会
  报 "There is no volume selected/specified" (0x80070057), 盘停在
  GPT + RAW 分区的半重建态 (资源管理器"读不出盘"属正常, 卷对象事后
  常被挂载管理器自动补上盘符)。第一轮修复 (assign 先于 format) 只挪了
  报错点, 同日复烧在同一只棒上又踩中 —— 竞态是概率性的, 必须根治。
  终版三段式: ①两个分区一次建完 (纯分区表操作, 无卷焦点需求);
  ②Python 侧轮询 Get-Volume 等每个卷枚举出来再落盘符
  (`_ensure_partition_letter`, 30×0.5s 封顶; 挂载管理器抢先自动补的
  盘符: 恰是目标则免 assign, 是别的则先摘再指); ③format 各自落在已
  枚举的卷上。任一段失败, 重跑同一 `burn --dual-entry` 命令 (clean
  开头) 即自愈。另: 杂牌大容量棒常为虚标容量, 当测试/交付盘前先做
  一次写满校验；
- **2026-09-22 真机复烧修复**: 已烧好盘的分区2带着 GPT 无盘符属性,
  复用时 `refresh` 直接 `assign letter` 挂不出卷根 → "分区2未能挂载到
  临时盘符"中止 (与旧 0x17 教训同构)。已改为挂盘符前先清属性 (仅 GPT
  盘发此命令), 收尾 finalize 把属性原样落回, **分区2最终形态不变**
  (同日用户再次确认: 分区2保持无盘符隐藏, 分区1 (1 GiB 底线) 才是
  拔插可见可读的入口)。
- **2026-09-22 复用路径分区1底线 (复盘补漏)**: 复用判据原本只看分区2
  内容 (keyinfo) 不看分区1尺寸 —— 底线抬到 1 GiB **之前**烧出的旧小
  分区1盘 (如 542 MiB) 重烧会静默保留小分区1。已补复用判据之二
  `_p1_below_target`: 分区1实际容量低于本轮应给尺寸 (含 1 GiB 底线),
  或分区1 RAW 读不出容量 → 全盘重建扩容 (照常过 confirm_wipe 确认),
  原因写入烧录告警。同日真机佐证: 那只白牌 115GB 棒复验时一次拔插后
  GPT 分区2条目消失 + 分区1 exFAT 变 RAW ("使用驱动器 F: 中的光盘之前
  需要将其格式化") —— 烧好的分区表自己坏 = 盘本身不可靠, **该棒淘汰,
  不作测试/交付盘**; 真盘 (14.6GB VendorCo) 重烧即可 (clean 起步, 全新
  1 GiB 分区1)。
- **2026-09-23 双入口落盘名定稿 (WIN)/(MAC) 后缀**: 分区1 两个阅览器入口
  改为 `图书阅览器(WIN)` 与 `图书阅览器(MAC).app` —— 收件人打开盘一眼
  可辨各属哪个平台。旧点前缀名 (`.图书阅览器`, 靠 Mac Finder 默认不显示
  点前缀项来隐身 Windows 入口) 弃用: 对端入口可见但无法运行 (Win 见 .app
  是普通文件夹, Mac 见 .exe 双击无效), 显式标注更稳。旧名常量保留为
  `*_DOTTED_LEGACY`, 仅用于重烧旧盘时清扫遗留 —— burner 的清扫/只读/
  回收/隐藏名单全部兼容"当前名 + 历史名"。收件人使用手册 v1.1 同步
  (PDF 需按平时方式从 md 重导)。**同日二次定稿见下条 (本版名称即历史)。**
- **2026-09-23 双入口落盘名二次定稿: 书名前缀 + 内主程序书名化**: 用户
  指令——分区1 两入口改为 `《欧阳竟无全集》《桂伯华全集》图书阅览器（WIN）`
  与 `《欧阳竟无全集》《桂伯华全集》图书阅览器（MAC）.app` (全角括号,
  按指令原文), 目录版收件人双击的内层主程序改为纯书名
  `《欧阳竟无全集》《桂伯华全集》.exe`。实现: 内主程序改名在烧录侧
  (整树拷贝+复读校验后 rename + 单文件复读, `_rename_dual_inner_exe`),
  **查看器产物无需重构** (onedir exe 可自由改名; .app 内部 CFBundleExecutable
  恒为 ASCII `vault_viewer`, 保 zip 中转抗性, Finder 只显示 .app 外名);
  半角后缀名转 `*_SUFFIXED_LEGACY` 入清扫名单。命名两次更替后, burner
  六处重复名单收敛为共享元组 `_ALL_EXE_NAMES` / `_ALL_VIEWER_DIR_NAMES`
  (下次改名只动 config)。手册 v1.2 同步 (PDF 需重导)。
  **注意**: 常量现已绑定本次交付的两部书名 —— 换书目交付时须改
  config 命名块并重跑 (烧录侧改动, 仍无需重构查看器)。
- **2026-09-23 双入口落盘名三次定稿: （适配Windows系统）/（适配MAC系统）后缀**:
  用户指令——两入口改为 `《欧阳竟无全集》《桂伯华全集》图书阅览器（适配Windows系统）`
  与 `《欧阳竟无全集》《桂伯华全集》图书阅览器（适配MAC系统）.app` (指令原文
  .app 名带尾随空格, 落盘不保留: Windows Win32 路径层会剥尾随空格/点,
  制造"看着叫这名实际叫别的"的歧义)。全角（WIN）/（MAC）名转
  `*_FULLWIDTH_LEGACY` 入清扫名单 (三代名: 点前缀 → 半角 → 全角 → 适配),
  内主程序纯书名不变; 仍是烧录侧改动, **查看器无需重构**。手册 v1.7
  同步 (PDF 需重导)。
- **2026-09-23 v2.4.5 书名 exe 语义变更: 真主程序书名化 → 自包含引导壳**:
  二次定稿的"内主程序改名"方案 (整树校验后把真主程序 rename 成书名) 在
  "书名 exe 被单独拷走"场景暴露缺陷 —— onedir exe 离了 `_internal` 死在
  PyInstaller C 装载器层, 弹英文 "Failed to load Python DLL" 而非谢幕文案
  (用户复测曝光)。本版起书名 exe = 微型 onefile 引导壳 (产物
  `vault_launcher.exe`, 烧录时拷入目录并改书名), 真主程序**保持构建名**
  `图书阅览器.exe` 由壳按名转交; 缺壳或缺真主程序都是烧录熔断项。这是
  **查看器行为变更** (版本号 2.4.4 → 2.4.5, 交付盘必须重构), 不再是纯
  烧录侧改动。
- **2026-09-23 v2.4.6 真棒事故: 盘上真主程序事后消失 → 隐藏 + 可分辨文案**:
  18:15 烧录 (burn 不变量核验过真主程序在盘) 的双入口盘, 复测时双击**盘上**
  书名 exe 竟弹"脱离U盘介质"——查实真主程序 `图书阅览器.exe` 已从盘上消失
  (Defender 历史为空; 烧录清扫仅根级不进查看器目录; 嫌疑排序 = 人工当旧版
  残留清掉 > 杀毒静默隔离)。根因是命名遗留: 真主程序顶着一代旧名裸露在
  书名 exe 旁, 看着就是"该清的旧文件"。两修: ① 真主程序在双入口目录内
  +H+S 隐藏 (判据 = 书名引导壳在旁, 单分区 onedir 唯一入口不藏; burn_dual
  与 lock_drive 两处同步) —— 推翻 v2.4.5 的"dropper 特征顾虑保持可见"
  权衡; ② 引导壳三态: 目录完整转交 / 单拷壳弹谢幕 / `_internal` 在而真主
  程序缺弹 BROKEN_INSTALL_MESSAGE ("阅览器文件不完整，请联系发件方重新
  烧录U盘")。**查看器行为变更** (2.4.5 → 2.4.6, 交付盘必须重构重烧);
  已烧的 v2.4.5 盘处于坏盘态, 重烧即修复。
- **2026-09-24 v2.4.7 Mac 侧观感隐藏: 分区2 卷标 + 密文链名全部点前缀**:
  用户问"MAC系统的加密分区部分可以隐藏嘛"。定案: 分区级不挂载在 Mac 无
  等效物 (阅览器靠 /Volumes 挂载点普通权限读; GPT 无盘符属性是 Windows
  专属语义), 只做观感层 —— ①分区2 卷标 `.资料存储` (Finder 不显示点前缀
  挂载点, 挂载照常; **卷标设置失败从仅告警升为熔断**); ②密文链名
  `.keyinfo.bin`/`.manifest.enc`/`.DATA` (macOS 不认 DOS hidden 位; 旧
  README"exFAT hidden 位映射 Finder 不可见"系无实证断言, 已撤回)。
  换名=换代: 复用判据只认新名, v2.4.0~2.4.6 明文名链重烧走全盘重建;
  旧名残留由 *_LEGACY 名清扫 (单分区旧盘重烧也不留残链)。**查看器行为
  变更** (2.4.6 → 2.4.7, 探测名进了查看器, 交付盘必须重构重烧); 手册
  v1.10 (Mac 桌面只见 图书资料 一个卷)。Finder 不显示点前缀挂载点/文件、
  挂载照常、.app 开门照常 —— Mac 真机核验列入验证手册第 3 步。
- **2026-09-24 Developer ID 签名+公证路线落地 (构建侧, 查看器行为不变,
  仍 v2.4.7)**: 用户购入 Apple Developer 账号并装好 "Developer ID
  Application" 证书。此前 Mac .app 只有 ad-hoc 签名, 换机即被 Gatekeeper
  拦 ("无法验证开发者"/"已损坏"), 手册靠"右键→打开"过渡 (macOS 15 已取消
  该旁路)。工具补两处公证硬前提: ①`sign_artifact` 传身份时自动加
  `--options runtime` + `--timestamp` (ad-hoc 不加); ②新增
  `patch_mac_bundle_metadata` 在签名前写合法 CFBundleIdentifier
  (`com.usbvault.vault-viewer`, 下划线不合法)+版本号 —— PyInstaller CLI
  无 spec 时落占位值, 直接交公证必拒。公证+装订流程见 1m⑤ (notarytool
  一次性存凭据, 之后每次构建三条命令)。烧录把 .app 拷为中文名目录
  不影响签名/票据 (签名不覆盖 bundle 目录名, 票据按 CDHash 匹配)。
  **Mac 构建机须先同步本版 usb_vault 目录再构建**。已公证产物烧盘后
  收件人双击零提示。
- **2026-09-25 烧录侧签名封条闸门 (不升号, 仍 v2.4.7)**: 真机事故 —— 公证
  Accepted 的 .app 烧上盘后收件人 Mac 弹「已损坏，无法打开」。取证 (Windows
  侧逐文件验 `_CodeSignature/CodeResources`): 盘上 .app 是"签名来自 A、文件树
  来自 B"的缝合产物 —— Mac dist 在签名之后混入了另一次构建 (Contents/ 下
  Frameworks/ 与 Resources/ 两棵真树并存, 封条只认一棵, 122 哈希不符 + 整棵
  多余树); 主程序本身是货真价实的 Developer ID 签名, 骨架/SEED 检查全部放行。
  修复分两层: ①流程 —— Mac 构建前 `rm -rf dist build` 清场、传回烧录机解压到
  全新空目录 (操作方案阶段 0/6 已写死); ②工具 —— burn 新增
  `_check_mac_app_seal`: 烧前把 .app 文件树与封条逐文件比对 (plist + sha1/
  sha256, Windows 上即可完成), 文件被改/缺/多、封条外多余文件、符号链接位
  失守一律熔断拒烧 —— 缝合产物到不了盘上。真机现盘 F: 已确认损坏, 须 Mac
  清场重建→重签→重公证→重装订→重烧。测试伪 .app 工厂同步产同构封条
  (test_burner/test_burner_dual `_write_seal`)。
- **2026-09-25(b) 根因二次修正 + 签名前去链接化 (不升号, 仍 v2.4.7)**: 当晚
  干净重走整链后烧录仍报封条不符 —— 深挖推翻上午"缝合产物"定论: 新 Mac
  构建本身完全自洽 (真正内容哈希不符 = 0), 真根因是**新版 PyInstaller 的
  .app 布局含 54 处符号链接** (Resources/⇄Frameworks/ 互链、PIL/.dylibs→
  __dot__dylibs、python3.14→python3__dot__14…), 而 Info-ZIP `zip -r` 打包默认
  **跟随**链接成副本 (zip 内 0 个链接、~494 处重复树), exFAT 又无链接语义 ——
  中转/烧录后封条登记的链接位必失守, Gatekeeper 必弹「已损坏」。上午闸门
  报的 628 处里 122"哈希不符"+12"缺失"实为两处封条格式误报 (旧版只认
  `link` 键、新版 codesign 用 `symlink` 键; 嵌套已签名代码条目只有
  cdhash/requirement 无内容哈希) —— 拒烧结论碰巧正确, 理由错了。修复:
  ①build-viewer 签名前 `_deref_mac_symlinks` 把链接拍平成真实拷贝 (目录
  链接优先/成链先拍目标/断链熔断), 封条登记纯文件树, zip/exFAT 怎么走都
  不变形; ②闸门改准确: link/symlink 双键识别、cdhash 条目验 Mach-O 嵌入
  签名魔数 (fade0cc0)、`._xxx` AppleDouble/.DS_Store 残留按 codesign 口径
  忽略、树内真实符号链接一律熔断 (exFAT 承载不了)。修复后闸门对真产物
  报 549 处、全部为真问题 (54 链接失守 + 494 封条外多余)。408 三绿
  (+7: 闸门 4 + 去链接化 3)。教训: 烧录机现存 dist/vault_viewer.app 及其
  zip 均为链接已丢失的废品, Mac 侧须同步最新代码整链重走 (阶段 5 加
  `find -type l | wc -l` 须为 0 的终验)。
- **2026-09-26(c) 拍平三分法 (不升号, 仍 v2.4.7)**: Mac 真机同步最新代码
  重走, `build-viewer --sign` 死在 codesign `bundle format unrecognized,
  invalid, or unsuitable` (指到 `Frameworks/PIL/.dylibs`) —— 根因: (b) 版
  拍平把**带点目录链接**落成了真目录, 而 codesign 规定 Frameworks 下只有
  .framework 目录许带点 (这正是 PyInstaller 6 把真目录起名 `__dot__dylibs`、
  另留 `.dylibs` 软链的原因, osx.py 源码注释原文)。定谳依据 = 烧录机本地
  同版 PyInstaller 6.22.2 源码: ①`utils/osx.py:545` 所有非系统依赖的 load
  command 一律改写成 `@rpath/<纯文件名>` (目录分量不进引用); ②Resources 侧
  点名目录 codesign 不禁。修复 = 三分法: Frameworks 下带点**目录**链接先拍
  完途经拷贝再**删除** (引用它的只有顶层文件链接, 拷后自成真文件); 带点
  **文件**链接 (python3.14) 与 Resources 下带点目录链接照旧拷贝; 另加拍平
  后不变量自检 (Frameworks 点名真目录除 .framework 外当场熔断报路径)。
  预解析改词法落真身 (normpath + 途经目录链接替换), 拷贝/删除互不依赖,
  顺带消灭"先删链接触发误报断链"。顺修: build() 里写死的 "ad-hoc codesign
  失败" 文案改中性 (误导过排障方向); 构建器自证标记更新为 2026-09-26。
  411 三绿 (+3)。阶段 5 终验加"本机先跑一次"步骤。
- **2026-09-26(d) 拍平收口 (不升号, 仍 v2.4.7)**: Mac 真机二连——(c) 版构建
  触发不变量熔断, Frameworks 下冒出带点真目录 (用户报作 "opt0.4")。根因:
  (c) 只删"带点名"目录链接, Frameworks 下**不带点**目录链接 (tcl8 类) 仍走
  拷贝, copytree 把 Resources 侧数据树的带点子目录 (Tcl/Tk 的 8.4/8.6 这类)
  原样搬进 Frameworks, 同样是 codesign 毒。定谳 (PyInstaller osx.py Step 2):
  Frameworks 侧目录链接**全部**是纯数据目录的镜像软链 (_DATA_DIR_TYPE: 真身
  在 Resources, 链接只维持两棵树观感一致), 运行期无人寻址 → 收口规则改为
  **Frameworks 下目录链接一律删除**, 唯一例外 .framework 内部 (合法区,
  Versions/Current 等规范结构照常拍平保真)。构建器自证标记升 2026-09-26(b)
  拍平修正二版 (与 Mac 上一版代码可辨)。413 三绿 (+2: 数据镜像删除回归 /
  .framework 内部目录链接保真回归)。**同日事故: 同步代码去 Mac 时烧录机
  src/app/usb_vault/ 被整目录剪切带走 (git HEAD 停在 v2.3.3, 差 12 个版本
  的工作树是唯一副本), 从 Mac 拷回恢复——双机摆渡永远用"复制", 且 v2.4.x
  起应尽快补一次 git 提交**。
- **2026-09-26(e) 拍平三连收口 (不升号, 仍 v2.4.7)**: Mac 真机三连——(d) 版
  构建过不变量, 死在 codesign `bundle format is ambiguous (could be app or
  framework)`。根因: (d) 留的".framework 内部照常拍平"例外恰恰是毒 —— 框架
  规范 (PyInstaller osx.py:197-199 转述 Apple 规则) 要求 **Versions/ 旁边
  只许站符号链接**; 把 Versions/Current、框架根的 Resources/Python 别名拍成
  真目录/真文件 = 非规范框架结构, codesign 面对无扩展名却带完整内容的目录
  无法分类 bundle。同源毒: Resources/Python.framework→../Frameworks/…
  整框架镜像链接落成拷贝时, copytree 跟随内层链接会在拷贝内部再造违规结构。
  终版规则: **.framework 子树内链接 (含文件别名) 一律删除; 名字以
  .framework 结尾的镜像链接一律删除**。dyld 不受影响: 装载路径全是版本化
  全路径 (…/Versions/3.14/Python) 或 @rpath/<纯文件名> (解析到 Frameworks
  顶层真实文件, 该顶层文件链接仍拍平)。构建器自证标记升 2026-09-26(c)
  拍平修正三版。414 三绿 (+1 净: Current 拍平回归翻转为删除回归, 新增
  整框架镜像删除回归)。
- **2026-09-26(f) 拍平四连收口·框架扁平化 (不升号, 仍 v2.4.7)**: Mac 真机
  四连——(e) 版死在 codesign `bundle format unrecognized … In subcomponent:
  …/Frameworks/Python.framework`。根因: (e) 删光框架内链接后, 剩下无
  Current 无根级别名的 `Versions/<ver>/{Python, Resources/Info.plist}` 残架
  codesign 仍不认 (PyInstaller utils/osx.py:672 自己的注释: Current "seems
  to be necessary for code signing")。版本化布局离不开链接, 而链接过不了
  zip/exFAT 中转 —— 唯一活路 = 链接删净后把 .framework 重构成**扁平布局**
  (二进制与 Resources 提到框架根, Versions/ 整体移除): 框架规范第 4 条
  (osx.py:196-199) 是条件式 —— "顶层**有** Versions 目录才禁他物站旁边",
  经典扁平框架合法。运行时零影响 (6.22.2 源码链全程实证): bootloader
  dlopen 的是 Frameworks 顶层锚点 `Python` (api.py 存纯 basename →
  bindepend 为非顶层二进制造顶层链接 → pyi_dylib_python.c 裸拼接 "%s/%s"
  直接 dlopen, 无框架路径魔法), 该顶层链接本就被拍平成真文件; 其余依赖
  load command 全是 @rpath/<纯文件名> → 同样解析到 Frameworks 顶层。
  新增 `_flatten_mac_frameworks` (挂 `_deref_mac_symlinks` 收敛分支, 意外
  结构熔断); 构建器自证标记升 2026-09-26(c) 拍平修正四版。415 三绿
  (+1 净: 框架内链接回归翻转为扁平化回归, 新增顶层锚点/已扁平框架回归)。
- **2026-09-26(g) 拍平五连收口·运行时寻址补偿 (不升号, 仍 v2.4.7)**: (f)
  版 Mac 真机 codesign **首过**, 但直跑死于 `ModuleNotFoundError: No module
  named '_struct'` (bootloader 最先导入的 pyimod01_archive 触发 `import
  struct`, struct.py 的 `from _struct import *` 找不到 C 扩展)。定谳:
  (c)(d) "Frameworks 目录链接一律删, 运行期无人寻址"的论证只覆盖 dylib 的
  @rpath 引用, 漏了**导入器按 sys.path 落盘探测扩展模块**这类寻址 ——
  PyInstaller 把标准库扩展落盘名改写成 `python3.14/lib-dynload/…`
  (building/utils.py:95-101, #5604/#9204), .app 里靠 "`python3.14` →
  `python3__dot__14`" 点名替换链接寻址, 链接一删路径即断 (struct 早于一切
  钩子, 无 Python 层可救); `_tcl_data`/`_tk_data` 数据镜像同理 (自带钩子
  pyi_rth__tkinter 只认 MEIPASS 下目录, 目录不在静默不设 → Tk 会接着死在
  "Can't find a usable init.tcl")。链接照删 (拷贝均触 codesign 点名关),
  补偿两件: ① `_hoist_mac_lib_dynload` 把 `python3__dot__14/lib-dynload`
  的 `.so` 提升到 Frameworks 顶层 (= sys.path 首项, FileFinder 自然命中),
  `python3.14` 链接两侧镜像都删不留死副本; ② `_write_mac_runtime_hook`
  生成 Mac 专用运行时钩子 (darwin 构建自动挂 `--runtime-hook`), 按 .app
  布局把 TCL_LIBRARY/TK_LIBRARY 指回 Resources 真树, 非 .app 布局空转,
  与自带钩子顺序无关。意外结构 (缺 lib-dynload/内含子目录/顶层撞名) 一律
  熔断。构建器自证标记升 2026-09-26(c) 拍平修正五版。418 三绿 (+3: 提升
  回归/撞名熔断/钩子重定向, 修复测试自身 monkeypatch.delenv 误还原
  TCL_LIBRARY 泄漏坑)。
- **2026-09-29(h) 拍平六连收口·公证 Resources 代码镜像 (不升号, 仍
  v2.4.7)**: 五版 Mac 真机构建+直跑通过后公证 `notarytool submit` 返回
  **Invalid**, log 被拒二进制全部在 `Contents/Resources/PIL/.dylibs/`
  (libwebpdemux/liblzma/libtiff…, 无一条指向 Frameworks), 报错两条:
  "not signed with a valid Developer ID certificate" + "no secure
  timestamp"。定谳: PyInstaller 双树布局里二进制真树在 Frameworks、
  Resources 侧只放**镜像链接** (目录镜像 Resources/PIL、顶层文件镜像
  Resources/libwebp.6.dylib); 拍平旧规则"Resources 下普通链接照拷"把
  镜像落成了真拷贝 —— 但外层 `codesign --deep` 只签代码位置
  (MacOS/Frameworks), Resources 下的散装 Mach-O 只封封条哈希**不当代码
  签名**, 于是这批镜像副本成了无主未签名二进制, 公证逐条拒 (09-25 那次
  Accepted 是因为当时还是链接, 公证 zip 用 ditto 保链接, 服务端看到的
  是链接条目)。修法: Resources 下链接的最终目标落在 Frameworks = 纯
  代码镜像, 目录/文件**一律删除** (运行时 MEIPASS=Frameworks 无人从
  Resources 装载代码, 删除是纯减重, 且吞并 (g) 的 python3.14 特例);
  新增公证前置不变量 `_assert_mac_resources_code_free` (Resources 下
  出现散装 .dylib/.so 当场熔断, 不等公证被拒才发现)。构建器自证标记升
  2026-09-29 拍平修正六版。452 三绿 (改 2: 顶层文件链接归位 Frameworks
  侧/删"Resources 照拷"旧前提测试; 增 2: 镜像删除回归 + Resources 二进制
  熔断)。
- **2026-09-29(d) 封条闸门三盲区修正·首个六版产物实测定谳 (烧录侧, 不升号
  仍 v2.4.7)**: 六版 Mac 产物 (公证+装订后传回) 首烧被闸门拦下 5 条, 实物
  解剖定谳**全部为闸门误报、产物本身完好** (修复后闸门对同一份 .app 全树
  比对零问题)。三盲区: ① 嵌套代码包 (扁平化 Python.framework) 在封条里是
  cdhash **目录**条目, 闸门一律按普通文件查 is_file → 误报"封条文件缺失"
  (实物: 目录在位, Python 二进制 5.5MB 带 fade0cc0, 内封条在包根
  _CodeSignature/ —— 扁平框架不在 Resources/ 下); ② 包内文件登记在包自己
  的内封条, 外封条不含, 多余扫描无"嵌套包子树"排除 → 误报"封条外多余";
  ③ codesign 对 Frameworks 顶层**非 Mach-O 数据文件**同样落 cdhash 条目且
  不写入嵌入签名 (base_library.zip/seed.bin 与 Resources 侧 hash2 锚定原件
  逐字节相同, 铁证) → 一律强求 SuperBlob 魔数误报"无嵌入签名魔数"。修:
  cdhash 条目按 codesign 语义分型 (真 Mach-O [feedface/feedfacf/反转/fat
  六魔数] → 仍须嵌入签名; 非 Mach-O 数据 → 在位即可); 目录条目=嵌套代码包
  (须有 _CodeSignature/CodeResources, 子树排除出多余扫描); 主程序 (原先
  allowed_extra 完全裸查) 加 Mach-O+魔数底线查 —— 它是外层签名+装订票据的
  载体。455 三绿 (改 2: cdhash 两测试假文件改真 Mach-O 头; 增 3: 非 Mach-O
  数据过/嵌套包过/缺内封条拦)。**教训: 闸门此前只被"坏产物"考验过 (09-25
  两轮), 首个"好产物"才暴露把正常形态当异常 —— 校验器的测试矩阵必须含
  正例**。
- **2026-09-29(i) rpath 归一化 + 依赖自包含/scrypt 双不变量·直跑解锁收口
  (构建侧不升号, 叠加在待交付 v2.4.8 上)**: (d) 闸门修正后原命令重烧, 收件 Mac 侧实测 .app
  **开窗正常、输解锁码后崩** `AttributeError: module 'hashlib' has no
  attribute 'scrypt'` (crypto.derive_data_key:75)。烧录机实物解剖 (Mach-O
  载入命令逐文件解析) 定谳: 产物里 `_hashlib`/libcrypto (OpenSSL 3.6.4)
  齐备且编译期就带 scrypt, 但 PyInstaller osx.py 布局契约 = **二进制真身按
  自然深度落盘 + 各自 rpath 按自身深度指向 Frameworks 顶层 + 顶层别名链接
  兜解析**; 拍平手术 (c)(g) 把顶层链接拷成真文件/把真身提升顶层, 文件挪了
  位置而字节里的 rpath (`@loader_path/../..`、`@loader_path/..`) 没跟着改
  → 提升副本在顶层再上一层解析到 `Contents/` 依赖全灭, 实测 11 文件 14 条
  (_hashlib/_ssl/_lzma/_decimal/_zstd/libmupdfcpp/libtiff/libwebp×3/libxcb)
  —— `_hashlib` 死后 **hashlib 静默降级** (sha256 走内置 _sha2 照常、唯独
  scrypt 属性消失), 开窗/弹码框全不受影响, 直到解锁派生才爆; 纵使过了解锁
  也轮到 PDF 渲染 (libmupdfcpp) / 图像解码 (libtiff/libwebp) 死。修
  (build_viewer, 签名前挂链): `_fix_mac_rpaths` 把走错楼的 @loader_path
  rpath **原位改写**回按当前真实位置指向 Frameworks 顶层 (只缩短 NUL 填充,
  载入命令长度与依赖声明不动; 深处原位真身 rpath 本来就对、零改动); 新增
  `_assert_mac_dylib_resolution` (全包每个 Mach-O 每条依赖模拟 dyld 解析,
  @rpath/@loader_path/@executable_path 展开须命中包内真文件、系统库放行、
  绝对 rpath/裸文件名/外部绝对路径一律熔断 —— 比 dyld 更严, 构建机上"碰巧
  能解析"正是要防的事故) 与 `_assert_mac_scrypt_ready` (_hashlib 与
  libcrypto 均须含 scrypt 字符串; LibreSSL 系 Python 编译期即无, 静态可判)。
  烧录机实物副本演练: 修前不变量熔断 14 条 → 归一化恰改 11 条 → 修后全包
  自包含 + scrypt 双件齐备。构建器自证标记升 **2026-09-29 拍平修正七版**。
  483 测试 481 绿 (+10: 解析往返/截断熔断/提升改写/原位零改动/变长熔断/
  真机布局复刻/非自包含两类/主程序 rpath 通路/scrypt 四态); 2 失败为
  ntfs_guard 在**管理员提权 shell** 下 deny-DE 拦不住删除的环境性行为 (与
  本改动无交集, 待非提权会话复跑)。**教训: "直跑看开窗"验不到深链 —— 静默
  降级类缺陷 (属性消失而非报错) 必须构建期静态不变量把关**。烧录机现存
  dist 及今日所烧棒 Mac 侧作废, Mac 同步七版代码后整链重走 (清场→构建→
  公证→装订→传回空目录→重烧)。
- **2026-09-23 分区1卷标恒落 title (Untitled 补漏)**: 真机发现未带
  `--set-label` 的双入口烧录, 分区1 在 Mac Finder 显示 Untitled / Windows
  显示 新加卷, 与收件人手册承诺的 图书资料/资料存储 两卷名不符。修复:
  双入口烧录在收尾固定对分区1 调 `set_volume_label(drive, title[:32])`
  (失败仅告警不阻断), 分区2 资料存储 卷标逻辑不变; `--set-label` 仅对
  单分区模式仍有开关意义。测试烧录 (默认 title=资料) 也不再出 Untitled 盘。
- **2026-09-29 跨平台实机裁决 + verify/heal 自愈 (不升号, 仍 v2.4.7)**:
  收件人 NTFS 新烧盘往返 Win→Mac→Win 实测: Mac 桌面显示 `.资料存储` 卷
  (点前缀卷隐藏**证伪**), 回 Windows 后分区2 带盘符现形 (macOS 改写 GPT
  剥无盘符属性位)。评审定案保持双分区 (Mac 侧现形为空卷可接受) +
  烧录机侧自愈: `verify` 增分区2隐藏态检查 (Get-Partition GptAttributes/
  DriveLetter, 普通权限), 新子命令 `heal` 摘盘符+重落属性+读回后验
  (须管理员), 复烧路径经 finalize 本就自愈。查看器行为不变。**附带
  mypy 抓出 2026-09-28 遗留真 bug**: `_volume_fs` 引用未导入的
  `volume_id` 模块名, NameError 被 `except` 吞成空串 → 真机上双分区
  复用判据恒判"非 NTFS"误走全盘重建 (测试注入掩盖此路); 修=直调已
  导入的 `get_volume_fs` + 回归钉。449 三绿 (+18)。详见功能段
  "2026-09-29 跨平台实机裁决"。
- **2026-09-29 v2.4.8 拷离介质闸门 (用户实测曝光 v2.4.4 边界)**: 用户把
  整个 `《欧阳竟无全集》《桂伯华全集》图书阅览器（适配Windows系统）`
  目录拷到电脑任意位置后双击书名 exe, **仍能正常打开阅读** —— 原盘当时
  插在同一台机器上, `_resolve_data_drive` 的全机卷扫描找到了原盘隐藏
  分区2 的加密链照常开门 (v2.4.4 只拦"全机无载体", 即原盘不在机一种
  形态; 用户要求 Windows/Mac 双平台一律弹"脱离U盘介质")。修 (评审定案
  方案 A 介质闸门): `_startup_gate` 最前端加 `_exe_on_removable_media`,
  仅冻结构建 (`sys.frozen`) 启用 (烧录机 `view` 源码预览不受影响);
  Windows = `GetDriveType(自身盘)==removable`, macOS = 新增
  `mac_volume.is_on_external_media` (上溯 `/Volumes/<卷名>` 挂载点 +
  diskutil `Internal=false`; 内置数据卷同样挂 /Volumes 下, 不能只看
  路径); 判不出按不在可移动介质处理 (fail-closed 谢幕, 文案 =
  config.DETACHED_MESSAGE 原文)。残余边界 (诚实声明): 拷到**另一支可
  移动盘**且原盘同机在场仍可读, 堵它需阅览器与加密链同物理盘校验
  (评审时方案 B, 用户选 A 未做)。**前置红线: 换交付棒型必须先验
  DriveType=removable / diskutil Internal=false** (探针时真棒不在机,
  个别 USB-SSD 类棒报 fixed 会被硬闸误伤)。**查看器行为变更: 交付前
  双平台重构 + 重烧, 标题须显 v2.4.8**。`--selfcheck` 同步加**自身介质预演**
  行 (镜像启动门禁顺序; 冻构建不在可移动介质 = ✗ 退出码 1 —— 构建机/收件
  人侧一条命令即可分辨拷贝件; 真棒插上跑自检, 介质类型直接报出, 即换棒
  红线的核验通道)。474 三绿 (455→474, +19: viewer 判据与接线顺序 9 +
  mac_volume 外置介质判据 7 + selfcheck 预演 3)。
  **2026-09-30 构建侧后果 (文档修, 代码不动)**: 用户在 Mac 构建机直跑
  dist 副本弹「脱离U盘介质」误判为故障 —— 闸门只看阅览器自身介质, 固定盘
  dist + 真棒在场照样谢幕, **属按设计**; 操作方案阶段 5 旧"dist 直跑能开窗
  即过"自此失效, 已改「①dist `--selfcheck` (自身介质行按设计 ✗, 其余须
  全 ✔) + ②外置副本 (备用棒或 hdiutil 磁盘映像) 拷 dist 产物、真棒在场,
  从外置副本输码过解锁翻 PDF」两步; 速查表补判别行 (双击的若确是U盘卷内
  阅览器, 以棒上 `--selfcheck` 的自身介质/数据卷两行分流: 前者=diskutil
  判据问题, 后者=分区2 未挂载按预案回退 exFAT)。

- **2026-09-30 v2.4.9 介质闸门 darwin 判据三级级联 (真机事故, 修
  v2.4.8 真棒误杀)**: 用户在收件 Mac (macOS 26) 上**从U盘卷内**双击
  阅览器仍弹「脱离U盘介质」—— 实锤探针链: 盘上构建确是 v2.4.8、
  `.资料存储` 已挂载且加密链齐全、文本 `diskutil info` 明明报
  Device Location: External / Removable Media: Removable, 但**卷级
  `diskutil info --plist` 缺 `Internal` 键** (FSKit 时代 NTFS 挂载的
  plist 裁剪)。旧 `is_on_external_media` 单通道读不出即
  `VolumeIdError`, 启动闸门 fail-closed 把**真棒错杀** —— 系统明说
  是外置盘, 我们的判据问错了地方。修: 判据改三级级联 (①挂载点
  plist `Internal` → 缺则同源旁证 `DeviceLocation=external` (同一
  IOKit 盘属性渲染出文本输出, 不引新事实源) → ②`mount` 反查设备
  节点问切片 `/dev/disk4s1` → ③归整盘 `/dev/disk4` —— 整盘信息
  不经文件系统栈, fskit 卷缺的键在这里仍在), 全通道灭才抛; `Internal`
  在场恒权威 (级联不许越过 `Internal=true` 翻案)。selfcheck 的 ✗ 行
  同步复跑判据带出各通道原因, 现场截图即自诊断。收件人视角行为不变
  (真棒照开、拷贝件照谢幕)。**查看器行为变更: 交付前双平台重构 +
  重烧, 标题须显 v2.4.9**; 今日所烧棒 Mac 侧作废。486 三绿
  (481→486: 级联判据 7 = 正例 3 + 内置反例 2 + 全通道灭/无设备 2,
  含 09-29(d) "校验器须有正例"教训的落地) + ruff/mypy --strict 全绿。

- **2026-10-01 v2.4.10 diskutil 探测双写法 (2026-10-01 真机复测推翻
  v2.4.9 机制推断)**: v2.4.9 上棒复测**仍谢幕**, 但 selfcheck 的三通道
  死因逐条带出后真相变了 —— 三条全是 `diskutil 退出码 1: Usage: diskutil
  info[rmation] [-plist]`, 连纯 ASCII 的 `/dev/disk4` 都拒: 不是"plist
  缺 Internal 键"、与卷名/编码无关, 是该 **macOS 26 的 diskutil 拒收
  双横线 `--plist` 写法** (其用法自述即单横线)。v2.4.9 级联三通道共用
  同一写法 = **公共单点故障, 级联形同虚设**。修: 全模块唯一探测点
  `_diskutil_plist` 按 `(-plist, --plist)` 双写法探测 (单横线是该机用法
  文本自载形态且历来各版通用, 失败补双横线兜旧机型); **三个调用点全部
  改走它** —— 介质判据级联、免特权 UUID 通道 (v2.4.10 前在这台机器上
  被 `--plist` 闷死, 只剩管理员授权框一条路)、管理员授权框的设备定位
  `_device_identifier` (原先靠 mount 反查兜底才没炸, 现在两路都有)。
  收件人视角行为不变。**查看器行为变更: 交付前双平台重构 + 重烧, 标题
  须显 v2.4.10**; v2.4.9 所烧棒作废。491 三绿 (486→491: 真机形态零重试
  直达 / 反形态重试兜底 / 双写法全灭报因 / 真机全形态级联回放 / UUID
  通道复活)。**教训: 级联的通道必须真正独立 —— 共用同一底层调用的
  多通道, 只是同一个单点穿了三件衣服; selfcheck 带死因的价值在这次
  复测里直接兑现 (用户贴一行输出即定谳)**。

- **2026-10-02 v2.4.11 macOS 防截屏 (用户收件 Mac 微信截屏实锤旧防护
  从未生效)**: 用户在收件 Mac 用微信截屏工具拍到了阅览器内容 ——
  Windows 侧 v2.3.4 起黑框, Mac 侧一直漏。排查定谳: `mac_protection`
  一直存在但**静默失效**, 与 v2.3.3 Windows 防截屏事故同款病换平台
  复发 —— ①只设 `mainWindow` 一个 NSWindow (解锁码框等 Toplevel 各有
  自己的 NSWindow, 一个罩不全); ②`4×400ms` 重试预算, U盘冷启动慢于
  预算即永久裸奔; ③失败无日志、自检无核验 ("调了没验、坏了没痕")。
  修: `protect_all_windows` 对 `NSApp.windows` **逐窗** `setSharingType:`
  并**回读核验** (不信设置调用本身, ntfs_guard 同款原则); 
  `start_window_protection` 常驻 2s 重刷 (后开的对话框窗口自动纳入,
  root 销毁后 after 抛错自然停摆); 状态恶化打一行 stderr (一次/转变,
  对齐 win_protection "失败必留痕"); selfcheck 新增"防截屏"核验行
  (隐藏探测窗未映射进 NSApp.windows 不算失败, 露窗后自动纳入)。
  边界不变: 防不住手机翻拍; NSWindowSharingNone 挡 CGWindowList 捕获
  管线 (系统 ⌘⇧3/4、screencapture 及以此实现的第三方工具), **微信
  截屏真机复测是本次重建的交付验收项** (见实证测试第 5 条)。**查看器
  行为变更: 交付前双平台重构 + 重烧, 标题须显 v2.4.11** (与待办的
  v2.4.10 重建合并一次覆盖); 今日之前所烧棒 Mac 侧防截屏视为无。
  500 三绿 (491→500: 非 darwin 拒绝/objc 缺失留痕/全窗口回读/部分
  生效即 fail/无窗口可重试/常驻循环不停/恶化留痕一次/首tick失败留痕/
  root 销毁停摆 + selfcheck 防截屏两行; ntfs_guard 2 例为管理员提权
  shell 环境性失败与本改零交集, 验收以手册"无管理员账户试删"为准) +
  ruff/mypy --strict 全绿。**教训: "设置类防护"的失败模式 (调了没验、
  坏了没痕) 在 Windows 侧已付过学费, 换平台重写时没把教训带过去 ——
  平台移植必须重审"失败如何显形", 不是只翻译 API 调用**。

- **2026-10-02 双架构 Mac 交付 (烧录侧, 不升号 —— 查看器零改动仍
  v2.4.11)**: 用户问 ARM Mac 时代如何兼容 Intel Mac。三路评审: ②
  universal2 单 .app 被否 —— 拍平/rpath/scrypt 三道构建期不变量都锚定
  **瘦魔数** (`_MACHO_MAGICS` 四瘦刻意不认 fat/cafebabe), fat 二进制会
  **静默绕过全部三道**, 正是 09-29(f) "静默降级"病类, 加固链等于对
  一半产物失效; ③ Intel-only + Rosetta 被否 —— 收件人首装 Rosetta 要
  联网 (离线交付冲突) 且渲染性能打折。**定案① 双 .app 按芯片命名**:
  Apple 版原生构建照旧, Intel 版在构建 Mac 上用 python.org universal2
  Python + `arch -x86_64` venv 跑同一条 build-viewer 链 (x86_64 瘦魔数
  本就在 `_MACHO_MAGICS` 里, 拍平/签名/公证/闸门全链零改动), 公证
  staple 后 `mv vault_viewer.app vault_viewer_intel.app` (目录名不在
  签名封条内, 改名安全) 再 zip。烧录侧: 落盘名
  `（适配MAC-Apple芯片）/（适配MAC-Intel芯片）.app` (旧（适配MAC系统）
  .app 转 legacy 名单, 换名=换代自动清扫重建; **名待用户定稿, config
  一行可换**); 双入口模式缺 Intel 版**熔断拒烧**, 单入口保持 Apple 版
  可选; 两 .app 各过结构/内嵌 SEED/封条三道闸门; 分区1 尺寸含两 .app
  字节。收件人按 关于本机→芯片 选入口 (手册 v1.14)。Intel 真机走同一
  套阶段5 三步终验。502 测试 (500 绿 + 2 ntfs_guard 提权环境性) +
  ruff/mypy --strict 全绿。

**收件人侧寻址 (双平台查看器已内置)**: 阅览器启动时若自身所在卷没有
keyinfo, 自动扫候选卷逐个 GCM 试解仲裁出真正的数据卷 —— Mac 扫
`/Volumes` 兄弟卷; **Windows 扫 `\\?\Volume{GUID}\` 无盘符卷 (正主) +
其余带盘符卷 (mbr 兜底)**, 单分区盘首个候选即自身卷直接命中, 行为与
旧版一致。个别拒访卷 (如 ESP) 按卷跳过。
**Mac 排障**: Finder 桌面/边栏显示分区1 (`图书资料`), **可能同时显示
分区2** (`.资料存储` 点前缀卷 —— 2026-09-29 收件人 Mac 实机证伪了"点前缀
卷不显示"的旧断言, 各 macOS 版本行为不一), 挂载照常发生在
`/Volumes/.资料存储`; 卷内文件同为点前缀名, **打开只见空白**。分区2
完全不挂载没有等效物: 阅览器靠 `/Volumes` 挂载点普通权限读它, 改类型
GUID 让 Mac 不认则 Windows 也不再枚举, 两头都得管理员裸读, 得不偿失。
若 Finder 连分区1 都未见, `diskutil list external` 找设备名后
`sudo diskutil mountDisk /dev/diskN`。**盘从 Mac 回到 Windows 后**:
macOS 改写 GPT 会剥掉无盘符属性位, Windows 可能给分区2 补上盘符
(显示为空卷, 内容仍 +H+S 双门槛不可见) —— 烧录机跑 `verify` 应报
"分区2隐藏形态: 已失效", 跑 `heal --drive <盘符>:` 免重烧恢复, 再 verify
应回"在位 ✔"。

**诚实声明**: 无盘符/GPT 属性是**整理性隐藏, 不是安全边界** —— 磁盘
管理/diskutil 能看到分区, 补个盘符就能让它现身。"不可读"由密文 + 序列号
绑定 + 解锁码保证 (安全边界与单分区版完全一致); 隐藏挡的是"检查时盘面
观感", 挡不住有意逆向。序列号可被 VolumeID 类工具伪造是另行声明过的
边界。

### 真机验证手册 (操作者执行 —— gpt 制式转正前须完整走一遍)

只在**空置备用盘**上操作 (F: 15GB 测试棒专用, 绝不接真资料盘):

1. **首烧**: 管理员终端 `burn --source <样例资料> --drive <备用盘>: --dual-entry --title 图书资料`
   (默认 gpt 制式), 核对确认提示里的磁盘号/型号/容量后输入 yes;
   (v2.4.0 起双入口盘分区1卷标**恒落 title**, 不再依赖 `--set-label` ——
   2026-09-23 真机教训: 没设卷标的 exFAT 在 Mac Finder 显示 Untitled /
   Windows 显示 新加卷, 与收件人手册承诺的 图书资料/资料存储 两卷名不符;
   `--set-label` 仅对单分区模式仍有开关意义);
2. **Windows 核验**: `Get-Partition -DiskNumber N` 见 2 个分区且分区2 无
   盘符、GptType 为基本数据 GUID; 资源管理器只见分区1 (阅览器可见、
   无任何密文文件), `《欧阳竟无全集》《桂伯华全集》图书阅览器（适配Windows系统）`
   内书名主程序双击可开门 (v2.4.5 起书名 exe 为引导壳, 无缝转交旁边的
   `图书阅览器.exe`, 收件人无感知; v2.4.6 起真主程序与 `_internal` 均
   +H+S 隐藏, 文件夹里可见入口只剩书名 exe —— 核对隐藏属性在位);
2b. **无盘符卷可读性 (F: 棒定案)**: 非管理员 PowerShell 跑
   `Get-ChildItem '\\?\Volume{<分区2 GUID}\'` (GUID 由 `mountvol /L`
   或 Get-Partition 查), 确认能列出 .keyinfo.bin —— 这是反转布局的
   地基, 探针已在系统盘验证, 须在真棒复测一次;
2c. **NTFS 形态与防误删 (2026-09-28 起)**: `Get-Volume` 确认两分区
   FileSystem 均为 NTFS; 资源管理器对阅览器目录 Shift+Del、`cmd`
   里 `del /f`、PowerShell `Remove-Item -Force` **全部须被拒**;
   正常双击开门阅读不受影响; `verify --drive` 末尾应打
   "防误删 ACL: 齐全 ✔"; 在**无管理员权限**的账户同样试删一次
   (收件人真实场景);
2d. **Mac 侧 NTFS 表现 (2026-09-29 实机已有首批数据, 不符即回退评估)**:
   插盘确认 P1 挂载为只读、.app 能双击运行开门 (NTFS 上运行 .app 无
   先例, DMG 同机理 —— 仍待真机); P2 (NTFS 点前缀卷) 挂载照常, 实机
   表现 = 桌面/边栏**可能显示** `.资料存储` (点前缀卷隐藏已证伪, 打开
   只见空白属预期); 任一不符记录现象并回退评估 (回退=重建 exFAT 双
   分区, 放弃防误删);
3. **Mac 核验 (2026-09-29 改版)**: 插盘看 Finder —— `图书资料` 卷在,
   `.资料存储` 卷**显示与否都属预期** (各 macOS 版本对点前缀卷行为不一)
   → 终端 `ls /Volumes/` 应见 `.资料存储` (挂载照常) → 双击 .app 走
   兄弟卷扫描开门 (输入解锁码/授权后能列出资料) → `ls -A
   /Volumes/.资料存储/` 应只见点前缀密文名 (Finder 打开该卷无可见文件);
4. **换 Windows 机核验 (gpt 制式决定性)**: 盘插**另一台** Windows, 确认
   分区2 依然无盘符不现身 (属性随盘走; 这正是 gpt 与 mbr 的分水岭),
   阅览器照常开门;
4b. **Mac 往返自愈 (2026-09-29 新增, 跨平台实机教训)**: 盘在 Mac 上
   挂载/推出后再插回烧录机 Windows —— 跑 `verify --drive <盘符>:`:
   若打"分区2隐藏形态: 已失效 ✘" (macOS 剥了无盘符属性位, 有时分区2
   还挂着盘符) 属已知行为 → 跑 `heal --drive <盘符>:` (管理员) 恢复 →
   重跑 verify 应回"分区2隐藏属性: 在位 ✔"; 若 Mac 往返后属性仍在位,
   也留档 (说明该 macOS 版本未改写分区表);
5. **重烧复用**: 同盘再跑一次 `burn --dual-entry`, 确认无清盘确认、
   分区2 加密链原位重写、分区1 阅览器已更新 (2026-09-28 前 exFAT 形态
   旧盘重烧会出现"换格式=换代"提示并走清盘重建 —— 属预期);
6. **旧版双分区盘升级**: 对分区2=.app 的旧盘重烧, 确认出现"分区2内容
   非本工具加密链…将全盘重建"提示并走清盘确认;
7. **换机仿真**: 盘插另一台 Windows / Mac 各验证一次查看器开门;
8. **PDF 缩略图核验 (v2.4.2 起)**: 打开大部头 PDF —— 正文左侧出现单列
   页缩略图 (懒加载渐进填充, 不冻结), 翻页/输页码时蓝框高亮跟页, 点击
   任一缩略图直达该页, 面板 × 与工具栏"缩略图"勾选框互为开关;
   **Mac 侧同验** (Tk 大坐标 canvas 是平台差异点, Windows 已探针实证);
8c. **高分屏 / 二合一平板核验 (v2.4.3 起)**: 在系统显示设置 ≥150% 缩放
   的机器 (或 Surface 类二合一) 上打开阅览器 —— 左栏文件名完整可读、
   列宽与文字同比放大; 拖动左栏与正文间的**分隔条**或点左栏顶部
   **◀/▶ 按钮**调整宽度有效; 缩略图面板与翻页按钮等比放大不挤压;
8b. **干净 VM AV 观察**: 双入口盘在干净 Windows VM 静置观察 Defender;
9. **结论回填**: 把各步结果 (OS 版本 + 现象 + 截图) 记录回本节。

## CLI 一览

| 命令 | 作用 |
|---|---|
| `burn --source DIR --drive E: --title T [--expire D] [--max-opens N] [--password] [--code-archive P] [--no-convert] [--viewer-exe P] [--viewer-mac P] [--converter B] [--av-cmd T] [--skip-scan] [--set-label] [--dual-entry [--dual-scheme gpt\|mbr] [--yes]]` | 烧录 (含烧录前病毒扫描; `--dual-entry`=双分区, 见专节) |
| `lock --drive E:` | 对已烧录U盘补加/恢复 只读+隐藏 (免重烧) |
| `heal --drive E:` | 恢复分区2隐藏形态 (macOS 用过的盘; 摘盘符+重落属性位, 须管理员) |
| `verify --drive E: [--code C]` | 全量复检 (密文哈希→解密→明文哈希; 含防误删 ACL 与分区2隐藏态检查) |
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

## 性能探针 (大书装载计时, Phase 0)

排查"大书打开慢"的**归因**工具 (2026-09-24): 环境变量 `USBVAULT_LOAD_TIMER`
门控, 不设 = 收件人可见行为零差异 (各调用点只多一次开关判断), 故不升版本号;
探针构建产物仅烧录机本地用完即弃, **不上盘不算交付版本** (若某天要把探针构
烧上盘交付, 先升号再烧)。

- **开启**: 设 `USBVAULT_LOAD_TIMER=1` 后启动阅览器。每次打开书, 悬浮
  "装载计时"窗逐行追加分段耗时 (同时打 stderr, dev 终端可见; `--windowed`
  产物 stderr=None, 探针窗是唯一出口):

  ```
  [书名] 装载 读盘 12.34s (152.3MB, 12.3MB/s) | 解密+拼接 0.21s | 整本拷贝 0.08s
  [书名] 构造 fitz.open+首页元数据 0.31s | 工具栏 0.05s | 缩略图占位卡 0.19s
  [书名] 首帧 提交→上屏 0.87s (栅格 0.78s)
  ```

  归因口径: "读盘"=U盘顺序读 (TimedReader 只包 read 单独计时);
  "解密+拼接"=装载循环剩余 (含 bytearray 拼接的 Python 开销, 不是纯解密);
  "首帧 提交→上屏"=含渲染队列等待与 30ms 轮询周期。

- **dev 运行** (烧录机插真棒; `--drive` = 阅览器所在分区1 的盘符):

  ```powershell
  $env:USBVAULT_LOAD_TIMER = "1"
  python -m src.app.usb_vault.main view --drive F:
  ```

- **裸读对照** (排除杀软过滤驱动等因素): 同一根棒上量最大 .enc 的纯读吞吐,
  与探针"读盘"行的 MB/s 对比 —— 探针明显低于裸读 => 中间层有额外开销
  (杀软/路径层), 可优化; 接近裸读 => 棒的硬件速度就是地板, 软件层已无事
  可做, 绝对提速只能换 USB 3.0 棒:

  ```powershell
  $v = (Get-Partition | Where-Object {
          !$_.DriveLetter -and $_.DiskNumber -in (Get-Disk | Where-Object BusType -eq 'USB').Number
        }).AccessPaths | Select-Object -First 1
  $f = Get-ChildItem -Force "$v.DATA" -Filter *.enc |
       Sort-Object Length -Descending | Select-Object -First 1
  $t = Measure-Command { $null = [IO.File]::ReadAllBytes($f.FullName) }
  "{0:N1} MB/s  ({1:N1} MB)" -f ($f.Length/1MB/$t.TotalSeconds), ($f.Length/1MB)
  ```

  注: 密文链在隐藏目录内, 必须 `-Force` (v2.4.7 起目录名 `.DATA`, 旧盘 `DATA`)。

## 防得住 / 防不住 (诚实声明)

**防得住:**

| 场景 | 结果 |
|---|---|
| 资源管理器里根本看不到密文载体 | `keyinfo/manifest/DATA/_internal` Windows 侧 +H+S 隐藏; Mac 侧点前缀名 (v2.4.7), Finder/ls 默认不显示 —— 默认视图无从选中 |
| 强行显示隐藏文件后对密文 Ctrl+C | 拷走的是无头密文: 换盘解不开 (序列号绑定), 无 SEED/解锁码永远解不开; 同盘粘贴出的只是随机名孤儿文件, 查看器不认 |
| 资源管理器把 DATA 文件拖到硬盘/发给别人 | 拿到的是无头密文, 无文件名无法解密 |
| 只拷阅览器 (不含加密链) 到其他电脑/硬盘运行 | "脱离U盘介质，加密书稿无法在该设备上打开查看" (整目录拷贝 v2.4.4 起; **单拷书名 exe** v2.4.5 起 —— 引导壳自包含, 残缺目录直接谢幕; 旧版两情形分别误报"该盘不是资料盘"/英文 "Failed to load Python DLL"); **盘上真主程序被删/被杀毒隔离** v2.4.6 起弹"阅览器文件不完整，请联系发件方重新烧录U盘" (与拷离谢幕可分辨, 真棒事故前两态同文案误导排障); **拷到电脑后即使原盘同机插着** v2.4.8 起同样谢幕 (自身介质闸门: 冻结构建必须运行在可移动介质上, Win/Mac 同) |
| 整盘内容拷到另一只U盘/硬盘目录再运行查看器 | "仅限指定U盘使用" (卷序列号不符, 解不开; Win/Mac 同) |
| 在查看器里 Ctrl+C/⌘C / 右键复制 / 另存 / 拖出 | 全部不存在该路径 |
| OBS/截图工具截查看器窗口 | Windows: 黑框 (SetWindowDisplayAffinity)。**v2.3.4 起才真黑**: 此前一直误设在 Tk `winfo_id()` 返回的内层子 HWND 上, 系统静默拒绝、失败又被吞 → 防护从未生效 (2026-09-20 收件人真机实测微信截图/FSCapture 录屏穿透曝光; 修复探针实证: 对 `GetAncestor(GA_ROOT)` 顶层窗口设置后 GDI BitBlt 与 PrintWindow 采样均黑)。**旧产物必须全部重烧**; macOS: **v2.4.11 起才真生效** —— 2026-10-01 用户实测微信截屏穿透曝光旧版 (只设 mainWindow 一次, 静默裸奔); 现为全窗口 + 常驻重刷 + 回读核验 (NSWindowSharingNone), 重建后微信截图/⌘⇧4/录屏复测为交付验收项 |
| PrtScr | 被吞掉 (窗口持焦点时; macOS 系统截图经 CGWindowList, 同样拍不到) |
| 收件人改系统时间续期 | 仅拖延有效期判断 (见下"防不住"末行) |
| 程序覆写/篡改盘上产物 (含阅读器文件) | 只读位生效: 改写与写打开被系统拒绝, `cmd del`/`Remove-Item` 不带强制参数删不动 |
| **收件人误删阅读器/密文 (资源管理器删文件/整目录/Shift+Del、cmd del /f、Remove-Item -Force)** | **NTFS 交付形态 (2026-09-28 起) 全部被拒**: 分区根 deny (DC) + 交付树 deny (OI)(CI)(DE,DC) 两级封死删除通道, 探针六路实测全 BLOCKED; 改名/移动同被拒; 读取/正常阅读不受影响 (配方见"NTFS 交付形态与防误删"节)。**旧 exFAT 盘 (2026-09-28 前) 无此防线, 重烧即换代获得** |
| 篡改盘上密文 | GCM tag 校验失败, verify 报错 |
| U盘插到"另一个 OS"看到对端查看器 (Win 见 .app / Mac 见 .exe) | 双分区: 分区2 无盘符 (gpt 无盘符属性 / mbr 摘盘符), Windows 不分配盘符; 分区1 上双入口用 书名前缀 + （适配Windows系统）/（适配MAC-Apple芯片）/（适配MAC-Intel芯片）后缀名显式标注 (2026-09-23 三度定稿, 2026-10-02 Mac 侧双架构拆分, 旧（适配MAC系统）/点前缀/半角/全角后缀名均弃用换名即换代) —— 对端入口可见但无法运行: Win 见 .app 为普通文件夹, Mac 见 .exe 双击无效 |
| Intel Mac 双击了 Apple 芯片版 .app (或反之) | 极少数未装 Rosetta 2 的 Intel 机可能弹"无法打开"或闪退 —— 属入口选错, 按 关于本机→芯片 换另一个 Mac 入口即可 (2026-10-02 双架构起; 名待用户定稿) |

**防不住 (软件方案的物理边界):**

| 场景 | 说明 |
|---|---|
| **手机翻拍屏幕** | 无解。极高敏资料请配合"少量分发+水印+追责"管理手段 |
| **VolumeID 类工具伪造卷序列号后整盘克隆** | 序列号是公开值, 可被改成与原盘相同; 克隆盘即可打开。**例外: `--password` 盘克隆后仍需 8 位解锁码 (30^8≈6.6e11, 配合 scrypt 内存硬度离线穷举代价高昂; 但低于旧 16 位方案 —— 码渠道保密是前提)** |
| **逆向查看器提取 SEED** | PyInstaller 包可被解包。**`--password` 盘提取 SEED 仍解不开** (码不落在盘上/exe 里); 无码盘则可离线解密该 SEED 烧的所有盘 —— 提高门槛: 每批 `new-seed` 轮换、必要时 Nuitka 重打包 |
| **内存取证 / 进程注入** | 内容展示时明文必然短暂存在于内存 |
| **管理员摘除防误删 ACL / 格式化重烧** | 防误删是防**误**删不是防蓄意: 所有者/管理员 `icacls <盘> /remove:d *S-1-1-0 /t` 整树摘除后可删可改, 格式化/diskpart clean 是卷级操作 ACL 无从干涉 (应对: 重烧或换盘); 只读属性同此 (整理性防护)。管理员拿到的仍是密文 (`--password` 盘还需解锁码) |
| **旧 exFAT 盘 (2026-09-28 前烧的) 误删** | exFAT 无 ACL, 只读位会被资源管理器剥掉再删 (2026-09-20 真机实测) —— 重烧即换代为 NTFS 形态获得防误删; macOS Finder 删除行为同属此列 (NTFS 盘在 Mac 侧原生只读挂载, 本就删不了, 待真机留档) |
| **"右击文件→复制"这个动作本身** | 用户态拦不住 —— 收件用户能读就能拷, 任何文件系统/属性组合都一样 (连加密 DVD 都如此; 商业"U盘防拷贝系统"靠签名内核过滤驱动, 是另一类产品形态)。本工具的对应防线是**拷了也没用**: 密文 + 盘序列号绑定 + 默认视图不可见 (见上表前三行) |
| **打开次数上限** | 计数存收件人本机 (Win: `%LOCALAPPDATA%`; mac: `~/Library/Application Support`): 按机计数、可删、换机清零 —— 纯软约束, 默认不开启 |
| **改系统时间绕过有效期** | 回拨可绕过; 上限场景加 `--max-opens` 双保险 |
| **双分区的无盘符隐藏** | 整理性隐藏非安全边界: 磁盘管理/diskutil 能看到分区, 补个盘符即现身; mbr 制式在其他 Windows 机首插可能自动补盘符; 内容防护仍全靠加密链与序列号绑定 (见"双分区烧录"节) |

**给发件人的实证测试** (第 7 条就是上表"伪造序列号"的演示):

1. `view --drive E:` 本机预览正常;
2. (先开"显示隐藏的文件") 把 `.DATA` 里文件拷到桌面 → 打不开;
3. 整盘拷到另一只U盘运行查看器 → "仅限指定U盘使用";
4. 拷整个盘内容到 C: 运行 → 同上; 拔盘后**只拷阅览器目录**到 C: 运行 →
   "脱离U盘介质，加密书稿无法在该设备上打开查看" (**v2.4.4 起**; 旧版
   弹"该盘不是资料盘: 缺少 keyinfo.bin" — 旧产物见到旧文案先换部件);
   **盘插着时**再拷整个阅览器目录到电脑任意位置 (桌面/D: 等) 运行 →
   同一句谢幕 (**v2.4.8 起** 自身介质闸门; 旧版原盘在场时照常开门 —
   2026-09-29 用户实测曝光的 v2.4.4 边界, Mac 拷 .app 到桌面同试);
   再**只拷目录内的书名 exe** (`《欧阳竟无全集》《桂伯华全集》.exe`) 到
   桌面双击 → 同一句谢幕文案 (**v2.4.5 起**, 引导壳; 旧版弹英文
   "Failed to load Python DLL" — 见到英文先换部件);
   盘上**删掉隐藏的真主程序** `图书阅览器.exe` 后双击书名 exe →
   "阅览器文件不完整，请联系发件方重新烧录U盘" (**v2.4.6 起**, 与拷离
   谢幕可分辨; 旧版误报同一句"脱离U盘介质"误导排障);
5. OBS/微信/FSCapture 窗口截图与录屏 → 黑框 (**v2.3.4 起才真黑**: 2026-09-20
   真机实测曝光 v2.3.3 防截屏从未生效 —— 误设子 HWND 且失败被吞; 修复后
   探针实证 BitBlt/PrintWindow 均黑, 真机微信/FSCapture 复测待重烧后留档);
   PrtScr → 无内容; Mac 端: 微信截屏 / ⌘⇧4 / 录屏 → 同样须拍不到
   (**v2.4.11 起**; 2026-10-01 用户实测旧版被微信截屏穿透, 重建后
   逐项留档; 手机翻拍仍属边界, 见"防不住"表);
6. 改日期到过期后 → "资料已过期";
7. (知己知彼) 用 VolumeID 把克隆盘序列号改成原盘 → 能打开 ⇒ 这就是边界。
8. 资源管理器删 `图书阅览器` 目录 / Shift+Del / `cmd del /f` / PowerShell
   `Remove-Item -Force` → **全部须被拒** (2026-09-28 NTFS 防误删 ACL;
   2026-09-20 曾实测旧 exFAT+只读位形态删得掉 —— 只读位不是防删手段,
   那是换 NTFS deny ACL 的动因); Mac 端删 `.app` 同试留档 (NTFS 只读
   挂载下预期删不了)。
9. 无盘符卷访问 (v2.4.0 反转布局地基, 2026-09-22 探针): 非提权进程经
   `\\?\Volume{GUID}\` 列目录/读文件成功 (无盘符 NTFS 恢复卷实测);
   EFI 分区拒访属微软引导分区专项加固, exFAT 数据分区不在其列 ⇒
   分区2 在 Windows "藏得住"且阅览器"读得到"; F: 棒烧录后复测见
   双分区节步骤 2b。

## 支持的操作系统 (收件人电脑)

- **Windows: 10 及以上** (2026-09-22 Win7 真机实测定案)。阅览器内嵌
  Python 3.14 运行时, Python 3.9 起弃 Win7/8, 3.14 官方要求 Win10+;
  Win7 双击报 "丢失 api-ms-win-core-sysinfo/path-l1-*.dll" 或
  "Failed to load python314.dll" 即此因 —— **换 Win10+ 电脑是唯一解**,
  手工补 DLL 的偏方对 3.14 无效且不可靠, 不采用;
- **macOS: 下限待定案** (取决于构建机 Python 的最低支持; 构建机为
  Sequoia 15.7, 老系统 Mac 可能闪退, 待真机留档)。**芯片: 2026-10-02
  起双架构交付** —— Apple 芯片 (M 系列) 与 Intel 各一份 .app (Intel 版
  由 python.org universal2 Python + `arch -x86_64` venv 构建, 不依赖
  收件人装 Rosetta; 详见"Mac签名公证操作方案"), 收件人按
  关于本机→芯片 自选入口, Intel 最低系统版本受该构建 Python 版本上限
  约束待真机留档;
- 交付前先确认收件人电脑系统版本; 确有 Win7/8 收件人时, 要么换机,
  要么另立 Python 3.8 专用构建线 —— 源码现需 3.10+ (@dataclass slots),
  降级是一条独立工程线, 且 3.8 已 EOL + 旧版 cryptography/OpenSSL,
  **加密产品不建议**。

## U 盘要求与恢复

- **格式**: **NTFS (2026-09-28 起交付形态**, 双分区由工具自建, 防误删 ACL
  只在 NTFS 上成立; macOS 端原生只读挂载 —— 收件人只读查阅场景不受影响,
  .app 运行待真机核验); exFAT/FAT32 单分区也能烧 (无防误删 ACL, 记警告);
  FAT32 另有 4GiB 单文件上限。烧录时会检查并提示;
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
├─ ntfs_guard.py       NTFS 防误删 deny ACL: 施加/整树清障/回读校验 (2026-09-28)
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
