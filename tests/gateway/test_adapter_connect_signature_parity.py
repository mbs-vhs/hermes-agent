from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_SOURCE_GLOBS = (
    "gateway/platforms/**/adapter.py",
    "gateway/platforms/*.py",
    "gateway/relay/adapter.py",
    "plugins/platforms/**/*adapter.py",
)


@dataclass(frozen=True)
class AdapterConnect:
    class_name: str
    path: Path
    node: ast.AsyncFunctionDef | ast.FunctionDef

    @property
    def relative_path(self) -> Path:
        return self.path.relative_to(REPO_ROOT)


def _adapter_source_paths() -> list[Path]:
    paths: set[Path] = set()
    for pattern in ADAPTER_SOURCE_GLOBS:
        paths.update(path for path in REPO_ROOT.glob(pattern) if path.is_file())
    return sorted(paths)


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    return ""


def _inherits_base_platform_adapter(class_node: ast.ClassDef) -> bool:
    return any(_base_name(base).endswith("BasePlatformAdapter") for base in class_node.bases)


def _connect_method(
    class_node: ast.ClassDef,
) -> ast.AsyncFunctionDef | ast.FunctionDef | None:
    for member in class_node.body:
        if isinstance(member, (ast.AsyncFunctionDef, ast.FunctionDef)) and member.name == "connect":
            return member
    return None


def _iter_adapter_connects() -> Iterable[AdapterConnect]:
    for path in _adapter_source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not _inherits_base_platform_adapter(node):
                continue
            connect = _connect_method(node)
            if connect is None:
                continue
            yield AdapterConnect(class_name=node.name, path=path, node=connect)


def _default_key(node: ast.expr | None) -> str:
    if node is None:
        return "<required>"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ast.unparse(node)


def _signature_shape(function: ast.AsyncFunctionDef | ast.FunctionDef) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    tuple[tuple[str, str], ...],
    str | None,
]:
    args = function.args
    return (
        tuple(arg.arg for arg in args.posonlyargs),
        tuple(arg.arg for arg in args.args),
        args.vararg.arg if args.vararg else None,
        tuple(
            (arg.arg, _default_key(default))
            for arg, default in zip(args.kwonlyargs, args.kw_defaults)
        ),
        args.kwarg.arg if args.kwarg else None,
    )


def _base_connect_signature_shape() -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    tuple[tuple[str, str], ...],
    str | None,
]:
    base_path = REPO_ROOT / "gateway/platforms/base.py"
    tree = ast.parse(base_path.read_text(encoding="utf-8"), filename=str(base_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BasePlatformAdapter":
            connect = _connect_method(node)
            if connect is not None:
                return _signature_shape(connect)
    raise AssertionError("BasePlatformAdapter.connect was not found")


BASE_CONNECT_SIGNATURE_SHAPE = _base_connect_signature_shape()
ADAPTER_CONNECTS = tuple(_iter_adapter_connects())


def _adapter_id(adapter: AdapterConnect) -> str:
    return f"{adapter.class_name}:{adapter.relative_path.as_posix()}"


def test_discovers_registered_adapter_connect_contract_surface():
    assert len(ADAPTER_CONNECTS) >= 30
    discovered_ids = {_adapter_id(adapter) for adapter in ADAPTER_CONNECTS}
    assert "QQAdapter:gateway/platforms/qqbot/adapter.py" in discovered_ids
    assert "WecomCallbackAdapter:plugins/platforms/wecom/callback_adapter.py" in discovered_ids


@pytest.mark.parametrize("adapter", ADAPTER_CONNECTS, ids=_adapter_id)
def test_adapter_connect_signature_matches_base_contract(adapter: AdapterConnect):
    assert _signature_shape(adapter.node) == BASE_CONNECT_SIGNATURE_SHAPE
