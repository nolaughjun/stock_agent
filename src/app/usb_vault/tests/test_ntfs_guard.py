"""ntfs_guard 单测 (2026-09-28 NTFS 交付形态): 真 ACL 断言, 不 mock 内核。

全部用例在 Windows 真文件系统上跑 (tmp_path 落在 NTFS 卷, icacls/ACL API
真执行) —— 防误删是内核行为, mock 掉就什么都没验。非 Windows 平台整文件跳过
(Mac CI 端 fs_name 恒 "", 施加逻辑自然缺席, 由 test_burner/test_burner_dual
的跨平台用例覆盖缺席分支)。

配方与机制断言与 2026-09-28 探针矩阵 (ntfs_guard 模块 docstring) 一一对应:
- ship 配方两级都拒 → 删除被拒, 读取/复制不受影响
- 半套配方 (只拒文件) → 父目录通道照删 (两级必要性的回归锚)
- 树根继承 ACE → 后落盘文件自动被罩
- 整树 /t 清障对继承 ACE 有效 (单文件 /remove:d 会谎报成功的坑)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from src.app.usb_vault import config
from src.app.usb_vault import ntfs_guard
from src.app.usb_vault.burner import (
    _ALL_EXE_NAMES,
    _ALL_VIEWER_DIR_NAMES,
    _apply_delete_denial,
    _clear_own_artifacts,
    _fs_not_ntfs,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="NTFS deny ACL 仅 Windows 真文件系统可断言"
)


@pytest.fixture()
def ship_base(tmp_path: Path) -> Path:
    """探针 ship 布局微缩: 分区根 + 交付树 + 树下文件 + 根下散文件。"""
    base = tmp_path / "part"
    ship = base / config.DATA_DIR_NAME
    ship.mkdir(parents=True)
    (ship / "book.enc").write_bytes(b"cipher")
    (base / config.KEYINFO_NAME).write_bytes(b"keyinfo")
    return base


def _apply_ship_recipe(base: Path) -> None:
    ntfs_guard.deny_delete_children(base)
    ntfs_guard.deny_delete_tree(base / config.DATA_DIR_NAME)
    ntfs_guard.deny_delete_file(base / config.KEYINFO_NAME)


def test_tmp_volume_is_ntfs(tmp_path: Path) -> None:
    """前置自证: 本组用例所在卷确为 NTFS (否则下面全部断言无意义)。"""
    assert ntfs_guard.is_ntfs(tmp_path)


def test_ship_recipe_blocks_delete_and_keeps_read(ship_base: Path, tmp_path: Path) -> None:
    _apply_ship_recipe(ship_base)
    enc = ship_base / config.DATA_DIR_NAME / "book.enc"
    keyinfo = ship_base / config.KEYINFO_NAME
    # 树内文件 (继承来的 DE) 与根下散文件 (显式 DE): 文件通道删被拒
    with pytest.raises(PermissionError):
        os.remove(enc)
    with pytest.raises(PermissionError):
        os.remove(keyinfo)
    # 整树删 (父目录 DC 通道): rmtree 同样被拒
    with pytest.raises(PermissionError):
        shutil.rmtree(ship_base / config.DATA_DIR_NAME)
    # 读取/复制不受影响 (deny 只含 DE/DC, 不碰读) —— 收件人正常阅读的前提
    assert enc.read_bytes() == b"cipher"
    outside = tmp_path / "copy.enc"
    shutil.copy2(enc, outside)
    assert outside.read_bytes() == b"cipher"


def test_half_measure_falls_to_parent_channel(tmp_path: Path) -> None:
    """只拒文件不拒父目录 (半套配方) → 父目录 FILE_DELETE_CHILD 通道照删。

    2026-09-28 探针矩阵核心结论的回归锚: 两级都拒不是锦上添花, 是充要。"""
    clean_parent = tmp_path / "clean"  # 无任何 deny 的父目录
    clean_parent.mkdir()
    f = clean_parent / "only-file-de.bin"
    f.write_bytes(b"x")
    ntfs_guard.deny_delete_file(f)
    assert ntfs_guard.has_delete_deny(f)
    os.remove(f)  # 父目录通道放行 → 删得掉 (半套防线形同虚设)
    assert not f.exists()


def test_tree_deny_shields_files_created_after(ship_base: Path) -> None:
    """树根继承 ACE: 施加之后才落盘的文件也被自动罩住 (重烧补文件的场景)。"""
    ntfs_guard.deny_delete_tree(ship_base / config.DATA_DIR_NAME)
    later = ship_base / config.DATA_DIR_NAME / "later.enc"
    later.write_bytes(b"late")
    assert ntfs_guard.has_delete_deny(later)
    with pytest.raises(PermissionError):
        os.remove(later)


def test_predicates_before_and_after(ship_base: Path) -> None:
    tree = ship_base / config.DATA_DIR_NAME
    loose = ship_base / config.KEYINFO_NAME
    assert not ntfs_guard.has_child_deny(ship_base)
    assert not ntfs_guard.tree_deny_present(tree)
    assert not ntfs_guard.has_delete_deny(loose)
    _apply_ship_recipe(ship_base)
    assert ntfs_guard.has_child_deny(ship_base)
    assert ntfs_guard.tree_deny_present(tree)
    assert ntfs_guard.has_delete_deny(loose)


def test_apply_is_idempotent(ship_base: Path) -> None:
    """重复施加跳过 (lock 补锁/烧录重跑场景), deny 条目不翻倍。"""
    _apply_ship_recipe(ship_base)
    assert ntfs_guard.deny_delete_children(ship_base) is False
    assert ntfs_guard.deny_delete_tree(ship_base / config.DATA_DIR_NAME) is False
    assert ntfs_guard.deny_delete_file(ship_base / config.KEYINFO_NAME) is False
    assert len(ntfs_guard.deny_rights(ship_base)) == 1


def test_strip_whole_tree_removes_inherited_aces(ship_base: Path) -> None:
    """整树 /t 清障: 继承来的 deny 一并收回 (单文件 /remove:d 谎报成功的坑)。

    重烧路径 (_clear_own_artifacts) 的清障依据。"""
    _apply_ship_recipe(ship_base)
    enc = ship_base / config.DATA_DIR_NAME / "book.enc"
    assert ntfs_guard.has_delete_deny(enc)  # 继承副本在场
    ntfs_guard.strip_delete_denial(ship_base)
    assert ntfs_guard.deny_rights(ship_base) == []
    assert ntfs_guard.deny_rights(enc) == []  # 继承副本随根撤回
    os.remove(enc)  # 清障后可删
    assert not enc.exists()


def test_strip_single_file_removes_explicit_deny(ship_base: Path) -> None:
    _apply_ship_recipe(ship_base)
    keyinfo = ship_base / config.KEYINFO_NAME
    ntfs_guard.strip_delete_denial(keyinfo)  # 文件: 显式 deny 直接摘
    assert ntfs_guard.deny_rights(keyinfo) == []
    os.remove(keyinfo)


def test_strip_root_non_recursive_keeps_tree_and_file_denies(ship_base: Path) -> None:
    """分区根清障只摘根上 (DC), 不递归 (2026-09-29 真机事故回归)。

    从分区根整树 /t 会踩进 System Volume Information (管理员亦无权,
    icacls rc=5, F: 真棒全盘重建即栽此); 根上本就只有非继承 (DC) 一条。
    树与散文件的 deny 必须仍在 —— 它们由 _clear_own_artifacts 名单
    逐项整树/单点摘, 不靠根递归兜底。"""
    _apply_ship_recipe(ship_base)
    ntfs_guard.strip_delete_denial(ship_base, recurse=False)
    assert ntfs_guard.deny_rights(ship_base) == []  # 根上 (DC) 已摘
    assert ntfs_guard.tree_deny_present(ship_base / config.DATA_DIR_NAME)
    assert ntfs_guard.has_delete_deny(ship_base / config.KEYINFO_NAME)


# ---------------------------------------------------------------------------
# burner 集成: 施加编排 / 换格式=换代判据 / 清障自愈
# ---------------------------------------------------------------------------


@pytest.fixture()
def stick_like(tmp_path: Path) -> Path:
    """单分区交付形态微缩: 链 + 阅览器目录 + 散 exe, 名字走 burner 全名单。"""
    drive = tmp_path / "USB"
    (drive / config.DATA_DIR_NAME).mkdir(parents=True)
    (drive / config.DATA_DIR_NAME / "book.enc").write_bytes(b"c")
    (drive / config.KEYINFO_NAME).write_bytes(b"k")
    (drive / config.MANIFEST_NAME).write_bytes(b"m")
    viewer_dir = drive / config.VIEWER_WIN_DIR_NAME_DUAL
    viewer_dir.mkdir()
    (viewer_dir / config.VIEWER_EXE_NAME_DUAL).write_bytes(b"EXE")
    (drive / config.VIEWER_EXE_NAME_DUAL).write_bytes(b"EXE")
    return drive


def test_apply_delete_denial_covers_whole_recipe(stick_like: Path) -> None:
    drive = stick_like
    data_dir = drive / config.DATA_DIR_NAME
    all_ok, warns = _apply_delete_denial(drive, drive, data_dir)
    assert all_ok and warns == []
    assert ntfs_guard.check_delete_denial(
        drive, dir_names=_ALL_VIEWER_DIR_NAMES,
        file_names=(*_ALL_EXE_NAMES, config.KEYINFO_NAME, config.MANIFEST_NAME),
    ) == []
    # verify 判据的负检出: 摘掉阅览器树的 deny → check 必须报出缺 ACE
    ntfs_guard.strip_delete_denial(drive / config.VIEWER_WIN_DIR_NAME_DUAL)
    problems = ntfs_guard.check_delete_denial(
        drive, dir_names=_ALL_VIEWER_DIR_NAMES,
        file_names=(*_ALL_EXE_NAMES, config.KEYINFO_NAME, config.MANIFEST_NAME),
    )
    assert any(config.VIEWER_WIN_DIR_NAME_DUAL in p for p in problems)


def test_apply_delete_denial_non_ntfs_warns_not_raises(
    stick_like: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exFAT 单分区是合法烧录场景: 记警告缺席, 绝不熔断。"""
    monkeypatch.setattr(ntfs_guard, "fs_name", lambda root: "exFAT")
    drive = stick_like
    all_ok, warns = _apply_delete_denial(drive, drive, drive / config.DATA_DIR_NAME)
    assert not all_ok
    assert any("非 NTFS" in w for w in warns)


