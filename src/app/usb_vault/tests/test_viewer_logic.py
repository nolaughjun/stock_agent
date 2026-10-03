"""viewer 纯逻辑单测 (不启 Tk): 有效期 / 软限制状态 / 渲染分发 / 文本截断。"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from src.app.usb_vault import config, unlock_code
from src.app.usb_vault.manifest import FileEntry
from src.app.usb_vault.viewer import loadtimer, renderers, state

# 隔离开发机真实 dist/ (conftest.dist_isolated): 文件内 gate 用例会 burn,
# 防止捡到真 .app 触发内嵌 SEED 硬拦
pytestmark = pytest.mark.usefixtures("dist_isolated")


# ------------------------------------------------------------------ 有效期


def test_expiry_none_is_forever() -> None:
    assert state.check_expiry(None) is False


def test_expiry_future() -> None:
    assert state.check_expiry("2999-01-01", today=date(2026, 9, 10)) is False


def test_expiry_today_still_valid() -> None:
    assert state.check_expiry("2026-09-10", today=date(2026, 9, 10)) is False


def test_expiry_past() -> None:
    assert state.check_expiry("2026-09-09", today=date(2026, 9, 10)) is True


# ------------------------------------------------------------------ 软限制


def test_state_fresh(tmp_path: Path) -> None:
    st = state.SoftLimitState("ab" * 16, base_dir=tmp_path)
    assert st.load_opens() == 0
    assert st.remaining(5) == 5
    assert st.remaining(None) is None


def test_state_record_and_persist(tmp_path: Path) -> None:
    nonce = "cd" * 16
    st = state.SoftLimitState(nonce, base_dir=tmp_path)
    assert st.record_open() == 1
    assert st.record_open() == 2
    # 新实例从盘上恢复 => 真持久化
    assert state.SoftLimitState(nonce, base_dir=tmp_path).load_opens() == 2


def test_state_corrupt_file_tolerated(tmp_path: Path) -> None:
    p = state.state_path("ef" * 16, tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("不是json", encoding="utf-8")
    st = state.SoftLimitState("ef" * 16, base_dir=tmp_path)
    assert st.load_opens() == 0  # 坏文件按 0 处理, 不挡阅读
    assert st.record_open() == 1


def test_state_json_schema(tmp_path: Path) -> None:
    nonce = "ab" * 16
    state.SoftLimitState(nonce, base_dir=tmp_path).record_open()
    data = json.loads(state.state_path(nonce, tmp_path).read_text(encoding="utf-8"))
    assert data == {"opens": 1}


# ------------------------------------------------------------------ 盘定位 (app._locate_drive)


def test_locate_drive_mac_walks_up_to_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")
    exe = Path("/Volumes/资料盘/图书阅览器.app/Contents/MacOS/图书阅览器")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert viewer_app.ViewerApp._locate_drive(None) == Path("/Volumes/资料盘")


def test_locate_drive_mac_not_on_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从 /Applications 跑: 上溯不到 /Volumes => 回落盘根, 由门禁给出明确报错。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")
    exe = Path("/Applications/图书阅览器.app/Contents/MacOS/图书阅览器")
    monkeypatch.setattr(sys, "executable", str(exe))
    assert viewer_app.ViewerApp._locate_drive(None) == Path("/")


def test_locate_drive_explicit_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.app.usb_vault.viewer import app as viewer_app

    assert viewer_app.ViewerApp._locate_drive(Path("E:/")) == Path("E:/")


# ------------------------------------------------------------------ 渲染分发


def test_renderer_kind_mapping() -> None:
    assert renderers.renderer_kind("text") == "text"
    assert renderers.renderer_kind("image") == "image"
    assert renderers.renderer_kind("pdf") == "pdf"
    assert renderers.renderer_kind("binary") == "binary"
    assert renderers.renderer_kind("unknown-media") == "binary"  # 未知兜底


def test_truncate_text_short() -> None:
    text, truncated = renderers.truncate_text("你好abc".encode())
    assert text == "你好abc"
    assert truncated is False


def test_truncate_text_over_cap() -> None:
    data = b"x" * (config.TEXT_CAP + 10)
    text, truncated = renderers.truncate_text(data, cap=100)
    assert len(text) == 100
    assert truncated is True


def test_truncate_text_exact_cap() -> None:
    text, truncated = renderers.truncate_text(b"y" * 100, cap=100)
    assert truncated is False


def test_binary_meta_text_contains_info() -> None:
    entry = FileEntry(
        id="a" * 32,
        rel_path="数据/data.zip",
        size=12345,
        enc_size=13000,
        sha256="0" * 64,
        enc_sha256="0" * 64,
        media="binary",
    )
    text = renderers.binary_meta_text(entry)
    assert "数据/data.zip" in text
    assert "12,345" in text
    assert "不支持" in text


# ------------------------------------------------------------------ 解锁码门禁循环
# (open_manifest_with_code 是 _startup_gate 的核心, 独立函数才测得了重试语义)

SERIAL = 0x11223344


@pytest.fixture()
def pw_drive(tmp_path: Path) -> tuple[Path, bytes, str]:
    """密码模式烧录环境: (盘根, seed, 正确码)。"""
    import os

    from src.app.usb_vault.burner import BurnOptions, burn

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("内容A", encoding="utf-8")
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    seed = os.urandom(64)
    report = burn(
        src,
        tmp_path / "USB",
        seed,
        BurnOptions(
            require_root=False,
            viewer_exe=viewer,
            convert_office=False,
            skip_scan=True,
            use_password=True,
        ),
        serial_getter=lambda _p: SERIAL,
    )
    assert report.unlock_code is not None
    return tmp_path / "USB", seed, report.unlock_code


class _FakeAsker:
    """按脚本依次吐出输入的假输入框; None 表示用户取消。"""

    def __init__(self, script: list[str | None]) -> None:
        self.script = list(script)
        self.hints: list[str | None] = []

    def __call__(self, hint: str | None) -> str | None:
        self.hints.append(hint)
        return self.script.pop(0)


def test_gate_plain_drive_never_asks(tmp_path: Path) -> None:
    import os

    from src.app.usb_vault.burner import BurnOptions, burn
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("A", encoding="utf-8")
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    seed = os.urandom(64)
    burn(
        src,
        tmp_path / "USB",
        seed,
        BurnOptions(require_root=False, viewer_exe=viewer, convert_office=False, skip_scan=True),
        serial_getter=lambda _p: SERIAL,
    )
    asker = _FakeAsker([])
    result = open_manifest_with_code(tmp_path / "USB", seed, asker, serial_getter=lambda _p: SERIAL)
    assert not isinstance(result, str)
    assert len(result[0].files) == 1
    assert asker.hints == []  # 无码盘绝不弹框


def test_gate_plain_drive_wrong_serial_shows_drive_error(tmp_path: Path) -> None:
    """无码盘换了序列号: 直接报"仅限指定U盘", 不询问任何码。"""
    import os

    from src.app.usb_vault.burner import BurnOptions, burn
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("A", encoding="utf-8")
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    seed = os.urandom(64)
    drive = tmp_path / "USB"
    burn(
        src,
        drive,
        seed,
        BurnOptions(require_root=False, viewer_exe=viewer, convert_office=False, skip_scan=True),
        serial_getter=lambda _p: SERIAL,
    )
    result = open_manifest_with_code(
        drive, seed, _FakeAsker([]), serial_getter=lambda _p: 0xAABBCCDD
    )
    assert isinstance(result, str)
    assert "仅限指定的U盘" in result


def test_gate_volume_error_shows_friendly_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """双通道全断 (macOS /dev 权限 + diskutil 无可用 UUID): 门禁返回人话文案。

    VolumeIdError 不是 InspectorError 子类 —— 修此 bug 前它会带着堆栈穿出
    open_manifest_with_code, --windowed 下进程直接消失, 收件人看到的是
    "输码后闪退、无任何弹窗" (2026-09-15 真机)。
    """
    import os

    from src.app.usb_vault import volume_id
    from src.app.usb_vault.burner import BurnOptions, burn
    from src.app.usb_vault.viewer.app import open_manifest_with_code
    from src.app.usb_vault.volume_id import VolumeIdError

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("A", encoding="utf-8")
    viewer = tmp_path / "v.exe"
    viewer.write_bytes(b"X")
    seed = os.urandom(64)
    burn(
        src,
        tmp_path / "USB",
        seed,
        BurnOptions(require_root=False, viewer_exe=viewer, convert_office=False, skip_scan=True),
        serial_getter=lambda _p: SERIAL,
    )

    def denied(_p: Path) -> int:
        raise VolumeIdError("读取U盘引导扇区权限不足")

    def no_fallback(_root: Path) -> list[int]:
        raise VolumeIdError("免特权通道 diskutil 未提供 VolumeUUID")

    monkeypatch.setattr(volume_id, "get_volume_serial_candidates", no_fallback)
    result = open_manifest_with_code(tmp_path / "USB", seed, _FakeAsker([]), serial_getter=denied)
    assert isinstance(result, str)
    assert "读取U盘信息失败" in result
    assert "免特权" in result  # 双通道都断时给的是最终原因 (免特权通道状态)


def _deny_all_serial_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 macOS 真机: 直读 (_serial_candidates 首选) 与免特权候选双双断。

    自定义 serial_getter 抛 VolumeIdError 后, inspector 会回退到真实的
    volume_id.get_volume_serial_candidates —— 测试里必须一并封死, 否则在
    Windows 测试机上会真去读 E: 盘序列号, 表现成 WrongDriveError。
    """
    from src.app.usb_vault import volume_id
    from src.app.usb_vault.volume_id import VolumeIdError

    def no_fallback(_root: Path) -> list[int]:
        raise VolumeIdError("免特权通道不可用: diskutil 信息里没有 VolumeUUID 键")

    monkeypatch.setattr(volume_id, "get_volume_serial_candidates", no_fallback)


