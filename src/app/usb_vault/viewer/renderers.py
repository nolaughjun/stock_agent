"""内容渲染器: 文本/图片/PDF 内置渲染, 二进制仅元数据。

防泄密原则: 内容只进内存、只上屏幕 —— 禁选中复制、无另存、无拖出、
不落盘、不调用外部程序。
"""

from __future__ import annotations

import io
import queue
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any, NamedTuple

from src.app.usb_vault import config
from src.app.usb_vault.manifest import FileEntry

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
    label = tk.Label(parent, text=binary_meta_text(entry), justify="left",
                     anchor="nw", padx=16, pady=12, wraplength=560)
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
    """单工作线程 + 最新者胜的渲染调度。

    2026-09-19 真机定案: 大书 (扫描件) 单帧渲染 1-2s 全在 UI 线程同步跑,
    连续滚轮串行数帧即触发 Windows"未响应"/Mac 彩球。架构: work (栅格化/
    重采样, 纯 CPU 无 Tk) 在工作线程执行, (seq, apply, result) 入回复队列,
    由持有 Tk 的主线程 after 轮询投递 —— tkinter 跨线程调用不安全, 主线程
    轮询队列是官方姿势。取消语义双层: 取活时丢弃积压旧帧 (排队的必然已被
    更新的缩放/翻页取代), 回主线程后按 seq 比对再丢一次 (渲染中途又变了)。
    渲染中的旧帧最多浪费一帧算力, 不积压不串行。
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._replies: queue.SimpleQueue[_Reply] = queue.SimpleQueue()
        self._worker = threading.Thread(target=self._loop,
                                        name="usbvault-render", daemon=True)
        self._worker.start()

    def submit(self, seq: int, work: Callable[[], object],
               apply: Callable[[object], None]) -> None:
        self._jobs.put((seq, work, apply))

    def take_reply(self) -> _Reply | None:
        """主线程轮询: 非阻塞取一条回复, 没有则 None。"""
        try:
            return self._replies.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        self._jobs.put(None)

    def _loop(self) -> None:
        """工作线程主体: 永不碰 Tk; 本线程拥有的对象才能在 work 里读写。"""
        closing = False
        while not closing:
            job = self._jobs.get()
            if job is None:
                return
            while True:  # 丢弃积压, 只渲染最新一帧
                try:
                    nxt = self._jobs.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    closing = True
                    break
                job = nxt
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
             resolution=config.ZOOM_SLIDER_RES_PCT, length=240
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


class PdfPager(tk.Frame):
    """PDF 逐页渲染: fitz 出图 -> Pillow -> Canvas, 连续缩放同 ZoomableImage。

    2026-09-17 真机反馈修复 (保留): 工具栏置顶防挤出; 画布内平移防撑破;
    首/末页禁用按钮 + 页码标注。
    2026-09-18 收件人反馈: 三档"清晰度"循环按钮 → 滑杆 25-400% 连续缩放。
    2026-09-19 大书卡顿根治: 栅格化搬进工作线程 (扫描件大页单帧可达秒级,
    同步渲染把 UI 线程堵死 = Windows"未响应"); doc 自构造后线程封闭给渲染
    线程, 主线程只读冻结的 _page_count 副本 —— MuPDF 文档跨线程并发访问
    不安全, 主线程自此零触碰。
    """

    def __init__(self, parent: tk.Widget, data: bytes) -> None:
        super().__init__(parent)
        import fitz  # type: ignore[import-untyped]  # noqa: PLC0415 PyMuPDF 仅查看器需要
        from PIL import ImageTk  # noqa: PLC0415

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

        # 工具栏固定最上: 无论页面图多大, 翻页/缩放控件永远可点
        bar = tk.Frame(self)
        bar.pack(fill="x")
        self._info = tk.Label(bar, text="")
        self._info.pack(side="left", padx=8)
        self._prev_btn = tk.Button(bar, text="上一页", command=self.prev)
        self._prev_btn.pack(side="left", padx=4, pady=4)
        self._next_btn = tk.Button(bar, text="下一页", command=self.next)
        self._next_btn.pack(side="left", padx=4)
        zbar, self._zoom_var, _ = _zoom_bar(bar, self._on_zoom_change)
        zbar.pack(side="left")

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
                    continue  # 渲染途中翻页/再缩放了, 此帧作废
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

        def work() -> Any:
            page = doc[page_no]
            rw, rh = round(page.rect.width), round(page.rect.height)
            eff = fit_pixels(rw, rh, pct)
            pix = page.get_pixmap(dpi=dpi_for_pct(eff))
            return _pil_from_pixmap(pix), eff

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
