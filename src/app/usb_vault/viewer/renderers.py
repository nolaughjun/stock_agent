"""内容渲染器: 文本/图片/PDF 内置渲染, 二进制仅元数据。

防泄密原则: 内容只进内存、只上屏幕 —— 禁选中复制、无另存、无拖出、
不落盘、不调用外部程序。
"""

from __future__ import annotations

import io
import math
import queue
import sys
import threading
import time
import tkinter as tk
import unicodedata
from collections import deque
from collections.abc import Callable
from tkinter import ttk
from typing import Any, NamedTuple

from src.app.usb_vault import config
from src.app.usb_vault.manifest import FileEntry
from src.app.usb_vault.viewer import loadtimer

# 每种 media 的渲染函数名 (纯映射, 可单测)
RENDERER_BY_MEDIA: dict[str, str] = {
    "text": "text",
    "image": "image",
    "pdf": "pdf",
    "binary": "binary",
}


def renderer_kind(media: str) -> str:
    """media -> 渲染类型; 未知 media 一律按 binary (只显示元数据)。"""
    return RENDERER_BY_MEDIA.get(media, "binary")


def truncate_text(data: bytes, cap: int = config.TEXT_CAP) -> tuple[str, bool]:
    """解码并截断文本, 返回 (文本, 是否被截断)。"""
    truncated = len(data) > cap
    text = data[:cap].decode("utf-8", errors="replace")
    return text, truncated


def binary_meta_text(entry: FileEntry) -> str:
    """binary 类型的展示文案 (不打开内容)。"""
    origin = f" (源格式 {entry.converted_from})" if entry.converted_from else ""
    return (
        f"文件: {entry.rel_path}\n"
        f"大小: {entry.size:,} 字节 (密文 {entry.enc_size:,} 字节)\n"
        f"类型: 此类文件{origin}不支持在查看器中打开,\n"
        "仅提供基本信息 (为防止内容外泄, 不调用外部程序)。"
    )


# ---------------------------------------------------------------------------
# Tk 渲染 (不进单测; 逻辑部分已在上面拆出)
# ---------------------------------------------------------------------------

def _break(_event: tk.Event) -> str:
    return "break"


# 吞复制事件序列: app.py 全局 bind_all 与本模块逐控件 bind 共用。
# 虚拟事件必须双尖括号 —— 写成 "<Copy>" 在 Windows Tk 是非法 keysym,
# bind 直接 TclError (2026-09-14 打包冒烟: 启动门禁连错误对话框都被炸掉)。
COPY_SWALLOW_SEQS = ("<Control-c>", "<Control-C>", "<Control-Insert>", "<<Copy>>",
                     "<Button-3>", "<Button-2>", "<B2-Motion>")
COPY_SWALLOW_SEQS_MAC = ("<Command-c>", "<Command-C>", "<Command-x>", "<Command-X>")


def _no_clipboard(widget: tk.Widget) -> None:
    """掐断一切复制路径: 无选中导出 + 吞 Ctrl+C/Insert/右键。"""
    for seq in COPY_SWALLOW_SEQS:
        try:
            widget.bind(seq, _break)
        except tk.TclError:  # 平台 keysym 差异只降级该序列, 不让渲染崩
            continue
    try:
        widget.bind_all("<Control-c>", _break, add="+")
        widget.bind_all("<Control-Insert>", _break, add="+")
    except tk.TclError:
        pass