def test_gate_admin_serial_recovery_success(
    pw_drive: tuple[Path, bytes, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS 权限恢复: 双通道全断后管理员授权拿到精确序列号 → 门禁通过。

    2026-09-15 真机链路: /dev EPERM + exFAT 无 VolumeUUID → 用户点授权、
    输一次管理员密码 → osascript 代读引导扇区 → 恢复后的序列号与已输入
    的解锁码原样复用 (不再重复问码)。
    """
    from src.app.usb_vault.viewer.app import open_manifest_with_code
    from src.app.usb_vault.volume_id import VolumeIdError

    _deny_all_serial_channels(monkeypatch)
    drive, seed, code = pw_drive

    def denied(_p: Path) -> int:
        raise VolumeIdError("读取U盘引导扇区权限不足")

    asker = _FakeAsker([code])
    result = open_manifest_with_code(
        drive, seed, asker, serial_getter=denied, admin_serial=lambda _d: SERIAL
    )
    assert not isinstance(result, str)
    assert len(result[0].files) == 1
    assert asker.hints == [None]  # 授权前后都只问过一次码


def test_gate_admin_serial_declined_still_fails(
    pw_drive: tuple[Path, bytes, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户拒绝授权 (或无管理员凭据): 回到原报错文案, 不静默不崩溃。

    2026-09-18: 授权通道被尝试过且失败 → 文案追加"终端重跑看原因"指引
    (admin_read_serial 已把原图打到 stderr), 不再让收件人对着一句
    "权限不足"猜三天。
    """
    from src.app.usb_vault.viewer.app import open_manifest_with_code
    from src.app.usb_vault.volume_id import VolumeIdError

    _deny_all_serial_channels(monkeypatch)
    drive, seed, code = pw_drive

    def denied(_p: Path) -> int:
        raise VolumeIdError("读取U盘引导扇区权限不足")

    asker = _FakeAsker([code])
    result = open_manifest_with_code(
        drive, seed, asker, serial_getter=denied, admin_serial=lambda _d: None
    )
    assert isinstance(result, str)
    assert "读取U盘信息失败" in result
    assert "免特权通道不可用" in result  # 最终原因是候选通道的失败详情
    assert "管理员授权读取未完成" in result
    assert "终端" in result  # 指引: 终端启动重跑, stderr 有原图


def test_gate_password_right_code_first_try(pw_drive: tuple[Path, bytes, str]) -> None:
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    drive, seed, code = pw_drive
    asker = _FakeAsker([code])
    result = open_manifest_with_code(drive, seed, asker, serial_getter=lambda _p: SERIAL)
    assert not isinstance(result, str)
    assert len(result[0].files) == 1
    assert asker.hints == [None]  # 首次无错误提示


def test_gate_password_cancel(pw_drive: tuple[Path, bytes, str]) -> None:
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    drive, seed, _code = pw_drive
    result = open_manifest_with_code(
        drive, seed, _FakeAsker([None]), serial_getter=lambda _p: SERIAL
    )
    assert isinstance(result, str)
    assert "未输入解锁码" in result


def test_gate_password_retry_then_success(pw_drive: tuple[Path, bytes, str]) -> None:
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    drive, seed, code = pw_drive
    asker = _FakeAsker([unlock_code.generate(), "  junk  ", code])
    result = open_manifest_with_code(drive, seed, asker, serial_getter=lambda _p: SERIAL)
    assert not isinstance(result, str)
    assert len(result[0].files) == 1
    assert asker.hints == [None, "解锁码错误或非本盘资料, 请重试", "解锁码错误或非本盘资料, 请重试"]


def test_gate_password_exhausted_retries(pw_drive: tuple[Path, bytes, str]) -> None:
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    drive, seed, _code = pw_drive
    asker = _FakeAsker([unlock_code.generate() for _ in range(4)])
    result = open_manifest_with_code(
        drive, seed, asker, max_tries=3, serial_getter=lambda _p: SERIAL
    )
    assert isinstance(result, str)
    assert "验证未通过" in result
    assert len(asker.hints) == 4  # 首发 1 + 错误重试 3


def test_gate_clone_drive_with_code_retries_then_fails(pw_drive: tuple[Path, bytes, str]) -> None:
    """克隆盘 + 输了正确码: 码只对授权盘有效, 重试耗尽后失败。"""
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    drive, seed, code = pw_drive
    asker = _FakeAsker([code, code, code, code])
    result = open_manifest_with_code(
        drive, seed, asker, max_tries=3, serial_getter=lambda _p: 0xAABBCCDD
    )
    assert isinstance(result, str)
    assert "验证未通过" in result


def test_gate_non_vault_drive(pw_drive: tuple[Path, bytes, str], tmp_path: Path) -> None:
    from src.app.usb_vault.viewer.app import open_manifest_with_code

    empty = tmp_path / "空盘"
    empty.mkdir()
    _drive, seed, _code = pw_drive
    result = open_manifest_with_code(empty, seed, _FakeAsker([]), serial_getter=lambda _p: SERIAL)
    assert isinstance(result, str)
    assert "不是资料盘" in result


# ------------------------------------------------------- 复制封堵事件序列


def test_copy_swallow_seqs_valid() -> None:
    """回归 (2026-09-14 真机打包冒烟): 虚拟事件写成单尖括号 "<Copy>" 在
    Windows Tk 是非法 keysym, bind_all 直接 TclError, 启动门禁连错误
    对话框都被炸掉 (进程活着但窗口是 PyInstaller 的未处理异常框)。
    """
    import re

    for seq in (*renderers.COPY_SWALLOW_SEQS, *renderers.COPY_SWALLOW_SEQS_MAC):
        # 单尖括号内恰好一个驼峰词 = 误写的虚拟事件 (合法 keysym 均含连字符)
        assert not re.fullmatch(r"<[A-Z][a-z]+>", seq), f"疑似误写虚拟事件: {seq}"
    assert "<<Copy>>" in renderers.COPY_SWALLOW_SEQS


# ----------------------------------------------- macOS 双入口数据卷扫描 (M4)
# 双入口布局: .app 在分区2 (无载体), 载体在分区1 —— 启动门禁先扫兄弟卷找回数据盘


def _burn_volume(drive: Path, *, serial: int = SERIAL, use_password: bool = False) -> bytes:
    """往 drive 烧一个最小资料盘, 返回 seed (供候选仲裁测试探解)。"""
    import os

    from src.app.usb_vault.burner import BurnOptions, burn

    src = drive.parent / f"{drive.name}_src"
    src.mkdir()
    (src / "a.txt").write_text("A", encoding="utf-8")
    viewer = drive.parent / f"{drive.name}_v.exe"
    viewer.write_bytes(b"X")
    seed = os.urandom(64)
    burn(
        src,
        drive,
        seed,
        BurnOptions(
            require_root=False,
            viewer_exe=viewer,
            convert_office=False,
            skip_scan=True,
            use_password=use_password,
        ),
        serial_getter=lambda _p: serial,
    )
    return seed


def test_mac_candidates_own_with_keyinfo_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单分区向后兼容: 自身卷有载体时排第一, 兄弟卷按名序补位。"""
    from src.app.usb_vault.viewer import app as viewer_app

    volumes = tmp_path / "Volumes"
    own = volumes / "资料"
    other = volumes / "图书阅览器Mac"
    own.mkdir(parents=True)
    other.mkdir()
    (own / config.KEYINFO_NAME).write_bytes(b"x")
    (other / config.KEYINFO_NAME).write_bytes(b"x")
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)
    assert viewer_app._mac_data_volume_candidates(own) == [own, other]


def test_mac_candidates_dual_p2_scans_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分区2 (自身无载体) 场景: 只挑出含 keyinfo 的兄弟卷, 无关卷排除。"""
    from src.app.usb_vault.viewer import app as viewer_app

    volumes = tmp_path / "Volumes"
    data = volumes / "资料"
    p2 = volumes / "图书阅览器Mac"
    noise = volumes / "Backup"
    data.mkdir(parents=True)
    p2.mkdir()
    noise.mkdir()
    (data / config.KEYINFO_NAME).write_bytes(b"x")
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)
    assert viewer_app._mac_data_volume_candidates(p2) == [data]


def test_mac_candidates_root_missing_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """/Volumes 不存在 (或不可读): 吞 OSError 退化空候选, 不炸门禁。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", tmp_path / "不存在")
    assert viewer_app._mac_data_volume_candidates(tmp_path / "X") == []


def test_resolve_data_drive_hits_second_sibling_by_gcm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """双兄弟卷仲裁: 命中的是 seed 对口的那个, 不是排在前面的那个。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")  # 候选源走 mac 分支 (仲裁平台无关)
    volumes = tmp_path / "Volumes"
    va = volumes / "资料A"
    vb = volumes / "资料B"  # 名序: A 在前, 仲裁必须穿透到 B
    va.mkdir(parents=True)
    vb.mkdir()
    seed_a = _burn_volume(va)
    seed_b = _burn_volume(vb)
    p2 = volumes / "图书阅览器Mac"
    p2.mkdir()
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)
    assert (
        viewer_app.ViewerApp._resolve_data_drive(p2, seed_b, serial_getter=lambda _p: SERIAL) == vb
    )
    assert (
        viewer_app.ViewerApp._resolve_data_drive(p2, seed_a, serial_getter=lambda _p: SERIAL) == va
    )


