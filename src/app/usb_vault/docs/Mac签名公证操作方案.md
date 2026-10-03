# Mac 端 .app 签名+公证操作方案

> 目标: 《…》图书阅览器 Mac 版 .app（2026-10-02 双架构起两份: Apple 芯片版
> + Intel 芯片版, 落盘名（适配MAC-Apple芯片）/（适配MAC-Intel芯片））在
> **任何 Mac 上双击零提示直开** —— 不上应用商店,
> 走 Apple 官方的 Developer ID 签名 + 公证 (Notarization) 路线。
> 本文按阶段推进, 每阶段有"✅ 完成标准"; 卡住按文末故障速查表对号入座。
> 配套: README 1m⑤ (要点版); 工具支持已进 build_viewer.py (2026-09-24, 仍 v2.4.7)。

## 背景 (为什么必须这么做)

| 现象 | 根因 |
|---|---|
| 编译的 Mac 能开, 别的 Mac 弹"无法验证开发者"/"已损坏" | .app 只有 ad-hoc 临时签名, 换机后 Gatekeeper 要验正式身份 |
| macOS 15 上右键→打开也没用了 | Sequoia 取消了右键旁路, 只剩 系统设置→隐私与安全性→仍要打开 |
| 微信/网盘传过去必弹窗, U盘(exFAT)传不弹但双架构/版本不对仍打不开 | 隔离标记只跟"下载类通道"走; 架构/系统版本是另一层病 |

根治 = Developer ID 签名 + 公证 + 装订, 之后任何 Mac 任何传输渠道零提示。

## 阶段 0 · 同步最新代码到编译机 (每次都先做)

烧录机仓库 → 拷 **最新** `src/app/usb_vault/` 完整目录到 Mac 本机磁盘 (备用空棒摆渡, 勿用交付棒)。
2026-09-24 版起工具自动处理两个公证硬前提 (旧版没有, **旧版构建的包公证必拒**):

- `--sign` 传身份时自动加 `--options runtime` + `--timestamp`;
- 签名前自动写合法 `CFBundleIdentifier` (`com.usbvault.vault-viewer`) + 版本号;
- 签名前自动把 .app 内**符号链接拍平成纯文件树** (2026-09-25(b): 新版
  PyInstaller 布局含 54 处符号链接, `zip -r` 打包会跟随成副本、exFAT 又无
  链接语义 —— 不拍平, 中转后收件人 Mac 必弹「已损坏」);