def test_fs_not_ntfs_generation_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换格式=换代判据: exFAT/读不出 → True (走重建), NTFS → False。"""
    warns: list[str] = []
    monkeypatch.setattr("src.app.usb_vault.burner._volume_fs", lambda root: "exFAT")
    assert _fs_not_ntfs(tmp_path, "分区1", warns) is True
    monkeypatch.setattr("src.app.usb_vault.burner._volume_fs", lambda root: "")
    assert _fs_not_ntfs(tmp_path, "分区1", warns) is True  # 读不出按旧形态处理
    monkeypatch.setattr("src.app.usb_vault.burner._volume_fs", lambda root: "NTFS")
    assert _fs_not_ntfs(tmp_path, "分区1", warns) is False
    assert any("换格式=换代" in w for w in warns)


def test_clear_own_artifacts_strips_denial_first(stick_like: Path) -> None:
    """带着拒删 ACL 重烧: 清障在前, unlink/rmtree 不被内核拒绝 (自愈闭环)。"""
    drive = stick_like
    data_dir = drive / config.DATA_DIR_NAME
    _apply_delete_denial(drive, drive, data_dir)
    _clear_own_artifacts(drive)  # 内部先整树摘 deny, 再删产物
    assert not (drive / config.KEYINFO_NAME).exists()
    assert not (drive / config.DATA_DIR_NAME).exists()
    assert not (drive / config.VIEWER_WIN_DIR_NAME_DUAL).exists()
    (drive / "用户文件.txt").write_text("不动", encoding="utf-8")  # 根可写 = ACL 已净


def test_clear_own_artifacts_root_strip_is_not_recursive(
    stick_like: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真机事故回归锚 (2026-09-29): 分区根清障绝不带 /t。

    带着防误删 ACL 的真棒重烧时, 从分区根 /t 递归必踩 System Volume
    Information (管理员亦无权, icacls rc=5 熔断) —— 施加真实 deny 后窥探
    icacls 实参: 根调用无 /t, 交付树调用保持整树 /t (清障效果仍由
    test_clear_own_artifacts_strips_denial_first 真删除断言兜底)。"""
    drive = stick_like
    _apply_delete_denial(drive, drive, drive / config.DATA_DIR_NAME)
    calls: list[tuple[Path, tuple[str, ...]]] = []
    real_icacls = ntfs_guard._icacls

    def spy(path: Path, *args: str, **kw: object) -> None:
        calls.append((path, args))
        real_icacls(path, *args, **kw)  # type: ignore[arg-type]  # 透传真实执行

    monkeypatch.setattr(ntfs_guard, "_icacls", spy)
    _clear_own_artifacts(drive)
    root_calls = [args for p, args in calls if p == drive]
    tree_calls = [args for p, args in calls if p != drive]
    assert root_calls, "分区根必须有清障调用"
    assert all("/t" not in args for args in root_calls)
    assert any("/t" in args for args in tree_calls), "交付树必须保持整树 /t"