def test_resolve_data_drive_single_partition_own_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单分区盘: 自身就是数据盘, 解析后仍是自身 (零行为变化)。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")  # 候选源走 mac 分支 (仲裁平台无关)
    volumes = tmp_path / "Volumes"
    own = volumes / "资料"
    own.mkdir(parents=True)
    seed = _burn_volume(own)
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", tmp_path / "不存在")
    assert (
        viewer_app.ViewerApp._resolve_data_drive(own, seed, serial_getter=lambda _p: SERIAL) == own
    )


def test_resolve_data_drive_password_required_is_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """密码盘候选: 试解立即要求解锁码 —— 等码也是命中, 交给输码流程。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")  # 候选源走 mac 分支 (仲裁平台无关)
    volumes = tmp_path / "Volumes"
    data = volumes / "资料"
    data.mkdir(parents=True)
    seed = _burn_volume(data, use_password=True)
    p2 = volumes / "图书阅览器Mac"
    p2.mkdir()
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)
    assert (
        viewer_app.ViewerApp._resolve_data_drive(p2, seed, serial_getter=lambda _p: SERIAL) == data
    )


def test_resolve_data_drive_volumeid_candidate_preferred_over_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全败但有 VolumeIdError 候选 (macOS 权限墙): 返回它接管理员授权恢复。"""
    from src.app.usb_vault.viewer import app as viewer_app
    from src.app.usb_vault.volume_id import VolumeIdError

    monkeypatch.setattr(sys, "platform", "darwin")  # 候选源走 mac 分支 (仲裁平台无关)
    _deny_all_serial_channels(monkeypatch)
    volumes = tmp_path / "Volumes"
    data = volumes / "资料"
    data.mkdir(parents=True)
    seed = _burn_volume(data)
    p2 = volumes / "图书阅览器Mac"
    p2.mkdir()
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)

    def denied(_p: Path) -> int:
        raise VolumeIdError("读取U盘引导扇区权限不足")

    assert viewer_app.ViewerApp._resolve_data_drive(p2, seed, serial_getter=denied) == data