- 拍平规则 (2026-09-29 六版, 六轮真机修正的收口): codesign 规定
  Frameworks 下只有 .framework 目录许带点 —— 这正是 PyInstaller 把真目录
  起名 `__dot__dylibs`、另留软链的原因, 也是"链接一律拷贝"必翻车的原因
  ((c): 带点目录链接拷成真目录当场拒签; (d): 不带点目录链接 (tcl8 类数据
  镜像) 拷贝时把 Resources 侧带点子目录搬进 Frameworks, 同样拒签;
  (e): .framework 内部链接 (Versions/Current、框架根别名) 拍成真目录 =
  非规范框架结构, codesign 报 "bundle format is ambiguous";
  (f): (e) 删光框架内链接后, 剩下无 Current 无别名的 Versions/<ver>/
  残架 codesign 仍报 "unrecognized @ …Python.framework" —— 版本化布局
  离不开链接, 而链接过不了中转, 唯一活路 = 扁平化;
  (g): 四版签名过了、**直跑**死于 `ModuleNotFoundError: No module named
  '_struct'` —— "目录链接全是纯观感镜像"论断漏了一类运行时寻址: 导入器
  按 sys.path 落盘探测扩展模块, (c)(d) 删链接把 lib-dynload 与 tcl/tk
  数据的寻址路径一并删断;
  (h): 五版能跑了、**公证 Invalid** —— log 全指向 Resources/PIL/.dylibs
  等 Resources 侧代码镜像, 它们被旧规则拍成了无主未签名拷贝):
  - **Frameworks 下目录链接一律删除** (拷贝必触 codesign 点名关 —— 带点
    目录链接本身带点, 数据镜像链接拷贝会把树内带点子目录搬进来);
    其中两类是运行时寻址必需, 删除后配套补偿 (见下两条);
  - **lib-dynload 扩展提升顶层** (g): PyInstaller 把标准库扩展落盘名改写
    成 `python3.14/lib-dynload/…` (building/utils.py:95-101), .app 里靠
    "`python3.14` → `python3__dot__14`" 点名替换链接寻址。struct 由
    bootloader 最先导入的 pyimod01_archive 触发、早于一切钩子, 无
    Python 层可救 —— 唯一修法 = 把 `.so` 提到 Frameworks 顶层 (sys.path
    首项), 导入器落盘探测自然命中; `python3.14` 链接两侧镜像都删不留
    死副本;
  - **tcl/tk 数据指回 Resources 真树** (g): `_tcl_data`/`_tk_data` 真身在
    Resources、Frameworks 侧镜像链接已删; 构建时自动生成 Mac 运行时钩子,
    按 .app 布局把 `TCL_LIBRARY`/`TK_LIBRARY` 指回 Resources 真树 (自带
    钩子 pyi_rth__tkinter 只认 MEIPASS 下目录, 目录不在时静默不设 →
    不补位 Tk 会死在 "Can't find a usable init.tcl");
  - **.framework 子树内链接 (含文件别名) 一律删除**; 名字以 .framework
    结尾的镜像链接 (Resources/Python.framework→../Frameworks/…) 同删
    (落成拷贝会在拷贝内部再造违规结构);
  - **Resources 下指向 Frameworks 的链接一律删除** (h, 2026-09-29 公证
    Invalid 定谳): 目录镜像 (Resources/PIL→../Frameworks/PIL) 与顶层
    文件镜像 (Resources/libwebp.6.dylib→…) 全算 —— PyInstaller 双树布局
    里二进制真树在 Frameworks、Resources 侧只放镜像链接, 拍成拷贝 =
    无主未签名 Mach-O (外层 codesign --deep 只签代码位置
    MacOS/Frameworks, Resources 下的散装二进制只封封条哈希不签名),
    公证逐条拒 "not signed with a valid Developer ID certificate";
    运行时 MEIPASS=Frameworks, 无人从 Resources 装载代码, 删除是纯减重;
  - **链接删净后 .framework 整体扁平化**: 二进制与 Resources 提到框架根,
    Versions/ 目录整体移除。框架规范第 4 条是条件式 —— "顶层**有**
    Versions 目录才禁他物站旁边", 经典扁平框架合法 (codesign 认)。运行时
    零影响 (PyInstaller 6.22.2 源码链实证): bootloader dlopen 的是
    Frameworks 顶层锚点 `Python` 真文件 (bindepend 为非顶层二进制造的
    顶层链接, 已拍平成真拷贝), 其余依赖全走 @rpath/<纯文件名> 解析到
    Frameworks 顶层 —— 框架内部布局无人在意;
  - 其余 (Frameworks 顶层文件链接 = @rpath 解析锚点、Resources 内部指向
    Resources 的数据链接) → 照旧拍平成真实拷贝 (文件名带点天然合法);
  - 拍平后自检不变量: Frameworks 下再有点名真目录 (除 .framework 及其
    内部)、或 Resources 下出现散装 .dylib/.so, 当场熔断报路径,
    不等 codesign/公证报天书。

⚠️ 别在 exFAT U盘上直接构建 (.app 内符号链接会被吞) —— 拷到 Mac 本机磁盘再操作。

⚠️ **构建前清场** (2026-09-25 真机事故教训: dist 新旧产物混装过一次, 且符号
链接经 zip/exFAT 中转必失守 —— 两条叠加时肉眼无法分辨, 只能靠烧录封条闸门
定谳; 每次从零构建最稳):

```bash
rm -rf dist build        # 每次正式构建前从零开始, 不许在旧 dist 上叠加
```

✅ 完成: Mac 上的 `viewer/build_viewer.py` 里能看到 `patch_mac_bundle_metadata`。

## 阶段 1 · 证书配对 (一次性)

.cer 文件里**只有公证书, 没有私钥**; 私钥是创建证书时在"当时那台机器"的钥匙串里生成的。
`find-identity` 数的是**证书+私钥成对**, 只有 .cer 永远是 0。

