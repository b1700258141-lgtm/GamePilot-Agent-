"""独立性守卫：判定器与执行器不得依赖被测实现。

两道检查：

1. 静态：`src/gamepilot/testing/` 下任何模块都不 import 其他 `gamepilot` 包
   （相对导入自身包除外）。判定规则必须来自规则文档，不能来自被测常量。
2. 运行时：在干净的解释器里只导入 testing 包，确认 `gamepilot.domain`、
   `gamepilot.repositories`、`gamepilot.persistence` 等实现模块没有进入
   `sys.modules`——「没有导入」这件事由解释器本身作证，而不是靠人看代码。
"""

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "src" / "gamepilot" / "testing"

# 允许出现的 gamepilot 模块前缀：只有 testing 包自己。
ALLOWED_PREFIX = "gamepilot.testing"


def _package_files() -> list[Path]:
    return sorted(PACKAGE_DIR.glob("*.py"))


def _absolute_gamepilot_imports(path: Path) -> list[str]:
    """找出文件里所有绝对导入的 gamepilot 模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.startswith("gamepilot"))
        elif isinstance(node, ast.ImportFrom):
            # level > 0 是包内相对导入，不构成对外部模块的依赖。
            if node.level == 0 and node.module and node.module.startswith("gamepilot"):
                found.append(node.module)
    return found


def test_package_has_modules_to_check() -> None:
    """守卫本身要有效：确认扫描目录里确实有模块。"""
    assert len(_package_files()) >= 5


def test_no_module_imports_implementation_packages() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _package_files():
        bad = [
            module
            for module in _absolute_gamepilot_imports(path)
            if not module.startswith(ALLOWED_PREFIX)
        ]
        if bad:
            offenders[path.name] = bad
    assert offenders == {}, f"testing 包不得导入被测实现：{offenders}"


def test_importing_testing_package_loads_no_implementation_module() -> None:
    code = (
        "import sys\n"
        "sys.path.insert(0, 'src')\n"
        "import gamepilot.testing.cli\n"
        "import gamepilot.testing.oracle\n"
        "import gamepilot.testing.replay\n"
        "import gamepilot.testing.runner\n"
        "loaded = sorted(\n"
        "    name\n"
        "    for name in sys.modules\n"
        # 顶层 gamepilot 包必然被加载（它是 testing 的父包，__init__ 只有版本号），
        # 这里检查的是它下面有没有任何实现子模块被拉进来。
        "    if name.startswith('gamepilot.')\n"
        "    and not name.startswith('gamepilot.testing')\n"
        ")\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = [name for name in result.stdout.strip().split(",") if name]
    assert loaded == [], f"导入 testing 包时不应加载实现模块：{loaded}"