def test_resolve_data_drive_all_fail_returns_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选全是假载体 (keyinfo 存在但解不开): 诚实返回 own, 门禁照常报错。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")  # 候选源走 mac 分支 (仲裁平台无关)
    volumes = tmp_path / "Volumes"
    fake = volumes / "假盘"
    fake.mkdir(parents=True)
    (fake / config.KEYINFO_NAME).write_bytes(b"FAKE")
    p2 = volumes / "图书阅览器Mac"
    p2.mkdir()
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)
    assert (
        viewer_app.ViewerApp._resolve_data_drive(p2, b"S" * 64, serial_getter=lambda _p: SERIAL)
        == p2
    )


def test_resolve_data_drive_detached_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阅览器被拷离配套U盘 (全机无任何 keyinfo 载体): 返回 None, 门禁给
    "脱离U盘介质"文案 —— 不再拿自身卷硬开清单弹"该盘不是资料盘"。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "platform", "darwin")  # 候选源走 mac 分支 (仲裁平台无关)
    volumes = tmp_path / "Volumes"
    noise = volumes / "Macintosh HD"  # 本机自己的卷: 与阅览器无关
    noise.mkdir(parents=True)
    # 拷到本机硬盘的 .app (如 ~/Applications): 自身目录无载体
    copied = tmp_path / "Applications" / "图书阅览器(MAC).app"
    copied.mkdir(parents=True)
    monkeypatch.setattr(viewer_app, "MAC_VOLUMES_ROOT", volumes)
    assert viewer_app.ViewerApp._resolve_data_drive(copied, b"S" * 64) is None


def test_detached_message_text_verbatim() -> None:
    """用户定稿文案原文锁定 (v2.4.4): 一字不改, 防后续维护顺手润色。"""
    from src.app.usb_vault.viewer.app import DETACHED_MESSAGE

    assert DETACHED_MESSAGE == "脱离U盘介质，加密书稿无法在该设备上打开查看"


# ------------------ v2.4.8 拷离介质闸门 (2026-09-29 用户实测曝光 v2.4.4 边界) --
# 整目录拷走阅览器到电脑后双击, 原盘同机插着时全机卷扫描仍找到加密链开门;
# 新判据只看"自己运行在哪": 冻结构建不在可移动介质上 = 一律谢幕。