1. 双击 Downloads 里的 .cer → 添加到**登录**钥匙串;
2. 终端验证: `security find-identity -v -p codesigning`;
3. 出现 `1 valid identities found` → **抄下完整身份串** (形如
   `"Developer ID Application: 单位名 (TEAMID)"`, 后面要用), 跳阶段 2;
4. 仍是 0 → 按下表三分支:

| 分支 | 判定 | 处置 |
|---|---|---|
| A | 证书就是在这台 Mac 上创建的 | 钥匙串访问 → 登录钥匙串 → 证书分类 → 该证书下应挂着私钥; 挂着但仍是 0 → 证书被导进了别的钥匙串, 拖回"登录" |
| B | 证书在另一台机器上创建 | 去那台机器: 钥匙串访问 → 右键证书 → 导出 .p12 (设密码) → AirDrop/移动介质拷到本机 (⚠️ 私钥红线: 不走网盘/聊天工具) → 双击导入 |
| C | 私钥那台机器已不可得 | 本机: 钥匙串访问 → 证书助理 → 从证书颁发机构请求证书 (存到磁盘, 私钥即进本机) → developer.apple.com → Certificates → 吊销旧证 → + 新建 Developer ID Application → 上传 CSR → 下载新 .cer → 双击导入 |

✅ 完成: `1 valid identities found`。附注: Developer ID 证书有效期约 5 年, 到期前 Apple 会邮件提醒, 换发重走本阶段即可。

## 阶段 2 · 公证凭据 (一次性)

前置: `xcode-select --install` (codesign/notarytool/stapler 都来自 Xcode 命令行工具)。

**路线 1 · App 专用密码**: 无痕窗口开 account.apple.com → 登录**开发者账号那个 Apple ID**
(注意别登成个人日常号; 开发者账号强制双重认证, 正确账号一定有此入口) → 左侧栏
**登录与安全** → **App 专用密码** → 生成 (形如 `abcd-efgh-ijkl-mnop`, 只显示一次):

```bash
xcrun notarytool store-credentials usbvault-notary \
  --apple-id 开发者账号邮箱 --team-id TEAMID --password 刚生成的专用密码
```

**路线 2 · App Store Connect API 密钥** (网页上没有"App 专用密码"时用 —— 多为单位托管的
Apple ID; 单人账号也推荐, 更稳): appstoreconnect.apple.com → 用户和访问 → **集成** →
App Store Connect API → 生成 Admin 密钥。**记三样**: 发行者 ID (Issuer)、密钥 ID、
下载 .p8 (⚠️ **只能下载一次**)。然后:

```bash
xcrun notarytool store-credentials usbvault-notary \
  --key ~/Downloads/AuthKey_XXXXXX.p8 --key-id 密钥ID --issuer 发行者ID
```

✅ 完成: 命令提示凭据已存入钥匙串 (`usbvault-notary` 档案), 机器重启也在。

## 阶段 3 · 构建 + 正式签名 (每次构建)

在 usb_vault 目录, venv 激活状态:

```bash
python3 main.py build-viewer --sign "Developer ID Application: 单位名 (TEAMID)"
```

身份串与阶段 1 抄下的**一字不差**。构建后核对输出 **"内嵌 SEED 指纹" 与烧录机 burn 输出同值**
(不同 = Mac 上 seed.bin 不是烧录机那颗, 烧出来的盘必然解不开, 立即停)。

✅ 完成: 构建成功 + SEED 指纹一致 (产物 dist/vault_viewer.app)。

## 阶段 4 · 公证 + 装订 (每次构建)

```bash
ditto -c -k --keepParent dist/vault_viewer.app nv.zip   # 不能用 zip -r
xcrun notarytool submit nv.zip --keychain-profile usbvault-notary --wait
xcrun stapler staple dist/vault_viewer.app
```

- `--wait` 等到 `status: Accepted` 才算过; 显示 `Invalid` 就取日志逐条看原因:
  `xcrun notarytool log 那次提交的SubmissionID --keychain-profile usbvault-notary`;
- 装订 (staple) 让收件人**离线**也能验票, 必做不可省。