def render_text(parent: tk.Widget, text: str, *, truncated: bool = False) -> tk.Widget:
    """只读文本视图: disabled 态不可选中, 配合剪贴板封堵。"""
    frame = tk.Frame(parent)
    txt = tk.Text(frame, wrap="none", exportselection=False, state="disabled",
                  relief="flat", background="#fafafa", padx=12, pady=8)
    ys = tk.Scrollbar(frame, orient="vertical", command=txt.yview)
    xs = tk.Scrollbar(frame, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    txt.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    txt.configure(state="normal")
    txt.insert("1.0", text)
    if truncated:
        txt.insert("end", f"\n\n—— 内容超过 {config.TEXT_CAP // 1024} KiB, 仅显示前部分 ——")
    txt.configure(state="disabled")
    _no_clipboard(txt)
    return frame


def render_binary_meta(parent: tk.Widget, entry: FileEntry) -> tk.Widget:
    s = ui_scale(parent)
    label = tk.Label(parent, text=binary_meta_text(entry), justify="left",
                     anchor="nw", padx=16, pady=12,
                     wraplength=round(560 * s))
    return label


def render_image(parent: tk.Widget, data: bytes) -> tk.Widget:
    """兼容入口: 图片查看现用 ZoomableImage (2026-09-18 起支持连续缩放)。"""
    return ZoomableImage(parent, data)


# ---------------------------------------------------------------------------
# PDF 翻页纯逻辑 (拆出 PdfPager 才测得了边界语义)
# ---------------------------------------------------------------------------

def pdf_page_step(page_no: int, page_count: int, delta: int) -> tuple[int, bool]:
    """受限翻页 (0-based): 越界不动, 返回 (页号, 是否移动)。

    2026-09-17 真机反馈: 到达首/末页后按钮静默无响应、无任何提示 ——
    调用方据"是否移动"禁用按钮并在页码文案里标注边界状态。
    """
    new = page_no + delta
    if 0 <= new < page_count:
        return new, True
    return page_no, False


def pdf_page_jump(text: str, page_count: int) -> int | None:
    """页码跳转解析 (收件人输入 → 0-based 页号): 不合法返回 None。

    收件人输的是页码标注 (第 1 页起)。中文输入法常打出全角数字/空白
    (NFKC 归一化吸收); 越界**不夹不猜**: 输 999 想跳末页与手误无法区分,
    静默夹到末页 = 看似成功的错误页 —— 由调用方给"范围 1~N"提示。
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    try:
        page = int(normalized)
    except ValueError:
        return None
    if not 1 <= page <= page_count:
        return None
    return page - 1


# ---------------------------------------------------------------------------
# 高分屏 DPI 缩放 (v2.4.3: Surface 类 150%~200% 缩放根治) + 缩略图纯逻辑
# ---------------------------------------------------------------------------

def dpi_scale(dpi: float, *, win32: bool) -> float:
    """系统 DPI -> UI 像素缩放系数, 夹 [1.0, 3.0]。

    进程按 Per-Monitor V2 声明 DPI 感知后, Windows 不再位图拉伸窗口:
    Tk **字体按点数随 DPI 放大**, 而列宽/sash/滑杆长度是裸像素 —— 200%
    下字体两倍大、列宽却纹丝不动, 左栏只装得下四个汉字 (2026-09-23
    Surface 真机反馈"列表显示不完全")。修法 = 所有裸像素 × 本系数,
    系数取自与字体同一 DPI 源 (winfo_fpixels("1i")), 两者永远同步。
    基线: Windows 96 (100%), macOS 72 (Aqua 逻辑点, retina 由系统吸收)。
    下限 1.0 = 无论如何不缩小; 上限 3.0 = 异常值 (如误报 480) 不放大到离谱。
    """
    base = 96.0 if win32 else 72.0
    return min(3.0, max(1.0, dpi / base))


def ui_scale(widget: tk.Misc) -> float:
    """dpi_scale 的部件侧入口 (Tk 根的 DPI 即进程所见 DPI)。

    参数类型用 Misc 而非 Widget: typeshed 中 Tk 根是 Misc+Wm, 不是 Widget,
    而 winfo_fpixels 定义在 Misc 层 (Widget/Tk 都有)。
    """
    return dpi_scale(float(widget.winfo_fpixels("1i")),
                     win32=(sys.platform == "win32"))


def nav_name_width(text_px: int, scale: float) -> int:
    """左栏文件名列初宽: 最长文件名实测宽 + 余量, 夹 [128, 320]×scale。

    下限保住 v2.3.3 定稿的紧凑观感, 上限防超长文件名把正文挤没 —— 超长
    名靠用户拖 sash/按钮展开 (不再赌一个"够宽"的定值; 128px 定值在
    200% 缩放下只装 4 个汉字, 正是本次真机反馈的根因)。
    """
    lower = round(128 * scale)
    upper = round(320 * scale)
    return min(upper, max(lower, text_px + round(24 * scale)))


# ---------------------------------------------------------------------------
# 页面缩略图纯逻辑 (v2.4.2: 与 PdfThumbPanel 的 UI 解耦, 可单测)
# ---------------------------------------------------------------------------

def thumb_dpi(page_w_pt: int,
              target_w: int = config.THUMB_WIDTH_PX) -> int:
    """缩略图渲染 DPI: 页宽 (PDF 点) -> 输出宽 ≈ target_w 的整数 DPI。

    推导: 输出宽 = 页宽/72 × dpi, 令其 ≈ 目标宽 ⇒ dpi ≈ 72×目标宽/页宽
    (与 PDF_BASE_DPI 无关 —— 那是缩放基线, 这里是固定小图)。页越宽 DPI
    越低、输出量级恒定; 必须返回 int (SWIG float TypeError 红线, 同
    dpi_for_pct 的 v2.3.1 事故)。target_w 由面板按 ui_scale 传入
    (v2.4.3 高分屏下面板等比放大)。
    """
    return max(1, round(72 * target_w / max(1, page_w_pt)))


def thumb_cache_cap(base: int, scale: float) -> int:
    """缩略图缓存上限随 ui_scale 反压缩: 位图面积 ∝ scale², 上限 ÷scale²。

    200% 缩放下单张位图 4 倍像素, 上限不压 => 内存翻 4 倍; 压回 ≈ 恒定
    (~40-60MB)。下限 120 张: 保证一屏缩略图 + 预取带不被反复淘汰。
    """
    return max(120, round(base / (scale * scale)))


def thumb_fit(w: int, h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """等比塞进槽位 box: 返回 (宽, 高), 巨宽/巨长页都不溢出 (最少 1px)。"""
    if w <= 0 or h <= 0 or box_w <= 0 or box_h <= 0:
        return (1, 1)
    scale = min(box_w / w, box_h / h)
    return (max(1, round(w * scale)), max(1, round(h * scale)))


def thumb_visible_range(y0: float, y1: float, slot_h: int, gap: int,
                        count: int) -> tuple[int, int]:
    """与纵向区间 [y0, y1) 相交的槽位号闭区间; 空/无效返回 (0, -1)。

    槽位 i 占 [i*(slot_h+gap), i*(slot_h+gap)+slot_h)。懒加载的取活依据:
    只渲可视区 (+预取), 大部头几百页不整本排队。
    """
    if count <= 0 or slot_h <= 0 or y1 <= y0:
        return (0, -1)
    step = slot_h + gap
    first = max(0, math.floor((y0 - slot_h) / step) + 1)
    last = min(count - 1, math.ceil(y1 / step) - 1)
    return (first, last) if last >= first else (0, -1)


def thumb_request_range(first: int, last: int, count: int,
                        margin: int) -> tuple[int, int]:
    """可视区 ± margin 的取活范围 (闭区间, 夹进 [0, count-1])。

    (0, -1) 入参 = 布局未就绪/无可视页, 退化为首页邻域 —— 面板初建时先有
    第一屏, <Configure> 后再按真实视口补齐。
    """
    if count <= 0:
        return (0, -1)
    lo = max(0, first - margin)
    hi = min(count - 1, last + margin)
    return (lo, hi) if hi >= lo else (0, -1)


def thumb_evict_keys(cached: list[int], vis_first: int, vis_last: int,
                     keep_max: int) -> list[int]:
    """缓存超限时应淘汰的页号 (按"离可视区最远"优先, 同距小页号优先)。

    可视区页**永不淘汰** —— 淘汰显示中的缩略图 = 画面凭空消失。返回长度
    = len(cached) - keep_max (可淘汰页不足时全给: 缓存临时超限好过闪屏)。
    """
    if len(cached) <= keep_max:
        return []
    protected = frozenset(range(vis_first, vis_last + 1))

    def dist(page: int) -> int:
        if page < vis_first:
            return vis_first - page
        if page > vis_last:
            return page - vis_last
        return 0

    droppable = sorted((p for p in cached if p not in protected),
                       key=lambda p: (-dist(p), p))
    return droppable[: len(cached) - keep_max]


# ---------------------------------------------------------------------------
# 连续缩放纯逻辑 (2026-09-18 收件人反馈: 三档"清晰度"循环 → 任意比例自选)
# ---------------------------------------------------------------------------

def zoom_clamp(pct: int) -> int:
    """缩放百分比夹进 [ZOOM_MIN_PCT, ZOOM_MAX_PCT]。"""
    return max(config.ZOOM_MIN_PCT, min(config.ZOOM_MAX_PCT, pct))


def zoom_step(pct: int, delta: int) -> tuple[int, bool]:
    """受限缩放步进: 返回 (新百分比, 是否移动), 与 pdf_page_step 同型。

    到边界后步进静默不动, 由调用方据"是否移动"决定要不要重渲染。
    """
    new = zoom_clamp(pct + delta)
    return new, new != pct


def dpi_for_pct(pct: int) -> int:
    """PDF 渲染 DPI: 100% = PDF_BASE_DPI (旧首档 96, 观感习惯连续)。

    必须返回 int: PyMuPDF 的 dpi 参数经 SWIG fz_pixmap_xres_set 只收整数,
    传 float 直接 TypeError —— v2.3.0 曾因此在 after 回调里静默炸成空白页。
    """
    return round(config.PDF_BASE_DPI * pct / 100)


def fit_pixels(w: int, h: int, pct: int) -> int:
    """像素预算内的有效缩放: 目标 w*h*pct² 超 RENDER_MAX_PIXELS 则压回。

    巨页 400% 放大 (或巨图) 会直接打爆内存, 这里按面积比例折算出可用
    百分比 (向下取整到 1%), 下限不低于 ZOOM_MIN_PCT —— 宁可少放大,
    不把收件人电脑卡死。
    """
    budget = config.RENDER_MAX_PIXELS
    if w * h * pct * pct <= budget * 10_000:  # pct² / 100² 化整数避浮点误差
        return pct
    capped = int((budget / max(1, w * h)) ** 0.5 * 100)
    return max(config.ZOOM_MIN_PCT, min(pct, capped))


def pdf_page_label(page_no: int, page_count: int) -> str:
    """页码提示文案 (0-based 入参): 首/末页显式标注, 单页文档两者都标。

    2026-09-17 用户反馈: DPI 档位已由"清晰度 N"按钮显示, 页码栏不再重复
    (原来"第 2/70 页 | 200 DPI"与按钮"清晰度 200"两处重叠)。
    """
    marks = []
    if page_no == 0:
        marks.append("首页")
    if page_no == page_count - 1:
        marks.append("最后一页")
    mark = f" ({'·'.join(marks)})" if marks else ""
    return f"第 {page_no + 1}/{page_count} 页{mark}"


# ---------------------------------------------------------------------------
# 连续缩放共享部件 (PDF 与图片同一套交互; 逻辑见上方纯函数区)
# ---------------------------------------------------------------------------

def _scrolled_canvas(parent: tk.Widget) -> tk.Canvas:
    """深底画布 + 双向滚动条: 大图只在画布内平移, 不撑破窗口布局。"""
    side = tk.Frame(parent)
    side.pack(fill="both", expand=True)
    canvas = tk.Canvas(side, background="#404040", highlightthickness=0)
    ys = ttk.Scrollbar(side, orient="vertical", command=canvas.yview)
    xs = ttk.Scrollbar(side, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    side.rowconfigure(0, weight=1)
    side.columnconfigure(0, weight=1)
    return canvas


def _bind_wheel(canvas: tk.Canvas) -> None:
    """滚轮平移大图 (Windows/macOS Tk8.6 <MouseWheel>, X11 <Button-4/5>)。

    只绑画布自身, 不碰全局; 平台不识别的序列只跳过 —— 降级好过崩启动。
    """
    def on_wheel(event: tk.Event) -> str:
        if getattr(event, "delta", 0):
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def scroll_up(_e: tk.Event) -> str:
        canvas.yview_scroll(-1, "units")
        return "break"

    def scroll_down(_e: tk.Event) -> str:
        canvas.yview_scroll(1, "units")
        return "break"

    for seq, fn in (("<MouseWheel>", on_wheel), ("<Button-4>", scroll_up),
                    ("<Button-5>", scroll_down)):
        try:
            canvas.bind(seq, fn)
        except tk.TclError:
            continue
    canvas.bind("<Button-1>", lambda _e: canvas.focus_set())


def _bind_ctrl_zoom(canvas: tk.Canvas,
                    handler: Callable[[int, int, int], None]) -> None:
    """Ctrl+滚轮 = 缩放 (handler(±步进, 光标x, 光标y), 锚点在光标处)。"""
    def on_wheel(event: tk.Event) -> str:
        if getattr(event, "delta", 0):
            handler(config.ZOOM_STEP_PCT if event.delta > 0
                    else -config.ZOOM_STEP_PCT, event.x, event.y)
        return "break"

    def up(e: tk.Event) -> str:
        handler(config.ZOOM_STEP_PCT, e.x, e.y)
        return "break"

    def down(e: tk.Event) -> str:
        handler(-config.ZOOM_STEP_PCT, e.x, e.y)
        return "break"

    for seq, fn in (("<Control-MouseWheel>", on_wheel),
                    ("<Control-Button-4>", up), ("<Control-Button-5>", down)):
        try:
            canvas.bind(seq, fn)
        except tk.TclError:
            continue


def zoom_anchor(view_frac: float, cursor_frac: float, ratio: float) -> float:
    """缩放后保持"光标处的内容"仍在光标下的新视口偏移 (0..1 clamp)。

    推导: 内容宽 W1→W2=ratio*W1, 视口像素偏移 o1→o2=ratio*o1+(ratio-1)*X1
    (光标局部坐标 X1 处的内容点缩放后仍落在 X1)。除以 W2 化为分数:
    view2 = (view1 + cursor_frac*(ratio-1)) / ratio。
    """
    new = (view_frac + cursor_frac * (ratio - 1.0)) / ratio
    return min(1.0, max(0.0, new))


def _render_error(canvas: tk.Canvas, exc: BaseException) -> None:
    """渲染失败显形: 把原图写上画布。事件/after 回调里的异常 tkinter 只打
    stderr, --windowed 产物 stderr=None 直接蒸发 => 收件人看到零症状空白
    (2026-09-18 v2.3.0 PDF 空白事故)。宁可见不可静默。"""
    try:
        canvas.delete("all")
        canvas.create_text(20, 20, anchor="nw", fill="#ff6666", width=600,
                           text=f"渲染失败: {exc}")
    except tk.TclError:
        pass


def _cancel_pending(widget: tk.Widget, after_id: str | None) -> None:
    """取消挂起的防抖渲染提交。"""
    if after_id is not None:
        try:
            widget.after_cancel(after_id)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 后台渲染调度 (2026-09-19 大书"未响应"根治: 渲染搬离 UI 线程)
# ---------------------------------------------------------------------------

_RENDER_DEBOUNCE_MS = 120  # 滑杆/滚轮触发后的渲染防抖窗口
_POLL_MS = 30  # 主线程取回渲染结果的轮询周期

_Job = tuple[int, Callable[[], object], Callable[[object], None]]  # (seq, work, apply)
_Reply = tuple[int, Callable[[object], None], object]  # (seq, apply, result)


class _RenderFailure(NamedTuple):
    """work 侧异常的信封: 失败也必须回主线程显形, 不在后台线程里吞。"""
    exc: BaseException


class RenderScheduler:
    """单工作线程 + 双通道渲染调度: 前台最新者胜, 后台 FIFO 恒让路。

    2026-09-19 真机定案: 大书 (扫描件) 单帧渲染 1-2s 全在 UI 线程同步跑,
    连续滚轮串行数帧即触发 Windows"未响应"/Mac 彩球。架构: work (栅格化/
    重采样, 纯 CPU 无 Tk) 在工作线程执行, (seq, apply, result) 入回复队列,
    由持有 Tk 的主线程 after 轮询投递 —— tkinter 跨线程调用不安全, 主线程
    轮询队列是官方姿势。取消语义双层: 取活时丢弃积压旧帧 (排队的必然已被
    更新的缩放/翻页取代), 回主线程后按 seq 比对再丢一次 (渲染中途又变了)。
    渲染中的旧帧最多浪费一帧算力, 不积压不串行。
    v2.4.2 缩略图起加后台通道 (submit_background): 缩略图一页一帧、一帧都
    不能丢, 进不了"最新者胜"通道; 但 MuPDF 文档线程封闭要求所有 doc 访问
    仍在同一条工作线程上串行 —— 故不另开线程, 只在同一循环里让前台帧恒
    优先 (翻页/缩放最多等一个在途缩略图, 量级几十毫秒, 不再排队等一串)。
    """

    def __init__(self) -> None:
        self._fg: deque[_Job] = deque()  # 前台: 翻页/缩放帧, 最新者胜
        self._bg: deque[_Job] = deque()  # 后台: 缩略图帧, FIFO 不丢
        self._cond = threading.Condition()
        self._closed = False
        self._replies: queue.SimpleQueue[_Reply] = queue.SimpleQueue()
        self._worker = threading.Thread(target=self._loop,
                                        name="usbvault-render", daemon=True)
        self._worker.start()

    def submit(self, seq: int, work: Callable[[], object],
               apply: Callable[[object], None]) -> None:
        """前台作业: 取活时只留最新一帧, 积压旧帧整批作废。"""
        with self._cond:
            self._fg.append((seq, work, apply))
            self._cond.notify()

    def submit_background(self, work: Callable[[], object],
                          apply: Callable[[object], None]) -> None:
        """后台作业 (seq 恒 -1): FIFO 全保留, 有前台帧时恒让路。"""
        with self._cond:
            self._bg.append((-1, work, apply))
            self._cond.notify()

    def take_reply(self) -> _Reply | None:
        """主线程轮询: 非阻塞取一条回复, 没有则 None。"""
        try:
            return self._replies.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        """停止调度: 当前在途作业做完即退, 未取活的作业不再执行。

        旧版走 None 哨兵排队 (FIFO 排空才退); 改双通道后积压可含几百张
        缩略图, 排空纯属浪费 —— destroy 之后 apply 侧全部被存活检查丢弃,
        直接置位退出语义等价且更干脆。
        """
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _next_job(self) -> _Job | None:
        with self._cond:
            while not self._fg and not self._bg and not self._closed:
                self._cond.wait()
            if self._closed:
                return None
            if self._fg:
                job = self._fg[-1]  # 最新前台帧; 更旧的在取活瞬间整批作废
                self._fg.clear()
                return job
            return self._bg.popleft()

    def _loop(self) -> None:
        """工作线程主体: 永不碰 Tk; 本线程拥有的对象才能在 work 里读写。"""
        while True:
            job = self._next_job()
            if job is None:
                return
            seq, work, apply = job
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 -- 失败也要回主线程显形
                result = _RenderFailure(exc)
            self._replies.put((seq, apply, result))


def _pil_from_pixmap(pix: Any) -> Any:
    """fitz 位图 -> PIL, 零压缩直构。仅工作线程调用。

    2026-09-19 实测 (A3@400%, 28.5M 像素): PNG 编码 0.90s + 解码 0.06s,
    占整帧 80%; frombytes 直构 0.11s —— 旧链路纯属为压缩付费。
    """
    from PIL import Image  # noqa: PLC0415
    if pix.n in (3, 4):
        mode = "RGB" if pix.n == 3 else "RGBA"
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    return Image.open(io.BytesIO(pix.tobytes("png")))  # 灰度/CMYK 罕见色彩兜底


def _zoom_bar(parent: tk.Widget,
              on_change: Callable[[int], None]) -> tuple[tk.Frame, tk.IntVar,
                                                          Callable[[], None]]:
    """缩放工具条 [− 滑杆 + | 100%]: 返回 (工具条, 百分比变量, 通知函数)。

    通知函数供 Ctrl+滚轮/按钮改值后同步滑杆并触发回调 —— IntVar.set 不
    会触发 Scale 的 command, 必须显式调。2026-09-18 收件人反馈原话:
    "可以放多大放多小是我自己选择的, 而不是点一下固定的"。
    """
    bar = tk.Frame(parent)
    var = tk.IntVar(value=100)

    def notify() -> None:
        on_change(int(var.get()))

    def set_and_notify(v: int) -> None:
        var.set(zoom_clamp(v))
        notify()

    tk.Button(bar, text="-", width=2,
              command=lambda: set_and_notify(var.get() - config.ZOOM_STEP_PCT)
              ).pack(side="left", padx=(8, 2), pady=4)
    tk.Scale(bar, from_=config.ZOOM_MIN_PCT, to=config.ZOOM_MAX_PCT,
             orient="horizontal", variable=var, command=lambda _v: notify(),
             resolution=config.ZOOM_SLIDER_RES_PCT,
             length=round(240 * ui_scale(parent))  # v2.4.3 裸像素随 DPI
             ).pack(side="left", padx=2)
    tk.Button(bar, text="+", width=2,
              command=lambda: set_and_notify(var.get() + config.ZOOM_STEP_PCT)
              ).pack(side="left", padx=2)
    tk.Button(bar, text="100%", width=5,
              command=lambda: set_and_notify(100)).pack(side="left", padx=2)
    tk.Label(bar, text="Ctrl+滚轮缩放", foreground="#888").pack(
        side="left", padx=10)
    return bar, var, notify


class ZoomableImage(tk.Frame):
    """图片查看: 连续缩放 (滑杆/Ctrl+滚轮/按钮) + 深底滚动画布, 后台渲染。

    2026-09-18 收件人反馈: 图片原先完全不能缩放。基图 = 解码后经
    IMAGE_MAX_SIDE 上限的位图, 缩放 = 基图重采样; RENDER_MAX_PIXELS 预算
    封顶防巨图炸内存。
    2026-09-19 大书卡顿根治: 解码/重采样全部搬进工作线程 (巨图解码本身
    可达秒级), 主线程只做 PhotoImage 转换与画布更新; _base 线程封闭
    (仅渲染线程读写), 主线程永不触碰解码态。
    """

    def __init__(self, parent: tk.Widget, data: bytes) -> None:
        super().__init__(parent)
        from PIL import ImageTk  # noqa: PLC0415 延迟导入, 减小 burner 端依赖面

        self._raw = data
        self._base: Any = None  # 线程封闭: 解码+thumbnail 后的基图, 仅渲染线程读写
        self._ImageTk = ImageTk
        self._photo: object | None = None  # 防 GC (PIL 延迟导入故 object)
        self._content = (1, 1)
        self._zoom_pct = 100
        self._after_id: str | None = None
        self._render_seq = 0
        self._burst: tuple[int, float, float, float, float] | None = None
        self._scheduler = RenderScheduler()
        bar, self._zoom_var, _ = _zoom_bar(self, self._on_zoom_change)
        bar.pack(fill="x")
        self._canvas = _scrolled_canvas(self)
        _bind_wheel(self._canvas)
        _bind_ctrl_zoom(self._canvas, self._zoom_delta)
        self._poll()  # 启动主线程取帧轮询链
        self._render_async()

    def destroy(self) -> None:
        self._scheduler.close()
        super().destroy()

    # -- 内部 ---------------------------------------------------------------

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _poll(self) -> None:
        """主线程取帧: 非阻塞轮询回复队列, seq 过期的帧直接丢弃。"""
        try:
            while True:
                reply = self._scheduler.take_reply()
                if reply is None:
                    break
                seq, apply, _result = reply
                if seq != self._render_seq:
                    continue  # 渲染途中又变了, 此帧作废
                try:
                    apply(_result)
                except tk.TclError:
                    return  # 部件已销毁
                except Exception as exc:  # noqa: BLE001 -- after 回调异常 tkinter 会吞 (v2.3.0 事故)
                    _render_error(self._canvas, exc)
        finally:
            try:
                self.after(_POLL_MS, self._poll)
            except tk.TclError:
                pass  # 部件已销毁, 轮询链自然终止

    def _render_async(self) -> None:
        self._after_id = None
        self._render_seq += 1
        seq = self._render_seq
        pct = self._zoom_pct
        raw = self._raw
        self._canvas.configure(cursor="watch")

        def work() -> Any:
            from PIL import Image  # noqa: PLC0415
            Image.MAX_IMAGE_PIXELS = config.IMAGE_MAX_PIXELS
            if self._base is None:
                img = Image.open(io.BytesIO(raw))
                img.load()
                if max(img.size) > config.IMAGE_MAX_SIDE:
                    img.thumbnail((config.IMAGE_MAX_SIDE, config.IMAGE_MAX_SIDE))
                self._base = img
            w, h = self._base.size
            eff = fit_pixels(w, h, pct)
            size = (max(1, round(w * eff / 100)), max(1, round(h * eff / 100)))
            return self._base.resize(size), eff

        def apply(result: Any) -> None:
            if isinstance(result, _RenderFailure):
                raise result.exc  # 统一走 _poll 的显形兜底
            if not self._alive():
                return
            img, _eff = result
            photo = self._ImageTk.PhotoImage(img)
            self._photo = photo
            self._canvas.delete("all")
            self._canvas.create_image(0, 0, anchor="nw", image=photo)
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            self._content = (photo.width(), photo.height())
            self._canvas.configure(cursor="")
            self._restore_burst_view()
            self._canvas.focus_set()  # 画布持焦: Windows 滚轮跟随焦点

        self._scheduler.submit(seq, work, apply)

    def _restore_burst_view(self) -> None:
        """连续滚轮的锚定还原: 目标视口按爆发起点到当前的总比例一次算准。"""
        if self._burst is None:
            return
        pct0, vx, vy, fx, fy = self._burst
        self._burst = None
        ratio = self._zoom_pct / pct0
        self._canvas.xview_moveto(zoom_anchor(vx, fx, ratio))
        self._canvas.yview_moveto(zoom_anchor(vy, fy, ratio))

    def _on_zoom_change(self, pct: int) -> None:
        """滑杆拖动: 高频触发, 渲染防抖 —— 拖动跟手, 松手定稿一次渲染。"""
        self._zoom_pct = pct
        _cancel_pending(self, self._after_id)
        self._after_id = self.after(_RENDER_DEBOUNCE_MS, self._render_async)

    def _zoom_delta(self, delta: int, cx: int, cy: int) -> None:
        old_pct = self._zoom_pct
        new_pct, moved = zoom_step(old_pct, delta)
        if not moved:
            return
        if self._burst is None:
            # 记爆发起点 (视口 + 光标内容坐标), 渲染落地后按总比例一次还原;
            # 防抖期内连续滚 N 格 = 数学上等价于从起点一步缩到 N 格终点
            w0, h0 = self._content
            self._burst = (old_pct,
                           self._canvas.xview()[0], self._canvas.yview()[0],
                           self._canvas.canvasx(cx) / w0 if w0 else 0.0,
                           self._canvas.canvasy(cy) / h0 if h0 else 0.0)
        self._zoom_var.set(new_pct)  # 同步滑杆 (set 不触发其 command, 探针实证)
        self._on_zoom_change(new_pct)  # 与滑杆同走防抖, 不再逐格整帧重渲


class PdfThumbPanel(tk.Frame):
    """PDF 页面缩略图面板 (v2.4.2, 依用户截图样例): 单列卡片 + "第N页"
    标签, 当前页蓝框高亮联动, 点击卡片直达该页, 面板 × 可关 (工具栏
    "缩略图"勾选框恢复)。

    防泄密原则不变: 缩略图只进内存、只上屏幕 (渲染走 PdfPager 的后台
    通道, 不落盘、无另存)。大部头对策: 占位卡全量铺设 (几百页 ≈ 每页
    3 个画布项, 千级项很轻; 2026-09-23 探针: 20 万 px scrollregion 坐标
    完好), 位图只渲可视区 ± THUMB_PREFETCH (懒加载), 缓存超上限按
    "离可视区最远"淘汰 —— 内存有界, 拖动滚动条渐进填充。
    """

    def __init__(self, parent: tk.Widget, *, page_count: int,
                 page_wh: tuple[int, int],
                 request_page: Callable[[int], None],
                 on_click: Callable[[int], None],
                 on_close: Callable[[], None]) -> None:
        super().__init__(parent, width=config.THUMB_PANEL_W, bd=0,
                         highlightthickness=0)
        from PIL import ImageTk  # noqa: PLC0415 延迟导入, 与渲染器同规
        self._ImageTk = ImageTk
        self._count = page_count
        self._request = request_page
        self._click_cb = on_click
        self._pending: set[int] = set()  # 已排队未落地的页
        self._images: dict[int, Any] = {}  # page -> PhotoImage (防 GC)
        self._vis: tuple[int, int] = (0, 0)
        self._hl_page = -1
        self._hl_rect: int | None = None
        self._refill_after: str | None = None
        # v2.4.3: 全部裸像素随 ui_scale 等比 (高分屏面板不再缩成一条缝),
        # 渲染目标宽/间距/槽高同步放大, 缓存上限按面积反压缩 (内存≈恒定)
        s = ui_scale(self)
        self._scale = s
        self._cache_cap = thumb_cache_cap(config.THUMB_CACHE_MAX, s)
        self.configure(width=round(config.THUMB_PANEL_W * s))
        self.pack_propagate(False)  # 面板宽钉死, 不随标签文字撑变

        # 槽位几何: 比例取首页 (书内页型基本一致; 个别异型页由 thumb_fit
        # 各自等比缩进槽内, 不撑破布局)
        aspect = page_wh[1] / max(1, page_wh[0])
        img_w = round(min(config.THUMB_WIDTH_PX,
                          config.THUMB_PANEL_W - 30) * s)
        img_h = max(1, round(img_w * aspect))
        self._img_wh = (img_w, img_h)
        self._canvas_w = img_w + round(16 * s)
        self._card_w = img_w + round(12 * s)
        self._slot_h = img_h + round(config.THUMB_SLOT_EXTRA_PX * s)
        self._gap = round(config.THUMB_GAP_PX * s)
        self._step = self._slot_h + self._gap
        self._img_top = (self._slot_h - img_h - round(14 * s)) // 2

        head = tk.Frame(self)
        head.pack(fill="x", pady=(4, 2))
        tk.Label(head, text=f"页面 · 共{page_count}页",
                 foreground="#666").pack(side="left", padx=10)
        tk.Button(head, text="×", width=2, relief="flat",
                  foreground="#888", command=on_close).pack(side="right")
        body = tk.Frame(self)
        body.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(body, width=self._canvas_w,
                                 background="#e8e8e8", highlightthickness=0)
        self._sb = ttk.Scrollbar(body, orient="vertical",
                                 command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._sb.pack(side="right", fill="y")

        self._total_h = max(10, self._count * self._step - self._gap)
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w,
                                             self._total_h))
        self._img_item: dict[int, int] = {}
        self._item_page: dict[int, int] = {}
        cx = self._canvas_w // 2
        for i in range(self._count):
            y = i * self._step
            pad = (self._canvas_w - self._card_w) // 2
            card = self._canvas.create_rectangle(
                pad, y, pad + self._card_w, y + self._slot_h,
                fill="#f5f5f5", outline="#d9d9d9")
            img_item = self._canvas.create_image(
                cx, y + self._img_top + img_h // 2, state="hidden")
            label = self._canvas.create_text(
                cx, y + self._slot_h - 9, text=f"第 {i + 1} 页", fill="#555")
            self._img_item[i] = img_item
            self._item_page[card] = i
            self._item_page[img_item] = i
            self._item_page[label] = i

        _bind_wheel(self._canvas)  # 滚轮翻面板; 其 Button-1 焦点绑定被下行的
        # 点击跳页覆盖 (点击语义优先)
        self._canvas.bind("<Button-1>", self._on_click)
        self._canvas.bind("<Configure>", lambda _e: self._schedule_refill())
        # 首次取活走 after_idle: 构造期回调 request_page 会撞上宿主
        # PdfPager 尚未完成的 self._thumbs 赋值 (同步回调 = AttributeError)
        self.after_idle(self._refill)

    # -- 对外 (PdfPager 调用) ----------------------------------------------

    @property
    def image_box(self) -> tuple[int, int]:
        """缩略图渲染的目标槽位 (等比缩放的 box, 传给渲染线程)。"""
        return self._img_wh

    def highlight(self, page: int) -> None:
        """当前页高亮 + 联动滚动 (同页幂等: 缩放重渲不抢用户的滚动位置)。"""
        if page == self._hl_page:
            return
        self._hl_page = page
        y = page * self._step
        pad = (self._canvas_w - self._card_w) // 2
        coords = (pad - 3, y - 3, pad + self._card_w + 3,
                  y + self._slot_h + 3)
        if self._hl_rect is None:
            self._hl_rect = self._canvas.create_rectangle(
                *coords, outline="#1a66d6", width=2)
        else:
            self._canvas.coords(self._hl_rect, *coords)
        self._ensure_visible(page)

    def set_thumb(self, page: int, pil_img: Any, _w: int, h: int) -> None:
        """位图落卡 (主线程): 重复/迟到帧安全忽略, 落地后按上限淘汰。"""
        self._pending.discard(page)
        if page in self._images or page >= self._count or page < 0:
            return
        photo = self._ImageTk.PhotoImage(pil_img)
        self._images[page] = photo
        cx = self._canvas_w // 2
        cy = page * self._step + self._img_top + (self._img_wh[1] + h) // 2
        item = self._img_item[page]
        self._canvas.coords(item, cx, cy)
        self._canvas.itemconfigure(item, image=photo, state="normal")
        # 淘汰保护带 = 可视区 ± 预取: 只保护可视区会"刚渲的预取页立刻被
        # 淘汰 → refill 再排队 → 渲了丢丢了渲"的死循环
        if len(self._images) > self._cache_cap:
            zone = thumb_request_range(self._vis[0], self._vis[1],
                                       self._count, config.THUMB_PREFETCH)
            for out in thumb_evict_keys(list(self._images), zone[0], zone[1],
                                        self._cache_cap):
                self._canvas.itemconfigure(self._img_item[out],
                                           state="hidden")
                del self._images[out]

    # -- 内部 ---------------------------------------------------------------

    def _on_yscroll(self, first: float, last: float) -> None:
        # typeshed 把回调参数标为 float, Tk 实际传的是 "0.043" 型字符串;
        # 两相兼容: 收下即转 str 交给 Scrollbar.set
        self._sb.set(str(first), str(last))
        self._schedule_refill()

    def _on_click(self, _event: tk.Event) -> str:
        items = self._canvas.find_withtag("current")
        if items:
            page = self._item_page.get(items[0])
            if page is not None:
                self._click_cb(page)
        return "break"

    def _visible_range(self) -> tuple[int, int]:
        h = self._canvas.winfo_height()
        if h <= 1:  # 未映射: 先取首页邻域, 映射后 <Configure> 补齐
            return thumb_request_range(0, -1, self._count,
                                       config.THUMB_PREFETCH)
        y0, y1 = self._canvas.yview()
        return thumb_visible_range(y0 * self._total_h, y1 * self._total_h,
                                   self._slot_h, self._gap, self._count)

    def _schedule_refill(self) -> None:
        """滚动/resize 高频触发: 120ms 合并一次取活 (与渲染防抖同窗口)。"""
        _cancel_pending(self, self._refill_after)
        self._refill_after = self.after(_RENDER_DEBOUNCE_MS, self._refill)

    def _refill(self) -> None:
        self._refill_after = None
        try:  # 部件已销毁: 静默终止取活链 (after 回调的 TclError 只打 stderr)
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        first, last = self._visible_range()
        self._vis = (first, last)
        lo, hi = thumb_request_range(first, last, self._count,
                                     config.THUMB_PREFETCH)
        for page in range(lo, hi + 1):
            if page not in self._images and page not in self._pending:
                self._pending.add(page)
                self._request(page)

    def _ensure_visible(self, page: int) -> None:
        """当前页滚出视口时把面板滚到它居中 (用户翻页 → 面板跟页)。"""
        h = self._canvas.winfo_height()
        if h <= 1:
            return
        y0, y1 = self._canvas.yview()
        top = page * self._step
        if top < y0 * self._total_h or top + self._slot_h > y1 * self._total_h:
            target = top - (h - self._slot_h) / 2
            frac = min(1.0, max(0.0, target / self._total_h))
            self._canvas.yview_moveto(frac)


class PdfPager(tk.Frame):
    """PDF 逐页渲染: fitz 出图 -> Pillow -> Canvas, 连续缩放同 ZoomableImage。

    2026-09-17 真机反馈修复 (保留): 工具栏置顶防挤出; 画布内平移防撑破;
    首/末页禁用按钮 + 页码标注。
    2026-09-18 收件人反馈: 三档"清晰度"循环按钮 → 滑杆 25-400% 连续缩放。
    2026-09-19 大书卡顿根治: 栅格化搬进工作线程 (扫描件大页单帧可达秒级,
    同步渲染把 UI 线程堵死 = Windows"未响应"); doc 自构造后线程封闭给渲染
    线程, 主线程只读冻结的 _page_count 副本 —— MuPDF 文档跨线程并发访问
    不安全, 主线程自此零触碰。
    2026-09-23 页码跳转: 工具栏页码框输页码回车直达 (大部头翻页刚需,
    解析/校验在 pdf_page_jump 纯函数)。
    2026-09-23 页面缩略图: 正文左侧内嵌 PdfThumbPanel (单列卡片/当前页
    高亮/点击跳页), 渲染走本调度器的后台通道 —— doc 线程封闭不破, 翻页
    缩放帧恒优先。
    """

    def __init__(self, parent: tk.Widget, data: bytes,
                 probe_label: str | None = None) -> None:
        super().__init__(parent)
        import fitz  # type: ignore[import-untyped]  # noqa: PLC0415 PyMuPDF 仅查看器需要
        from PIL import ImageTk  # noqa: PLC0415

        # Phase 0 装载计时 (app._load_bytes 已记"装载"行): 这里记构造分段与
        # 首帧; probe_label 只由 app 在探针开启时传入, 默认 None = 零开销
        probe = loadtimer.enabled() and probe_label is not None
        self._probe_label = probe_label
        self._probe_first = probe
        st = loadtimer.StageTimer() if probe else None
        if st is not None:
            st.begin()

        self.doc = fitz.open(stream=data, filetype="pdf")
        self._page_count = self.doc.page_count  # 冻结副本: 主线程翻页判断用
        self.page_no = 0  # 0-based
        self._ImageTk = ImageTk
        self._photo: object | None = None  # 防 GC (PIL 延迟导入故 object)
        self._content = (1, 1)
        self._zoom_pct = 100
        self._after_id: str | None = None
        self._render_seq = 0
        self._burst: tuple[int, float, float, float, float] | None = None
        self._scheduler = RenderScheduler()
        # 缩略图槽位比例取首页宽高: 此刻仍在主线程、doc 尚未交给渲染线程
        # (首帧提交后才进入线程封闭), 读一次 rect 合规
        page0 = self.doc[0]
        page0_wh = (round(page0.rect.width), round(page0.rect.height))
        if st is not None:
            st.mark("fitz.open+首页元数据")

        # 工具栏固定最上: 无论页面图多大, 翻页/缩放控件永远可点
        bar = tk.Frame(self)
        bar.pack(fill="x")
        self._info = tk.Label(bar, text="")
        self._info.pack(side="left", padx=8)
        self._prev_btn = tk.Button(bar, text="上一页", command=self.prev)
        self._prev_btn.pack(side="left", padx=4, pady=4)
        self._next_btn = tk.Button(bar, text="下一页", command=self.next)
        self._next_btn.pack(side="left", padx=4)
        # 页码跳转 (2026-09-23 用户需求): 大部头翻页靠按钮太慢 —— 输页码
        # 回车即跳; 点进输入框全选旧值, 直接敲新页码覆盖
        self._jump_var = tk.StringVar()
        self._jump_entry = tk.Entry(
            bar, textvariable=self._jump_var, width=6, justify="center"
        )
        self._jump_entry.pack(side="left", padx=(10, 2), pady=4)
        self._jump_entry.bind("<Return>", lambda _e: self.jump())
        self._jump_entry.bind(
            "<FocusIn>", lambda _e: self._jump_entry.select_range(0, "end")
        )
        self._jump_btn = tk.Button(bar, text="跳转", command=self.jump)
        self._jump_btn.pack(side="left", padx=(0, 10))
        # 缩略图开关 (v2.4.2): 面板默认展开; 面板内 × 与此勾选框双向同步
        self._thumbs_var = tk.BooleanVar(value=True)
        tk.Checkbutton(bar, text="缩略图", variable=self._thumbs_var,
                       command=self._toggle_thumbs).pack(side="left",
                                                         padx=(0, 10))
        zbar, self._zoom_var, _ = _zoom_bar(bar, self._on_zoom_change)
        zbar.pack(side="left")
        if st is not None:
            st.mark("工具栏")

        body = tk.Frame(self)
        body.pack(fill="both", expand=True)
        self._thumbs = PdfThumbPanel(
            body, page_count=self._page_count, page_wh=page0_wh,
            request_page=self._thumb_request, on_click=self.goto,
            on_close=self._close_thumbs)
        self._thumbs.pack(side="left", fill="y")
        if st is not None:
            st.mark("缩略图占位卡")
        self._canvas = _scrolled_canvas(body)
        _bind_wheel(self._canvas)
        _bind_ctrl_zoom(self._canvas, self._zoom_delta)
        self._poll()  # 启动主线程取帧轮询链
        self._render_async()
        if st is not None:
            loadtimer.log(f"[{probe_label}] 构造 {st.line()}")

    def destroy(self) -> None:
        self._scheduler.close()
        super().destroy()

    # -- 内部 ---------------------------------------------------------------

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _poll(self) -> None:
        """主线程取帧: 非阻塞轮询回复队列, seq 过期的帧直接丢弃。"""
        try:
            while True:
                reply = self._scheduler.take_reply()
                if reply is None:
                    break
                seq, apply, _result = reply
                if seq >= 0 and seq != self._render_seq:
                    continue  # 前台帧渲染途中翻页/再缩放了, 此帧作废;
                    # 后台缩略图 (seq=-1) 不参与最新者胜, 落地时自校验
                try:
                    apply(_result)
                except tk.TclError:
                    return  # 部件已销毁
                except Exception as exc:  # noqa: BLE001 -- after 回调异常 tkinter 会吞 (v2.3.0 事故)
                    _render_error(self._canvas, exc)
        finally:
            try:
                self.after(_POLL_MS, self._poll)
            except tk.TclError:
                pass  # 部件已销毁, 轮询链自然终止

    def _render_async(self) -> None:
        self._after_id = None
        self._render_seq += 1
        seq = self._render_seq
        page_no = self.page_no
        pct = self._zoom_pct
        doc = self.doc  # 线程封闭: 自此只有渲染线程访问 doc
        self._canvas.configure(cursor="watch")
        self._info.configure(text="正在渲染…")
        # Phase 0 探针: 只记首帧 (构造后第一次渲染)。栅格耗时在工作线程测、
        # 经闭包携带回传; 提交→上屏在 apply (主线程) 收口, 含队列等待与
        # 30ms 轮询周期
        probe_first = self._probe_first
        t_submit = time.perf_counter()
        raster = [0.0]
        if probe_first:
            self._probe_first = False

        def work() -> Any:
            t0 = time.perf_counter()
            page = doc[page_no]
            rw, rh = round(page.rect.width), round(page.rect.height)
            eff = fit_pixels(rw, rh, pct)
            pix = page.get_pixmap(dpi=dpi_for_pct(eff))
            result = (_pil_from_pixmap(pix), eff)
            raster[0] = time.perf_counter() - t0
            return result

        def apply(result: Any) -> None:
            if isinstance(result, _RenderFailure):
                raise result.exc  # 统一走 _poll 的显形兜底
            if not self._alive() or self.page_no != page_no:
                return
            img, eff = result
            photo = self._ImageTk.PhotoImage(img)
            self._photo = photo
            self._canvas.delete("all")
            self._canvas.create_image(0, 0, anchor="nw", image=photo)
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            self._content = (photo.width(), photo.height())
            self._info.configure(
                text=pdf_page_label(self.page_no, self._page_count)
                + (f"  |  {eff}%" if eff != self._zoom_pct else ""))
            self._prev_btn.configure(
                state="normal" if self.page_no > 0 else "disabled")
            self._next_btn.configure(
                state="normal" if self.page_no < self._page_count - 1 else "disabled")
            self._canvas.configure(cursor="")
            self._restore_burst_view()
            self._canvas.focus_set()  # 画布持焦: Windows 滚轮跟随焦点
            self._thumbs.highlight(self.page_no)  # 当前页联动 (同页幂等)
            if probe_first:
                loadtimer.log(
                    f"[{self._probe_label}] 首帧 "
                    f"提交→上屏 {time.perf_counter() - t_submit:.2f}s "
                    f"(栅格 {raster[0]:.2f}s)"
                )

        self._scheduler.submit(seq, work, apply)

    def _restore_burst_view(self) -> None:
        """连续滚轮的锚定还原: 目标视口按爆发起点到当前的总比例一次算准。"""
        if self._burst is None:
            return
        pct0, vx, vy, fx, fy = self._burst
        self._burst = None
        ratio = self._zoom_pct / pct0
        self._canvas.xview_moveto(zoom_anchor(vx, fx, ratio))
        self._canvas.yview_moveto(zoom_anchor(vy, fy, ratio))

    def _on_zoom_change(self, pct: int) -> None:
        self._zoom_pct = pct
        _cancel_pending(self, self._after_id)
        self._after_id = self.after(_RENDER_DEBOUNCE_MS, self._render_async)

    def _zoom_delta(self, delta: int, cx: int, cy: int) -> None:
        old_pct = self._zoom_pct
        new_pct, moved = zoom_step(old_pct, delta)
        if not moved:
            return
        if self._burst is None:
            # 记爆发起点 (视口 + 光标内容坐标), 渲染落地后按总比例一次还原;
            # 防抖期内连续滚 N 格 = 数学上等价于从起点一步缩到 N 格终点
            w0, h0 = self._content
            self._burst = (old_pct,
                           self._canvas.xview()[0], self._canvas.yview()[0],
                           self._canvas.canvasx(cx) / w0 if w0 else 0.0,
                           self._canvas.canvasy(cy) / h0 if h0 else 0.0)
        self._zoom_var.set(new_pct)  # 同步滑杆 (set 不触发其 command, 探针实证)
        self._on_zoom_change(new_pct)  # 与滑杆同走防抖, 不再逐格整帧重渲

    # -- 公共 ---------------------------------------------------------------

    def prev(self) -> None:
        new_no, moved = pdf_page_step(self.page_no, self._page_count, -1)
        if moved:
            self.page_no = new_no
            self._render_async()

    def next(self) -> None:
        new_no, moved = pdf_page_step(self.page_no, self._page_count, 1)
        if moved:
            self.page_no = new_no
            self._render_async()

    def jump(self) -> None:
        """页码跳转: 非法输入只提示不渲染 (页码文案暂时让位给范围提示)。"""
        target = pdf_page_jump(self._jump_var.get(), self._page_count)
        if target is None:
            self._info.configure(text=f"请输入 1~{self._page_count} 之间的页码")
            return
        self._jump_var.set("")  # 跳走即清空, 下次进来不用先删
        self.goto(target)

    def goto(self, page_no: int) -> None:
        """直达指定页 (0-based; 缩略图点击与页码跳转共用), 同页不重渲染。"""
        if 0 <= page_no < self._page_count and page_no != self.page_no:
            self.page_no = page_no
            self._render_async()

    def _toggle_thumbs(self) -> None:
        """工具栏勾选框: 开 = 面板回到正文左侧 (pack 在画布前, 占左缘)。"""
        if self._thumbs_var.get():
            self._thumbs.pack(side="left", fill="y",
                              before=self._canvas.master)
        else:
            self._thumbs.pack_forget()

    def _close_thumbs(self) -> None:
        """面板内 ×: 收起面板并同步勾选框 (两处开关恒一致)。"""
        self._thumbs_var.set(False)
        self._thumbs.pack_forget()

    def _thumb_request(self, page_no: int) -> None:
        """缩略图渲染作业: 与主视图共用 doc 与渲染线程 (线程封闭不破),
        走后台通道 (FIFO 不丢, 恒让路前台翻页/缩放帧)。"""
        doc = self.doc
        box_w, box_h = self._thumbs.image_box

        def work() -> Any:
            page = doc[page_no]
            # 渲染目标宽随面板 scale (高分屏缩略图不糊成马赛克)
            pix = page.get_pixmap(dpi=thumb_dpi(round(page.rect.width),
                                                target_w=box_w))
            img = _pil_from_pixmap(pix)
            w, h = thumb_fit(img.width, img.height, box_w, box_h)
            return img.resize((w, h)), w, h

        def apply(result: Any) -> None:
            if isinstance(result, _RenderFailure):
                return  # 单页缩略图失败: 留占位卡, 该页主视图仍可开 ——
                # 不写主画布 (那属于页渲染失败的显形通道)
            if not self._alive():
                return
            pil, _w, _h = result
            self._thumbs.set_thumb(page_no, pil, _w, _h)

        self._scheduler.submit_background(work, apply)


def dispatch(media: str) -> Callable[..., Any]:
    """media -> 渲染函数 (app.py 调用; 便于测试替换)。"""
    kind = renderer_kind(media)
    table: dict[str, Callable[..., Any]] = {
        "text": render_text,
        "image": render_image,
        "binary": render_binary_meta,
        "pdf": PdfPager,
    }
    return table[kind]