def test_media_gate_dev_source_run_always_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源码运行 (无 sys.frozen) = 烧录机 view 预览, 在固定盘上也不设卡。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert viewer_app._exe_on_removable_media() is True


def test_media_gate_windows_fixed_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(viewer_app, "get_drive_type", lambda _p: "fixed")
    assert viewer_app._exe_on_removable_media() is False


def test_media_gate_windows_removable_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(viewer_app, "get_drive_type", lambda _p: "removable")
    assert viewer_app._exe_on_removable_media() is True


def test_media_gate_windows_read_failure_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GetDriveType 读不出 (卷不在线等) = fail-closed 谢幕, 不猜。"""
    from src.app.usb_vault.viewer import app as viewer_app
    from src.app.usb_vault.volume_id import VolumeIdError

    def boom(_p: str) -> str:
        raise VolumeIdError("读不出")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(viewer_app, "get_drive_type", boom)
    assert viewer_app._exe_on_removable_media() is False


def test_media_gate_darwin_external_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.usb_vault import mac_volume
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mac_volume, "is_on_external_media", lambda _p: True)
    assert viewer_app._exe_on_removable_media() is True


def test_media_gate_darwin_internal_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.app.usb_vault import mac_volume
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(mac_volume, "is_on_external_media", lambda _p: False)
    assert viewer_app._exe_on_removable_media() is False


def test_media_gate_unknown_platform_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不存在的冻结构建平台 (linux 等): 保守拒绝, 不放行。"""
    from src.app.usb_vault.viewer import app as viewer_app

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert viewer_app._exe_on_removable_media() is False


def test_startup_gate_media_gate_fires_before_scan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """整目录拷贝件 (固定盘 + 原盘在场): 门禁最先谢幕, 不进种子装载与卷扫描。"""
    from src.app.usb_vault.viewer import app as viewer_app

    app = object.__new__(viewer_app.ViewerApp)  # 绕过 __init__, 不建 Tk
    app.drive = tmp_path
    monkeypatch.setattr(viewer_app, "_exe_on_removable_media", lambda: False)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("介质闸门应先于种子装载与全机卷扫描")

    monkeypatch.setattr(viewer_app, "load_embedded_seed", boom)
    assert viewer_app.ViewerApp._startup_gate(app) == viewer_app.DETACHED_MESSAGE


def test_startup_gate_media_pass_proceeds_to_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """顺序钉: 介质判据通过后才进入种子装载 (闸门在门禁链最前)。"""
    from src.app.usb_vault.seed import SeedError
    from src.app.usb_vault.viewer import app as viewer_app

    app = object.__new__(viewer_app.ViewerApp)
    app.drive = tmp_path
    monkeypatch.setattr(viewer_app, "_exe_on_removable_media", lambda: True)

    def boom() -> bytes:
        raise SeedError("种子缺失")

    monkeypatch.setattr(viewer_app, "load_embedded_seed", boom)
    fail = viewer_app.ViewerApp._startup_gate(app)
    assert fail is not None and "程序组件缺失" in fail


# ------------------------------------------ 窗口尺寸 / PDF 翻页 (2026-09-17 UI 优化)


def test_default_geometry_screen_proportional() -> None:
    """默认窗口 = 屏幕的 3/4 且居中 (2026-09-19 收件人反馈: 随屏幕自适配,
    大屏自动放大, 小屏自动缩小, 不再固定 1536x1020)。"""
    from src.app.usb_vault.viewer.app import default_geometry

    assert default_geometry(2560, 1440) == "1920x1080+320+160"


def test_default_geometry_shrinks_with_small_screen() -> None:
    """小屏按比例缩小: 1080p → 3/4 屏; 1366x768 笔记本同样 3/4。"""
    from src.app.usb_vault.viewer.app import default_geometry

    assert default_geometry(1920, 1080) == "1440x810+240+115"
    assert default_geometry(1366, 768) == "1024x576+171+76"


def test_default_geometry_floor_on_tiny_screen() -> None:
    """极小屏落 640x480 地板值, 且坐标夹在 0 不为负 (窗口不出屏)。"""
    from src.app.usb_vault.viewer.app import default_geometry

    assert default_geometry(600, 400) == "640x480+0+0"


def test_pdf_page_step_moves_and_blocks() -> None:
    assert renderers.pdf_page_step(0, 10, 1) == (1, True)
    assert renderers.pdf_page_step(5, 10, -1) == (4, True)
    assert renderers.pdf_page_step(0, 10, -1) == (0, False)  # 首页禁退
    assert renderers.pdf_page_step(9, 10, 1) == (9, False)  # 末页禁进


def test_pdf_page_step_single_page_never_moves() -> None:
    assert renderers.pdf_page_step(0, 1, 1) == (0, False)
    assert renderers.pdf_page_step(0, 1, -1) == (0, False)


# ------------------------------------------ 页码跳转 (2026-09-23 用户需求)


def test_pdf_page_jump_parses_and_validates() -> None:
    assert renderers.pdf_page_jump("5", 10) == 4  # 1-based 输入 → 0-based 页号
    assert renderers.pdf_page_jump(" 7 ", 10) == 6  # 容忍两端空白
    assert renderers.pdf_page_jump("１２", 70) == 11  # 全角数字 (中文输入法常态)
    assert renderers.pdf_page_jump("10", 10) == 9  # 末页有效
    assert renderers.pdf_page_jump("1", 1) == 0  # 单页文档首页有效


def test_pdf_page_jump_rejects_invalid_without_guessing() -> None:
    """越界不夹不猜: 输 999 想跳末页与手误无法区分, 静默夹末页 = 看似
    成功的错误页。空串/非数字/0 (页码从 1 起) 同样只给 None 由 UI 提示。"""
    assert renderers.pdf_page_jump("", 10) is None
    assert renderers.pdf_page_jump("abc", 10) is None
    assert renderers.pdf_page_jump("5页", 10) is None  # 带汉字不是纯数字
    assert renderers.pdf_page_jump("0", 10) is None
    assert renderers.pdf_page_jump("11", 10) is None
    assert renderers.pdf_page_jump("999", 10) is None
    assert renderers.pdf_page_jump("2", 1) is None  # 单页文档无第 2 页