✅ 完成: Accepted + staple 无报错。

## 阶段 4b · Intel 版构建 + 公证 (双架构交付, 2026-10-02 起, 每次交付)

背景: 构建机是 Apple 芯片 Mac, 原生构建产物只有 arm64 —— Intel Mac 收件人
双击打不开。评审定案**双 .app 按芯片命名** (universal2 单包被否: 拍平/rpath/
scrypt 三道构建期不变量都锚定瘦魔数, fat 二进制会静默绕过全部把关; Intel-only
+Rosetta 被否: 收件人首装 Rosetta 要联网且渲染性能打折)。Intel 版 = 同一条
build-viewer 链在 `arch -x86_64` 解释器下跑一遍 (x86_64 瘦魔数本就在不变量
扫描名单里, 拍平/签名/公证/封条闸门全链零改动)。

**一次性准备** (构建机 Apple 芯片, 只需做一遍; 2026-10-03 修订):

```bash
softwareupdate --install-rosetta --agree-to-license   # Rosetta 2, 幂等已装则秒过
# 先看现有构建用的 Python 是不是已经 universal2 —— python.org 官方安装器
# 装出来的本来就是胖二进制, 是的话**无需再装任何东西**, 直接进下一步:
file $(readlink -f $(which python3))                   # 含 "universal2" = 直接用
# 不是 (如 brew arm64 单架构) 才从 python.org 装一份 —— 装到
# /Library/Frameworks/, 与 brew 版不同路径共存互不覆盖; 唯一副作用是安装器
# 会把自己加进 PATH, 新终端里裸敲 python3 可能变成 python.org 版 —— 脚本
# 一律用完整路径/venv (本文全部如此), 现有构建 venv 不受任何影响
#   安装步 (2026-10-03): python.org/downloads/macos 选与 Apple 版构建 venv
#   同版本号的 "macOS 64-bit universal2 installer" (.pkg) → 双击装或
#   sudo installer -pkg /tmp/python314.pkg -target / →
#   file …/Versions/3.14/bin/python3.14 须含 universal2 + 上面两条 arch 验证
#   各打 x86_64/arm64。⚠ 已有 python.org 3.14.x 就别再装不同 patch 号 ——
#   同 minor 共用 Versions/3.14/ 会原地换二进制, 连 Apple 版构建 venv 脚下
#   的解释器一起换, 已 universal2 直接用即可
arch -x86_64 /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m venv ~/venv-intel
arch -x86_64 ~/venv-intel/bin/python3 -m pip install cryptography pymupdf pillow pyinstaller
arch -x86_64 ~/venv-intel/bin/python3 -c "import platform; print(platform.machine())"
# ↑ 必须打 x86_64 —— 这是本段的地基自检, 不符停手
```