# ------------------------------------------ 连续缩放 (2026-09-18 收件人反馈)


def test_zoom_clamp_bounds() -> None:
    assert renderers.zoom_clamp(100) == 100
    assert renderers.zoom_clamp(1) == config.ZOOM_MIN_PCT
    assert renderers.zoom_clamp(9999) == config.ZOOM_MAX_PCT


def test_zoom_step_moves_and_blocks() -> None:
    """受限步进与 pdf_page_step 同型: 边界处静默不动 (是否移动=调用方渲染依据)。"""
    assert renderers.zoom_step(100, 10) == (110, True)
    assert renderers.zoom_step(100, -10) == (90, True)
    assert renderers.zoom_step(config.ZOOM_MIN_PCT, -10) == (config.ZOOM_MIN_PCT, False)
    assert renderers.zoom_step(config.ZOOM_MAX_PCT, 10) == (config.ZOOM_MAX_PCT, False)
    # 步进被夹到边界时算"移动" (值变了), 调用方照常重渲染一次
    assert renderers.zoom_step(config.ZOOM_MAX_PCT - 3, 10) == (config.ZOOM_MAX_PCT, True)


def test_dpi_for_pct_tracks_zoom() -> None:
    assert renderers.dpi_for_pct(100) == config.PDF_BASE_DPI
    assert renderers.dpi_for_pct(200) == config.PDF_BASE_DPI * 2
    assert renderers.dpi_for_pct(25) == round(config.PDF_BASE_DPI / 4)


def test_dpi_for_pct_is_int_for_pymupdf() -> None:
    """回归 (2026-09-18 v2.3.0 "打开的内容为空" 事故): PyMuPDF get_pixmap(dpi=)
    走 SWIG fz_pixmap_xres_set 只收 int, 传 float 直接 TypeError; 且异常炸在
    after 回调里被 tkinter 静默吞掉 (--windowed 产物 stderr=None), 收件人端
    表现为零症状空白页。dpi_for_pct 对任意档位必须返回 int。"""
    for pct in (25, 100, 137, 235, 400):
        assert isinstance(renderers.dpi_for_pct(pct), int)


def test_fit_pixels_budget_cap() -> None:
    """预算内原样通过; 超预算压回且不低于下限 (宁可少放大, 不炸内存)。"""
    # A4 @ 100% ≈ 794×1123 ≈ 0.89M px, 400% → 14.3M px < 36M: 不压
    assert renderers.fit_pixels(794, 1123, 400) == 400
    assert renderers.fit_pixels(794, 1123, 100) == 100
    # 巨页 2000×3000 @ 400% = 96M px > 36M: 压到 ~211% (36M/6M 开方)
    capped = renderers.fit_pixels(2000, 3000, 400)
    assert config.ZOOM_MIN_PCT <= capped < 400
    assert 2000 * 3000 * capped * capped <= config.RENDER_MAX_PIXELS * 10_000
    # 用户要的比例本来就低于上限时, 不放大、不越权
    assert renderers.fit_pixels(2000, 3000, 100) == 100


def test_zoom_anchor_keeps_cursor_content() -> None:
    """锚定缩放: 光标处的内容点缩放后仍落在光标下。"""
    # 视口左上 (view=0) + 光标在视口左上 (frac=0): 偏移仍为 0
    assert renderers.zoom_anchor(0.0, 0.0, 2.0) == 0.0
    # 光标在内容原点上 (frac=0), 视口有偏移: 新偏移 = view/ratio
    assert renderers.zoom_anchor(0.5, 0.0, 2.0) == pytest.approx(0.25)
    # 光标在视口正中 (frac=0.5), 视口贴内容顶 (view=0): 放大 2 倍后
    # 新视口正好让原中心内容居于中心 → 0.25
    assert renderers.zoom_anchor(0.0, 0.5, 2.0) == pytest.approx(0.25)
    # 恒等缩放不动
    assert renderers.zoom_anchor(0.3, 0.7, 1.0) == pytest.approx(0.3)
    # 越界夹回 [0, 1]
    assert renderers.zoom_anchor(0.9, 1.0, 2.0) == 0.95
    assert renderers.zoom_anchor(0.0, 0.0, 0.5) == 0.0


# ------------------------------------------ 后台渲染调度 (2026-09-19 大书卡顿根治)


def test_render_scheduler_latest_wins() -> None:
    """取活时丢弃积压旧帧: 渲染中又提交的新请求, 只有最新一帧会被执行。

    大书连续缩放的真实场景 —— 旧帧渲染未完, 新缩放已排队; 不丢弃就会
    串行渲染每一档 = UI 冻结复现 (这正是 v2.3.1 同步渲染"未响应"的根因)。
    """
    import threading
    import time as _time

    started = threading.Event()
    release = threading.Event()

    def slow() -> object:
        started.set()
        assert release.wait(timeout=5), "测试死等: release 未置位"
        return "slow"

    s = renderers.RenderScheduler()
    s.submit(1, slow, lambda _r: None)
    assert started.wait(timeout=5), "测试死等: 工作线程未取活"
    s.submit(2, lambda: "stale", lambda _r: None)  # 渲染中积压
    s.submit(3, lambda: "latest", lambda _r: None)
    release.set()

    got: list[tuple[int, object]] = []
    deadline = _time.monotonic() + 5
    while len(got) < 2 and _time.monotonic() < deadline:
        reply = s.take_reply()
        if reply is None:
            _time.sleep(0.005)
            continue
        seq, _apply, result = reply
        got.append((seq, result))
    s.close()
    # seq=1 在取活后才积压, 照常完成; seq=2 被"只留最新"丢弃; seq=3 执行
    assert got == [(1, "slow"), (3, "latest")]


def test_render_scheduler_failure_surfaces() -> None:
    """work 侧异常必须封进回复回主线程显形, 不在工作线程里蒸发。"""
    import time as _time

    def boom() -> object:
        raise ValueError("render boom")

    s = renderers.RenderScheduler()
    s.submit(1, boom, lambda _r: None)
    reply = None
    deadline = _time.monotonic() + 5
    while reply is None and _time.monotonic() < deadline:
        reply = s.take_reply()
        if reply is None:
            _time.sleep(0.005)
    s.close()
    assert reply is not None
    _seq, _apply, result = reply
    assert isinstance(result, renderers._RenderFailure)
    assert str(result.exc) == "render boom"


def test_pdf_page_label_marks_first_last() -> None:
    """首/末页必须有可见标注 (2026-09-17 真机反馈: 到最后一页毫无提示)。"""
    assert "首页" in renderers.pdf_page_label(0, 10)
    assert "最后一页" not in renderers.pdf_page_label(0, 10)
    assert "最后一页" in renderers.pdf_page_label(9, 10)
    assert "首页" not in renderers.pdf_page_label(9, 10)
    mid = renderers.pdf_page_label(4, 10)
    assert "首页" not in mid and "最后一页" not in mid
    assert "第 10/10 页" in renderers.pdf_page_label(9, 10)
    assert "DPI" not in mid  # 2026-09-17 反馈: 页码栏不重复渲染参数


def test_pdf_page_label_single_page_marks_both() -> None:
    label = renderers.pdf_page_label(0, 1)
    assert "首页" in label and "最后一页" in label
    assert "DPI" not in label


# ------------------------------------------ 页面缩略图 (v2.4.2)


def test_thumb_dpi_int_and_proportional() -> None:
    """DPI 必须 int (SWIG 红线), 且输出宽恒 ≈ 目标宽 (页宽反比自洽)。"""
    for page_w in (595, 842, 100, 2000):
        dpi = renderers.thumb_dpi(page_w)
        assert isinstance(dpi, int)
        out_w = round(page_w / 72 * dpi)
        assert 0.9 * config.THUMB_WIDTH_PX <= out_w <= 1.2 * config.THUMB_WIDTH_PX
    assert renderers.thumb_dpi(100) > renderers.thumb_dpi(842)  # 小页高 DPI
    assert renderers.thumb_dpi(1) >= 1
    assert renderers.thumb_dpi(100_000) == 1  # 巨页夹到地板 1
    # v2.4.3: 高分屏面板按 ui_scale 传更大目标宽, DPI 同步等比
    for target in (120, 240, 360):
        dpi = renderers.thumb_dpi(595, target_w=target)
        assert isinstance(dpi, int)
        out_w = round(595 / 72 * dpi)
        assert 0.9 * target <= out_w <= 1.2 * target


def test_thumb_fit_keeps_aspect_inside_box() -> None:
    """等比进槽: 不溢出、保比例、退化入参不炸 (最少 1px)。"""
    assert renderers.thumb_fit(595, 842, 120, 170) == (120, 170)
    wide = renderers.thumb_fit(842, 595, 120, 170)  # 横排页: 高度让位
    assert wide[0] == 120 and wide[1] < 170
    tall = renderers.thumb_fit(100, 1000, 120, 170)  # 长条页: 宽度让位
    assert tall[1] == 170 and tall[0] < 100
    assert renderers.thumb_fit(0, 100, 120, 170) == (1, 1)
    assert renderers.thumb_fit(120, 170, 0, 0) == (1, 1)


def test_thumb_visible_range_boundaries() -> None:
    """可视区与槽位相交判定: 槽高 200 + 间距 10 (步进 210)。"""
    assert renderers.thumb_visible_range(0, 300, 200, 10, 700) == (0, 1)
    # 首槽 [0,200) 刚滚出视口顶 (y0=200): 从第 2 槽起
    assert renderers.thumb_visible_range(200, 300, 200, 10, 700) == (1, 1)
    assert renderers.thumb_visible_range(0, 100_000, 200, 10, 5) == (0, 4)
    assert renderers.thumb_visible_range(0, 300, 200, 10, 0) == (0, -1)
    assert renderers.thumb_visible_range(5, 5, 200, 10, 5) == (0, -1)  # 空区间


def test_thumb_request_range_clamps_margin() -> None:
    assert renderers.thumb_request_range(10, 20, 700, 4) == (6, 24)
    assert renderers.thumb_request_range(0, 3, 5, 4) == (0, 4)
    # 布局未就绪 (0,-1) 入参: 退化为首页邻域, 面板初建先有第一屏
    assert renderers.thumb_request_range(0, -1, 5, 2) == (0, 1)
    assert renderers.thumb_request_range(0, 3, 0, 4) == (0, -1)


def test_thumb_evict_far_first_and_protects_visible() -> None:
    """超限淘汰: 离保护带最远优先; 保护带内一张不丢。"""
    cached = list(range(1, 601))  # 600 张, 上限 500 → 淘 100
    drop = renderers.thumb_evict_keys(cached, 100, 110, 500)
    assert len(drop) == 100
    assert set(range(100, 111)).isdisjoint(drop)  # 保护带永不淘汰
    assert 600 in drop and 599 in drop  # 最远 (带上沿之外)
    # 可视带偏文档尾部: 上沿距离 (391~490) 压过下沿全部 (≤99), cutoff 全在上侧
    assert drop[0] == 600  # 列表序 = 淘汰优先级 (最远在前)
    assert sorted(drop) == list(range(501, 601))
    assert 1 not in drop and 99 not in drop  # 带下沿的近页保留
    assert renderers.thumb_evict_keys(cached, 100, 110, 600) == []  # 未超限
    # 保护带大于可淘汰集: 全给 (缓存临时超限好过闪屏)
    tight = renderers.thumb_evict_keys([1, 2, 3, 4], 1, 4, 2)
    assert tight == []


# ------------------------------------------ 高分屏 DPI 缩放 (v2.4.3)