⚠️ **arch 前缀必须每次调用都带, venv 不会"记住"创建时的架构** (2026-10-03 修订,
首版"激活即 x86_64"是错的): venv 里的 python 指向的是 universal2 **胖二进制**,
普通 `source ~/venv-intel/bin/activate` 后裸跑 `python3`, 内核默认挑 **arm64**
切片 —— pip 会装 arm64 轮子、PyInstaller 会打出 arm64 包, 白跑一场。等价替代:
给终端开"用 Rosetta 打开"(访达→应用程序→实用工具→终端→显示简介→勾选"使用
Rosetta 打开"), 该窗口内一切进程天然 x86_64, 普通激活即可 —— 但多窗口时容易
记错自己在哪个窗口, **统一 arch 前缀更防呆**。Apple 版构建 venv 全程不受影响
(venv 各钉各的解释器, 互不干扰)。

**每次交付** (Apple 版走完阶段 3+4 后, 同一工作树; arch 前缀一个都不能省):

```bash
arch -x86_64 ~/venv-intel/bin/python3 main.py build-viewer --sign "Developer ID Application: 单位名 (TEAMID)"
# 构建输出开头应报 x86_64 架构; 同样核对"内嵌 SEED 指纹"与烧录机一致 (同一颗 seed.bin)
ditto -c -k --keepParent dist/vault_viewer.app nv-intel.zip
xcrun notarytool submit nv-intel.zip --keychain-profile usbvault-notary --wait
xcrun stapler staple dist/vault_viewer.app
mv dist/vault_viewer.app dist/vault_viewer_intel.app   # ⚠ 装订完再改名; 目录名不在
                                                        # 签名封条内, 改名安全
```

⚠️ 注意: Intel 构建会覆盖 dist/vault_viewer.app —— **先走完 Apple 版的阶段
3+4+5 再走本段**, 或两版各自"清场→构建→公证"串行; 全新空 dist 里最终留
`vault_viewer.app` (Apple) + `vault_viewer_intel.app` (Intel) 两份。

✅ 完成: Accepted + staple 无报错 + `file dist/vault_viewer_intel.app/Contents/MacOS/*`
显示 x86_64。阶段 5 的三步终验 (①selfcheck ②外置副本真棒在场输码翻 PDF
③防截屏) **在 Intel 真机上原样再走一遍** (用户有 Intel Mac, 2026-10-02 确认)。

## 阶段 5 · 终验

拍平策略动过文件树, 签名前后的运行能力在此一锤定音 —— 2026-09-26(f) 起
.framework 整体扁平化, (g) 起 lib-dynload `.so` 提升顶层、tcl/tk 数据改走
Resources 真树。注: 2026-09-29(i) 起构建期新增两道不变量自动把关"开窗验不到
的深链" —— 全包依赖解析自包含 + _hashlib/libcrypto scrypt 就位 (解锁派生/
PDF 渲染的静默死因), 构建输出见到这两关没熔断即深链有守。

**2026-09-30 实测教训: v2.4.8 介质闸门起, 固定盘上的 dist 直跑永远只弹
「脱离U盘介质」谢幕框** —— 闸门只看阅览器**自身**所在介质 (可移动与否),
与真棒插不插无关 (README v2.4.8 段), 旧流程"dist 直跑能开窗即过"自 (e)
起已不可能。终验分三步:

① dist 自检 (深链健康度, 不弹 GUI):

```bash
dist/vault_viewer.app/Contents/MacOS/vault_viewer --selfcheck
```

固定盘上**「自身介质」行按设计 ✗** (挂载点=不在 /Volumes 下) —— 这只说明
闸门在岗, 不算产物故障; **其余行须全 ✔**, 真棒在场时「数据卷」行应列出
`/Volumes/.资料存储` 及序列号通道状态。

② 解锁链真验 (外置副本 + 真棒在场): 旧"开窗即过"验不到解锁/PDF 深链,
v2.4.8 起又只能在可移动介质上启动 —— 把 dist 产物拷一份到**可写外置卷**
(备用 exFAT/FAT32 U 盘; 没有备用棒可用磁盘映像代替, 见下), **真交付棒
同时插着** (加密链在其分区2), 从外置副本启动:

```bash
cp -R dist/vault_viewer.app /Volumes/<备用棒卷名>/
/Volumes/<备用棒卷名>/vault_viewer.app/Contents/MacOS/vault_viewer
```

预期: 弹解锁码 → 输码 (中途弹一次管理员授权框读序列号 = 设计内兜底通道)
→ 开门 → **翻一本 PDF 到尾** (七版修的 rpath 深链在此一锤定音)。收工
卸载外置卷后继续。

③ 防截屏真验 (v2.4.11 起必做, 2026-10-01 用户微信截屏实锤旧版从未生效):
开门状态下用**微信截屏**框选阅览器窗口 → 须拍不到窗口内容; 再试系统
⌘⇧4 截阅览器、录屏/QuickTime 画面采集 → 同样拍不到 (NSWindowSharingNone,
1Password 同款机制); 若 dist 自检的「防截屏」行 ✗ 或微信仍能拍到内容,
按 v2.4.11 README 段排查后重建, 不得放行。手机翻拍物理屏幕属方案边界
(与 Windows 侧一致), 不在本步范围。

没有备用棒时的磁盘映像替代 (disk image 非内置硬件, 同样过介质闸门;
万一系统判成 internal 则必须换实体备用棒):