def test_dpi_scale_windows_ratio_and_clamps() -> None:
    """Surface 200% → 系数 2.0; 异常低不缩小, 异常高封顶 3.0。"""
    assert renderers.dpi_scale(96.0, win32=True) == 1.0
    assert renderers.dpi_scale(144.0, win32=True) == 1.5
    assert renderers.dpi_scale(192.0, win32=True) == 2.0
    assert renderers.dpi_scale(48.0, win32=True) == 1.0
    assert renderers.dpi_scale(400.0, win32=True) == 3.0


def test_dpi_scale_mac_logical_points_neutral() -> None:
    """Aqua 基线 72 逻辑点: 标准屏恒 1.0; 偶发 96 也不失控。"""
    assert renderers.dpi_scale(72.0, win32=False) == 1.0
    assert renderers.dpi_scale(96.0, win32=False) == pytest.approx(4 / 3)


def test_nav_name_width_adaptive_with_clamps() -> None:
    """左栏名宽: 短名落下限/超长名落上限/常规=实测+余量, 全随 scale 等比。"""
    assert renderers.nav_name_width(80, 1.0) == 128
    assert renderers.nav_name_width(4000, 1.0) == 320
    assert renderers.nav_name_width(200, 1.0) == 224
    assert renderers.nav_name_width(80, 2.0) == 256
    assert renderers.nav_name_width(4000, 2.0) == 640


def test_thumb_cache_cap_scales_by_area() -> None:
    """缩略图位图面积 ∝ scale²: 上限 ÷ scale² 压回, 地板 120 保一屏+预取。"""
    assert renderers.thumb_cache_cap(500, 1.0) == 500
    assert renderers.thumb_cache_cap(500, 1.5) == 222
    assert renderers.thumb_cache_cap(500, 2.0) == 125
    assert renderers.thumb_cache_cap(500, 3.0) == 120


def test_render_scheduler_background_fifo_and_priority() -> None:
    """后台通道 (缩略图): FIFO 全保留不丢帧, 且恒让路前台最新帧。"""
    import threading
    import time as _time

    started = threading.Event()
    release = threading.Event()

    def blocker() -> object:
        started.set()
        assert release.wait(timeout=5), "测试死等: release 未置位"
        return "block"

    s = renderers.RenderScheduler()
    s.submit(1, blocker, lambda _r: None)
    assert started.wait(timeout=5), "测试死等: 工作线程未取活"
    # 前台帧在途时: 后台排 2 帧, 前台再积压 1 帧更新
    s.submit_background(lambda: "b1", lambda _r: None)
    s.submit_background(lambda: "b2", lambda _r: None)
    s.submit(2, lambda: "latest", lambda _r: None)
    release.set()

    got: list[tuple[int, object]] = []
    deadline = _time.monotonic() + 5
    while len(got) < 4 and _time.monotonic() < deadline:
        reply = s.take_reply()
        if reply is None:
            _time.sleep(0.005)
            continue
        seq, _apply, result = reply
        got.append((seq, result))
    s.close()
    # 前台帧 2 先于后台落地 (优先级); b1/b2 按序全保留 (不丢);
    # 后台回复 seq 恒 -1 (不参与前台最新者胜比对)
    assert got == [(1, "block"), (2, "latest"), (-1, "b1"), (-1, "b2")]


# ------------------------------------------------------------------ 装载计时探针 (Phase 0)


def test_loadtimer_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(loadtimer.ENV_NAME, raising=False)
    assert loadtimer.enabled() is False


def test_loadtimer_env_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(loadtimer.ENV_NAME, "0")
    assert loadtimer.enabled() is False
    monkeypatch.setenv(loadtimer.ENV_NAME, "1")
    assert loadtimer.enabled() is True


def test_stage_timer_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 1.0, 2.5])
    # patch 全局 time 模块 (loadtimer 同一对象); 经 loadtimer.time 访问会被
    # mypy --strict 的 no-implicit-reexport 拒掉
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks))
    st = loadtimer.StageTimer()
    st.begin()
    st.mark("读盘")
    st.mark("解密")
    assert st.line() == "读盘 1.00s | 解密 1.50s | 合计 2.50s"


def test_stage_timer_needs_two_points() -> None:
    # 没打任何 mark (或没 begin) = 无可计段, 空串而非半截输出
    st = loadtimer.StageTimer()
    assert st.line() == ""
    st.begin()
    assert st.line() == ""
    st.mark("唯一段")
    assert st.line() == "唯一段 0.00s | 合计 0.00s"


def test_timed_reader_accounting(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 2.0, 10.0, 10.5])
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks))
    r = loadtimer.TimedReader(io.BytesIO(b"abcdef"))
    assert r.read(3) == b"abc"
    assert r.read() == b"def"
    assert r.read_s == 2.5
    assert r.read_bytes == 6
    assert r.mbps() == pytest.approx(6 / 1048576 / 2.5)


def test_timed_reader_mbps_zero_division_safe() -> None:
    # 一个字节没读过 (或时钟零走时): 吞吐 0.0, 不 ZeroDivisionError
    r = loadtimer.TimedReader(io.BytesIO(b""))
    assert r.mbps() == 0.0


def test_log_sink_history_and_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(loadtimer.ENV_NAME, raising=False)
    seen: list[str] = []
    loadtimer.set_sink(seen.append)
    try:
        loadtimer.log("行1")
        assert seen == ["行1"]
        assert loadtimer.history()[-1] == "行1"  # 只验尾行: 历史跨用例累积
        assert "[loadtimer] 行1" in capsys.readouterr().err
    finally:
        loadtimer.set_sink(None)  # 不泄漏给后续用例


def test_timed_reader_passthrough_context() -> None:
    # with 语义转交内层句柄: 正常路径关闭底层文件 (BytesIO.closed 可观测)
    inner = io.BytesIO(b"xy")
    with loadtimer.TimedReader(inner) as r:
        assert r.read(1) == b"x"
    assert inner.closed is True