```bash
hdiutil create -size 2g -fs "ExFAT" -volname USBVAULT_TEST /tmp/usbvault_test.dmg
hdiutil attach /tmp/usbvault_test.dmg
cp -R dist/vault_viewer.app /Volumes/USBVAULT_TEST/
/Volumes/USBVAULT_TEST/vault_viewer.app/Contents/MacOS/vault_viewer
```

```bash
codesign --verify --deep --strict --verbose=2 dist/vault_viewer.app   # satisfied on all tiers
spctl -a -vv dist/vault_viewer.app                                    # accepted source=Notarized Developer ID
find dist/vault_viewer.app -type l | wc -l                            # 必须为 0 (符号链接经
                                                                      # zip/exFAT 必失守; 最新代码
                                                                      # 构建时已自动拍平, 非 0 =
                                                                      # Mac 代码没同步到最新)
```

`source=Notarized Developer ID` = 任何 Mac 双击零弹窗的通行证。有条件再抽一台
别的 Mac 实测双击。

✅ 完成: 两条命令的输出如上。

## 阶段 6 · 回烧录机烧盘 (每次交付)

```bash
cd dist && zip -r vault_viewer.app.zip vault_viewer.app          # 中转传输, 照旧
zip -r vault_viewer_intel.app.zip vault_viewer_intel.app          # Intel 版同晒一份
```

拷回烧录机, **解压到全新空目录** (勿并入旧 dist/ —— 2026-09-25 事故就是新旧两份
产物混在同目录), 再整体替换 `dist/` (2026-10-02 双架构起 dist 里须有
`vault_viewer.app` **和** `vault_viewer_intel.app` 两份, 烧录自动各认各的; Intel
版放在别处时用 `--viewer-mac-intel <路径>` 指定), 然后 (管理员 PowerShell):

```powershell
python -m src.app.usb_vault.main burn --source <书稿目录> --drive X: --dual-entry --password --title 图书资料
```

烧录把 .app 拷成中文名目录**不影响签名/票据** (签名不覆盖 bundle 目录名, 票据按文件哈希匹配)。
烧录工具自带**签名封条自检** (2026-09-25 起): 烧前把 .app 文件树与
`_CodeSignature/CodeResources` 逐文件比对 (含符号链接位与嵌套签名条目识别),
对不上 (文件被改/缺/多、链接被中转拍平、混入另一次构建) 当场熔断拒烧 ——
坏产物到不了收件人手里。
烧完按 README 真机验证手册走一遍 (含 v2.4.7 Mac 点前缀核验)。

✅ 完成: 烧录成功 + 双平台真机验证通过 + 收件人 Mac 双击零提示。

## 故障速查

| 症状 | 对策 |
|---|---|
| 双击阅览器弹「脱离U盘介质，加密书稿无法在该设备上打开查看」, 但U盘明明插在机器上 (2026-09-30 构建机首例) | 先看双击的是**哪一份**阅览器。v2.4.8 介质闸门判据 = 阅览器**自身**所在介质是否可移动, 与U盘在不在场无关 —— dist 目录/桌面/内置盘上的副本, 真棒插着也照样谢幕, **这是按设计** (手册 v1.13「拷到本机任何位置, 即使U盘插在同机也拒」; 旧版直跑能开窗是因为那时还没有这道闸门)。构建侧验证改走阶段 5 的「外置副本 + 真棒在场」流程。若双击的确实是**U盘卷内**的阅览器仍弹此框: 在棒上跑 `--selfcheck` —— 「自身介质」✗ = diskutil 判据问题 (把整段输出取回, 死因都在里面); 「数据卷未找到」= 分区2 没挂上 (终端 `ls /Volumes/` 看有无 `.资料存储`; NTFS 分区2 在该 Mac 不挂载则按既定预案回退 exFAT 重新交付)。**此形态真机两连发已定谳 (v2.4.10 收口)**: 第一轮 (09-30) 以为卷级 plist 缺 `Internal` 键, v2.4.9 加了三级级联仍谢幕; 第二轮 selfcheck 死因带出三条全是 `diskutil 退出码 1: Usage: … [-plist]` —— 该 macOS 26 的 diskutil **拒收双横线 `--plist` 写法** (用法自述即单横线), 级联三通道共用它 = 公共单点故障。**v2.4.10 已修**: 探测改 `(-plist, --plist)` 双写法且全模块收敛单一探测点 (介质判据 / UUID 通道 / 授权框设备定位三处共用); **v2.4.10 之前构建的棒遇此框不必排障直接重建**; 换新 macOS 机型后可先跑 `diskutil info -plist /Volumes/盘名 | head -3` 确认该机认哪种写法 |
| 直跑报 `[PYI-…:ERROR] Module object for struct is NULL` / `ModuleNotFoundError: No module named '_struct'`; 或开窗阶段报 `Can't find a usable init.tcl` | 拍平代码是四版及更早: "目录链接一律删"把 lib-dynload 扩展的寻址路径 (`python3.14` 点名替换链接) 和 tcl/tk 数据镜像一并删断 —— 签名能过, 跑不起来。对策 = 同步最新代码 (自证标记 **2026-09-29 拍平修正七版**: `.so` 自动提升 Frameworks 顶层 + 运行时钩子把 TCL_LIBRARY/TK_LIBRARY 指回 Resources 真树), 回阶段 0 清场重建 |
| 直跑**开窗正常**, 输解锁码后崩 `AttributeError: module 'hashlib' has no attribute 'scrypt'`; 或解锁后翻书/渲染 PDF 崩 `dyld: Library not loaded: @rpath/libmupdf.dylib…` (libtiff/libwebp 同类) | 六版及更早 (2026-09-29 真机定谳): 拍平的提升搬移 (lib-dynload `.so` 上顶层、PIL/.dylibs 与 pymupdf 的顶层镜像) 没跟着改字节里的 rpath 算术 —— PyInstaller 契约是"真身按自然深度落盘 + rpath 按自身深度指向 Frameworks 顶层", 副本在顶层再上一层解析到 `Contents/` 依赖全灭。`_hashlib` 死了 **hashlib 还会静默降级** (sha256 走内置 _sha2 照常、唯独 scrypt 消失), 开窗/弹码框全不受影响, 极具迷惑性。对策 = 同步最新代码 (自证标记 **2026-09-29 拍平修正七版**: 构建期 rpath 自动归一化回顶层 + 依赖解析自包含/scrypt 双不变量把关, 这类"开窗正常深链全死"构建当场熔断), 回阶段 0 清场重建 |
| `codesign: bundle format unrecognized` / 构建报「Frameworks 下存在名字带点的真目录 (…opt0.4 之类)」 | 拍平代码是旧版: (c) 前会把带点目录链接 (PIL/.dylibs) 拷成真目录, (d) 前还会把数据镜像目录链接 (tcl8 类) 的带点子目录搬进 Frameworks。对策 = 同步最新代码, 回阶段 0 清场重建。注: 输出里的 `replacing existing signature` 是信息性行不是错误 |
| `codesign: bundle format unrecognized … In subcomponent: …/Frameworks/Python.framework` | 拍平代码是 (e) 及更早: 删光了框架内链接 (Versions/Current、根别名) 却没扁平化, 剩下无 Current 无别名的版本化残架 codesign 不认 (PyInstaller 源码注释自认 Current 对 codesign 必要)。对策 = 同步最新代码 (自证标记 **2026-09-29 拍平修正七版**, 构建时自动扁平化 .framework), 回阶段 0 清场重建 |
| `codesign: bundle format is ambiguous (could be app or framework)` | 拍平代码是 (d) 及更早: 曾把 .framework 子树内链接 (Versions/Current、框架根别名) 拍成真目录 —— 框架规范要求 Versions/ 旁边只许站符号链接, 拍平 = 非规范结构, codesign 分不清该当 app 还是 framework。对策 = 同步最新代码 (自证标记 **2026-09-29 拍平修正七版**), 回阶段 0 清场重建 |
| `find-identity` → 0 valid identities | 阶段 1 三分支 (A 本机配对 / B 别机导 .p12 / C 吊销重发) |
| 网页没有"App 专用密码" | 先确认登的是开发者账号; 还没有 → 单位托管 ID, 走阶段 2 路线 2 (API 密钥) |
| `xcrun: notarytool 未找到` | `xcode-select --install` 后重开终端 |
| 公证 Invalid | 看 log, 按报错路径对号: ① 缺 `--options runtime` / identifier 非法 —— 旧版 usb_vault 构建的包; ② log 全指向 `Contents/Resources/…` 且报 "The binary is not signed with a valid Developer ID certificate" —— 五版及更早把 Resources 侧代码镜像 (PIL 等) 拍成了无主未签名拷贝, codesign --deep 不签 Resources 下的散装二进制 (2026-09-29 真机定谳)。两条同策: 回阶段 0 同步最新代码 (自证标记 **2026-09-29 拍平修正七版**, 镜像改删不拷) 清场重建 |
| SEED 指纹与烧录机不一致 | 立即停, 别烧; Mac 目录里的 seed.bin 换成烧录机的 (shasum -a 256 核对) |
| 公证过了但有的 Mac 仍打不开 | 先分清两类: ① **芯片不匹配** (2026-10-02 双架构起最常见的"打不开"): Intel Mac 双击了 Apple 芯片版入口 (或反之) —— 看 关于本机→芯片, 换另一个 Mac 入口即好 (收件人手册 v1.14 第三节第 2 步); Intel 收件人确实存在而盘上没有 Intel 入口 = 该盘是双架构交付 (2026-10-02) 之前烧的, 重烧。② 系统版本过旧: 构建机 `uname -m`, 产物 `file dist/vault_viewer.app/Contents/MacOS/*` (Apple 版应 arm64 / Intel 版应 x86_64), 老系统 Mac 可能闪退待真机留档。universal2 单包方案已评审否决 (fat 二进制静默绕过三道构建期不变量), 不采用 |
| 收件人 Mac 弹「已损坏，无法打开」 | .app 与签名封条不符。头号根因 (2026-09-25 定谳): 新版 PyInstaller 布局含大量符号链接, `zip -r` 打包跟随成副本 + exFAT 无链接语义, 中转后封条必失守 —— 同步最新代码 (构建时自动拍平链接) 后按"阶段 0 清场 → 阶段 3 重建 → 阶段 4 公证装订 → 阶段 6 重烧"整链重走 |
| **微信截屏 / ⌘⇧4 / 录屏能拍到阅览器内容** (2026-10-01 收件 Mac 实锤) | **v2.4.11 之前构建的棒一律视为无防截屏** —— 旧版只设 mainWindow 一次 + 1.6s 重试预算, 失败静默 (Windows v2.3.3 同款事故换平台复发), 不必排障。对策 = 同步最新代码整链重建 (v2.4.10 + v2.4.11 可一次覆盖), 终验加跑阶段 5 第 ③ 步防截屏真验; 重建后棒上 `--selfcheck` 应见「防截屏」✔ 行 |
| 烧录报「与签名封条不符」 | 同上。报「符号链接失守」= 构建用的还是旧代码 (新版自动拍平), 先同步代码再整链重走; 传回烧录机解压到全新空目录。**2026-09-29(c) 定谳**: 旧版闸门不认嵌套代码包目录条目 (Python.framework 报"缺失"+包内文件报"多余") 与非 Mach-O 的 cdhash 数据条目 (base_library.zip/seed.bin 报"无嵌入签名魔数") —— 这三类是**误报**, 产物本身是好的, 同步最新烧录机代码后原命令重烧即可, 不必回 Mac 重建 |
| .p8 下载按钮点完找不到了 | .p8 只能下载**一次**, 弄丢只能吊销该 API 密钥重新生成 |

## 边界与红线 (向单位报备口径)

- 公证会把 .app 上传 Apple 扫描 —— 该二进制本就要发给每位收件人, 信任边界不变;
  不触"真 SEED 不上 VirusTotal"红线 (那是公开样本库, Apple 只扫描不留存)。
- Developer ID 证书私钥、.p8 API 密钥文件**离线保管, 不进 git/网盘/聊天工具**。
- 每次改代码重新构建, 阶段 3–6 必须整段重走 (签名和公证绑定具体二进制)。
- 首个公证产物烧盘后: 收件人手册第三节第 3 步改为"双击即开", 并顺手补 macOS 15
  "仍要打开"流程说明 (旧 ad-hoc 盘的历史 FAQ 保留) —— 手册 v1.11, PDF 攒版重导。
