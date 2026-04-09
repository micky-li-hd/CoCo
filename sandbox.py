#!/usr/bin/env python3
"""
轻量级沙箱执行引擎 - 基于 Thyme 的实现但简化为图像处理专用
参考: ./bagel_new/Thyme/eval/VLMEvalKit/vlmeval/vlm/thyme/sandbox.py

关键特性:
1. 危险代码检查 (黑名单)
2. 文件输入保护 (只读)
3. 代码缩进修复
4. 超时控制 (默认10秒)
5. 异常捕获和错误记录
"""

import json
import os
import re
import contextlib
import ast
import sys
import io
import stat
import glob
import traceback
import textwrap
import signal
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from PIL import Image

# 为 matplotlib 设置稳定可写缓存目录，避免并发/权限导致字体缓存行为不稳定
_DEFAULT_MPLCONFIGDIR = f"/tmp/bagel_mplconfig_{os.getuid()}"
if not os.environ.get("MPLCONFIGDIR"):
    os.environ["MPLCONFIGDIR"] = _DEFAULT_MPLCONFIGDIR
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

# 导入matplotlib - 用于中文字体配置
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ============================================================================
# CJK 字体配置 - 显式注册所有可用中文字体，建立完整 fallback 链
# ============================================================================

# 系统上已确认可用的 CJK 字体路径（按优先级排序）
_CJK_FONT_PATHS = [
    # 简体中文，无衬线，覆盖最全 - 首选
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # Arphic 文鼎宋体 GB（简体中文衬线）
    "/usr/share/fonts/truetype/arphic-gbsn00lp/gbsn00lp.ttf",
    # Arphic 文鼎楷体 GB
    "/usr/share/fonts/truetype/arphic-gkai00mp/gkai00mp.ttf",
    # Arphic UMing（简繁中文，TTC 集合）
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    # IPA Gothic（日文+部分中文）
    "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
]

# 去掉不存在的路径
_CJK_FONT_PATHS = [p for p in _CJK_FONT_PATHS if os.path.exists(p)]

# 字体 family 名称（供 rcParams 使用），按优先级排列
# 规则：Latin/ASCII 字体放首位，CJK 字体作为 fallback
# - AR PL SungtiL GB 同时有 Latin 和简体中文，是最好的混合 fallback
# - Droid Sans Fallback 只有 CJK 没有 ASCII，必须排在 Latin 字体之后
_CJK_FONT_FAMILIES = [
    "DejaVu Sans",          # 首选：完整 ASCII/Latin，保证英文字符正常渲染
    "AR PL SungtiL GB",     # 同时有 Latin + 简体中文（宋体）
    "AR PL KaitiM GB",      # 简体中文（楷体）
    "AR PL UMing CN",       # 简繁中文（明体 TTC）
    "Droid Sans Fallback",  # 纯 CJK 补充（无 ASCII，放最后）
    "IPAexGothic",          # 日文 + 部分中文
]

_PRIMARY_CJK_FONT = "DejaVu Sans"

if MATPLOTLIB_AVAILABLE:
    # 显式注册所有 CJK 字体，避免 font cache 缺失导致 tofu
    for _p in _CJK_FONT_PATHS:
        try:
            fm.fontManager.addfont(_p)
        except Exception:
            pass

    # 运行时动态选择当前进程可解析的 CJK 主字体，避免硬编码 family 不存在
    _PRIMARY_CANDIDATES = [
        "AR PL SungtiL GB",
        "AR PL UMing CN",
        "AR PL KaitiM GB",
        "Droid Sans Fallback",
        "IPAexGothic",
        "DejaVu Sans",
    ]
    for _family in _PRIMARY_CANDIDATES:
        try:
            fm.findfont(_family, fallback_to_default=False)
            _PRIMARY_CJK_FONT = _family
            break
        except Exception:
            continue

    plt.rcParams['font.family'] = _PRIMARY_CJK_FONT
    plt.rcParams['font.sans-serif'] = _CJK_FONT_FAMILIES
    plt.rcParams['axes.unicode_minus'] = False

# ============================================================================
# 配置
# ============================================================================

EXEC_TIME_LIMIT = 10  # 执行超时 (秒)


# 超时处理 (使用 signal 模块，不依赖 timeout_decorator)
class TimeoutException(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutException("Code execution timeout")


# 危险操作黑名单 (参考 Thyme line 36-50)
DANGEROUS_PATTERNS = [
    r'\bsys\.',                      # 系统调用
    r'\bsocket\.',                   # 网络
    r'\bsubprocess\.',               # 子进程
    r'\bexec\(', r'\beval\(',        # 代码注入
    r'\bcompile\(',                  # 动态编译
    r'\b__import__\(',               # 动态导入
    r'\bos\.(remove|unlink|rmdir)\b',  # 文件删除
    r'\bos\.system\b',               # os.system 调用
    r'\bos\.popen\b',                # os.popen 调用
    r'\bshutil\.rmtree\b',           # 目录删除
    r'\bshutil\.move\b',             # 文件移动
    r'\bos\.(rename|renames)\b',     # 文件重命名
]


# ============================================================================
# 工具类
# ============================================================================

class ReadOnlyPath:
    """
    上下文管理器: 在执行期间将文件设为只读
    参考 Thyme line 71-106
    """
    def __init__(self, path):
        self.path = path if isinstance(path, str) else None
        self.original_permissions = None

    def __enter__(self):
        if self.path and os.path.isfile(self.path):
            try:
                self.original_permissions = os.stat(self.path).st_mode
                read_only = self.original_permissions & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                if self.original_permissions != read_only:
                    os.chmod(self.path, read_only)
            except OSError as e:
                print(f"⚠️  无法设置文件为只读: {e}", file=sys.stderr)
                self.original_permissions = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.path and self.original_permissions is not None and os.path.isfile(self.path):
            try:
                os.chmod(self.path, self.original_permissions)
            except OSError as e:
                print(f"⚠️  无法恢复文件权限: {e}", file=sys.stderr)


def align_first_line_to_second(code_string: str) -> str:
    """
    修复代码缩进: 将第一行的缩进对齐到第二行
    参考 Thyme line 109-163
    """
    lines = code_string.splitlines()
    first_line_info = None
    second_line_info = None

    for index, line_content in enumerate(lines):
        if line_content.strip():
            if first_line_info is None:
                first_line_info = {'index': index, 'content': line_content}
            elif second_line_info is None:
                second_line_info = {'index': index, 'content': line_content}
                break

    if not first_line_info or not second_line_info:
        return code_string

    first_content = first_line_info['content']
    second_content = second_line_info['content']

    first_indent = ' ' * (len(first_content) - len(first_content.lstrip(' ')))
    second_indent = ' ' * (len(second_content) - len(second_content.lstrip(' ')))

    if first_indent != second_indent:
        original_index = first_line_info['index']
        stripped = first_content.lstrip(' ')
        lines[original_index] = second_indent + stripped

    return "\n".join(lines)


def get_image_paths(output_dir: str) -> List[str]:
    """查找输出目录中的所有图像文件"""
    extensions = ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'tiff', 'webp', 'svg']
    image_paths = []
    for ext in extensions:
        pattern = os.path.join(output_dir, f'*.{ext}')
        image_paths.extend(glob.glob(pattern))
    return image_paths


# ============================================================================
# 核心沙箱实现
# ============================================================================

class CodeSandbox:
    """
    轻量级代码沙箱执行引擎
    参考 Thyme 的实现，简化为图像处理专用场景
    """

    def __init__(self, timeout: int = EXEC_TIME_LIMIT, temp_dir: str = None):
        """
        初始化沙箱

        Args:
            timeout: 执行超时时间 (秒)
            temp_dir: 临时输出目录
        """
        self.timeout = timeout
        self.temp_dir = temp_dir or "./temp_output"
        os.makedirs(self.temp_dir, exist_ok=True)

    @staticmethod
    def check_dangerous_code(code: str) -> bool:
        """
        检查代码是否包含危险操作

        参考 Thyme line 53-61

        Args:
            code: Python代码字符串

        Returns:
            bool: True = 代码安全, False = 代码包含危险操作
        """
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                return False
        return True

    @staticmethod
    def prepare_sandbox_environment(allowed_modules: Dict = None) -> Tuple[Dict, Dict]:
        """
        准备安全的执行环境

        参考 Thyme line 476-518

        Returns:
            tuple: (sandbox_globals, sandbox_locals)
        """
        try:
            import numpy as np
        except ImportError:
            np = None

        try:
            import cv2
        except ImportError:
            cv2 = None

        try:
            import pandas as pd
        except ImportError:
            pd = None

        try:
            import math
            import random
            import datetime
            import collections
            import itertools
            import functools
        except ImportError:
            pass

        # 允许的内置函数 (参考 Thyme line 476-494)
        allowed_builtins = {
            "print": print,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "frozenset": frozenset,
            "range": range,
            "round": round,
            "abs": abs,
            "min": min,
            "max": max,
            "sum": sum,
            "sorted": sorted,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "any": any,
            "all": all,
            "isinstance": isinstance,
            "issubclass": issubclass,
            "type": type,
            "pow": pow,
            "divmod": divmod,
            "bin": bin,
            "oct": oct,
            "hex": hex,
            "chr": chr,
            "ord": ord,
            "repr": repr,
            "format": format,
            "bytes": bytes,
            "bytearray": bytearray,
            "complex": complex,
            "slice": slice,
            "id": id,
            "hash": hash,
            "callable": callable,
            "iter": iter,
            "next": next,
            "vars": vars,
            "dir": dir,
            "hasattr": hasattr,
            "getattr": getattr,
            "setattr": setattr,
            "delattr": delattr,
            "open": open,
            "object": object,
            "super": super,
            "property": property,
            "classmethod": classmethod,
            "staticmethod": staticmethod,
            "__build_class__": __build_class__,
            "__import__": __import__,
            "locals": locals,
            "globals": globals,
            # 常用异常类
            "Exception": Exception,
            "BaseException": BaseException,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "KeyError": KeyError,
            "IndexError": IndexError,
            "AttributeError": AttributeError,
            "NotImplementedError": NotImplementedError,
            "StopIteration": StopIteration,
            "RuntimeError": RuntimeError,
            "OverflowError": OverflowError,
            "ZeroDivisionError": ZeroDivisionError,
            "IOError": IOError,
            "OSError": OSError,
            "NameError": NameError,
            "ImportError": ImportError,
            "AssertionError": AssertionError,
            "FileNotFoundError": FileNotFoundError,
            "MemoryError": MemoryError,
            "ArithmeticError": ArithmeticError,
            "GeneratorExit": GeneratorExit,
        }

        # 全局环境 (参考 Thyme line 496-512)
        sandbox_globals = {
            "__builtins__": allowed_builtins,
            "__name__": "__main__",
            "os": os,
            "sys": sys,
            "json": json,
            "re": re,
            "math": math,
            "random": random,
            "datetime": datetime,
            "collections": collections,
            "itertools": itertools,
            "functools": functools,
            "Image": Image,
            "PIL": __import__('PIL'),
        }

        if np is not None:
            sandbox_globals["np"] = np
            sandbox_globals["numpy"] = np

        if cv2 is not None:
            sandbox_globals["cv2"] = cv2

        if pd is not None:
            sandbox_globals["pd"] = pd
            sandbox_globals["pandas"] = pd

        # 配置matplotlib支持中文 - 关键修复
        if MATPLOTLIB_AVAILABLE:
            import matplotlib.patches as mpatches
            import matplotlib.colors as mcolors
            import matplotlib.ticker as mticker
            import matplotlib.lines as mlines
            import matplotlib.path as mpath
            import matplotlib.patheffects as patheffects
            import matplotlib.gridspec as gridspec
            import matplotlib.transforms as transforms
            # 设置中文字体和负号处理
            sandbox_globals["matplotlib"] = matplotlib
            sandbox_globals["plt"] = plt
            sandbox_globals["patches"] = mpatches
            sandbox_globals["mpatches"] = mpatches
            sandbox_globals["mcolors"] = mcolors
            sandbox_globals["mticker"] = mticker
            sandbox_globals["mlines"] = mlines
            sandbox_globals["mpath"] = mpath
            sandbox_globals["patheffects"] = patheffects
            sandbox_globals["gridspec"] = gridspec
            sandbox_globals["transforms"] = transforms

            # 应用中文字体配置
            plt.rcParams['font.family'] = _PRIMARY_CJK_FONT
            plt.rcParams['font.sans-serif'] = _CJK_FONT_FAMILIES
            plt.rcParams['axes.unicode_minus'] = False

        # 合并用户提供的模块
        if allowed_modules:
            sandbox_globals.update(allowed_modules)

        # 本地环境
        sandbox_locals = {}

        return sandbox_globals, sandbox_locals

    def _execute_with_timeout(
        self,
        code: str,
        sandbox_globals: Dict,
        sandbox_locals: Dict,
        output_dir: str
    ) -> Dict:
        """
        带超时的代码执行 (核心执行逻辑)

        参考 Thyme line 629-744

        Args:
            code: Python代码字符串
            sandbox_globals: 全局变量字典
            sandbox_locals: 局部变量字典
            output_dir: 输出目录路径

        Returns:
            dict: 执行结果 {success, output, error, image_paths}
        """
        result = {
            'success': False,
            'output': '',
            'error': None,
            'image_paths': [],
            'traceback': None,
        }

        # 添加输出目录到全局和本地变量（函数内部只查 globals，不查 locals）
        sandbox_locals['output_dir'] = output_dir
        sandbox_globals['output_dir'] = output_dir

        captured_stdout = io.StringIO()

        try:
            # 代码预处理
            code = align_first_line_to_second(code)
            code = textwrap.dedent(code).strip()

            # 禁用xkcd样式 - 与中文字体配置不兼容
            code = code.replace('with plt.xkcd(scale=1, length=100, randomness=2):',
                              'if True:  # xkcd style disabled for Chinese text support')

            # 替换代码里的字体设置 - 强制使用同时覆盖 Latin + CJK 的字体
            # 这版 matplotlib 不支持 per-character fallback，必须用单一全覆盖字体
            import re as regex_module
            _font_list_repr = repr(_CJK_FONT_FAMILIES)
            # 替换 font.sans-serif 设置
            pattern = r"plt\.rcParams\['font\.sans-serif'\]\s*=\s*\[.*?\]"
            code = regex_module.sub(
                pattern,
                f"plt.rcParams['font.sans-serif'] = {_font_list_repr}",
                code, flags=regex_module.DOTALL
            )
            # 替换 font.family 设置（如果代码有显式设置）
            pattern2 = r"""plt\.rcParams\[['"]font\.family['"]\]\s*=\s*['"][^'"]*['"]"""
            code = regex_module.sub(
                pattern2,
                f"plt.rcParams['font.family'] = '{_PRIMARY_CJK_FONT}'",
                code
            )
            # 在代码开头注入字体设置，确保优先级最高
            font_injection = (
                f"plt.rcParams['font.family'] = '{_PRIMARY_CJK_FONT}'\n"
                f"plt.rcParams['font.sans-serif'] = {_font_list_repr}\n"
                f"plt.rcParams['axes.unicode_minus'] = False\n"
            )
            code = font_injection + code

            # 修复 output_dir：覆盖代码中所有非绝对路径的 output_dir 字符串赋值
            # 匹配 output_dir = '任意值' 或 output_dir = "任意值"，跳过绝对路径
            def _replace_output_dir(m):
                val = m.group(1) if m.group(1) is not None else m.group(2)
                if val.startswith('/'):
                    return m.group(0)  # 保留绝对路径不变
                return f"output_dir = r'{output_dir}'"
            code = re.sub(
                r"""output_dir\s*=\s*(?:'([^']*)'|"([^"]*)")""",
                _replace_output_dir,
                code
            )

            # 修复非标准 matplotlib 颜色名称（5次重试都会失败）
            _INVALID_COLORS = [
                ('brick',       '#B5451B'),
                ('mintgreen',   '#98FF98'),
                ('darkpink',    '#FF1493'),
                ('lightolive',  '#808000'),
                ('lightolive',  '#808000'),
                ('darkolive',   '#556B2F'),
                ('darkviolet2', '#9400D3'),
            ]
            for bad_color, good_color in _INVALID_COLORS:
                code = code.replace(f"'{bad_color}'", f"'{good_color}'")
                code = code.replace(f'"{bad_color}"', f'"{good_color}"')

            # 修复 stem() 不支持的 baseline 参数
            code = re.sub(r',\s*baseline\s*=\s*[^,)\s][^,)]*', '', code)

            # 修复无效 legend loc 值（matplotlib 不支持组合位置如 'upper center right'）
            _INVALID_LOCS = [
                ('upper center right', 'upper right'),
                ('upper center left',  'upper left'),
                ('lower center right', 'lower right'),
                ('lower center left',  'lower left'),
            ]
            for bad_loc, good_loc in _INVALID_LOCS:
                code = code.replace(f"'{bad_loc}'", f"'{good_loc}'")
                code = code.replace(f'"{bad_loc}"', f'"{good_loc}"')

            # 使用 signal 实现超时 (Unix/Linux)
            if sys.platform != 'win32':
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(self.timeout)

            try:
                # 执行代码（只传 globals，让顶层变量对函数内部可见）
                sandbox_globals.update(sandbox_locals)
                with contextlib.redirect_stdout(captured_stdout):
                    exec(code, sandbox_globals)

                # 强制关闭所有 figure，防止内存泄漏
                if MATPLOTLIB_AVAILABLE:
                    plt.close('all')

                # 取消超时警报
                if sys.platform != 'win32':
                    signal.alarm(0)

                result['success'] = True
                result['output'] = captured_stdout.getvalue().strip()

                # 查找输出的图像文件
                result['image_paths'] = get_image_paths(output_dir)

                if not result['image_paths'] and result['output']:
                    # 尝试从 stdout 解析文件路径
                    path_patterns = [
                        rf"({re.escape(output_dir)}[^\s'\"\]]+\.(?:jpg|jpeg|png|bmp|gif|tiff))",
                        r"([^\s'\"\]]+\.(?:jpg|jpeg|png|bmp|gif|tiff))"
                    ]
                    for pattern in path_patterns:
                        matches = re.findall(pattern, result['output'])
                        for match in matches:
                            if os.path.isfile(match):
                                result['image_paths'].append(match)
                                break
                        if result['image_paths']:
                            break

                if not result['image_paths'] and result['output']:
                    result['error'] = "执行成功但未生成图像"

            finally:
                # 确保恢复信号处理，并关闭所有残留 figure
                if MATPLOTLIB_AVAILABLE:
                    plt.close('all')
                if sys.platform != 'win32':
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

        except TimeoutException:
            result['error'] = f"代码执行超时 (>{self.timeout}秒)"
            result['traceback'] = "execution timeout"

        except ImportError as e:
            result['error'] = f"导入错误: {str(e)}"
            result['traceback'] = traceback.format_exc()

        except MemoryError as e:
            result['error'] = "内存溢出"
            result['traceback'] = traceback.format_exc()

        except SyntaxError as e:
            result['error'] = f"语法错误: {str(e)}"
            result['traceback'] = traceback.format_exc()

        except Exception as e:
            result['error'] = f"{type(e).__name__}: {str(e)}"
            result['traceback'] = traceback.format_exc()

        return result

    def execute(
        self,
        code: str,
        input_image_path: Optional[str] = None,
        allowed_modules: Dict = None,
        item_id: str = "N/A"
    ) -> Dict:
        """
        执行Python代码在沙箱中

        Args:
            code: Python代码字符串
            input_image_path: 输入图像路径 (可选)
            allowed_modules: 额外允许的模块字典
            item_id: 项目ID (用于日志)

        Returns:
            dict: {
                'success': bool,
                'output': str,
                'error': str|None,
                'image_paths': list,
                'traceback': str|None
            }
        """
        result = {
            'success': False,
            'output': '',
            'error': None,
            'image_paths': [],
            'traceback': None,
        }

        # 安全检查
        if not self.check_dangerous_code(code):
            result['error'] = "代码包含危险操作"
            return result

        # 准备沙箱环境
        sandbox_globals, sandbox_locals = self.prepare_sandbox_environment(allowed_modules)

        # 为输出目录创建子目录
        output_dir = os.path.join(self.temp_dir, f"exec_{item_id}")
        os.makedirs(output_dir, exist_ok=True)

        # 保护输入文件 (只读)
        try:
            with ReadOnlyPath(input_image_path):
                result = self._execute_with_timeout(
                    code,
                    sandbox_globals,
                    sandbox_locals,
                    output_dir
                )
        except TimeoutException:
            result['error'] = f"代码执行超时 (>{self.timeout}秒)"
            result['traceback'] = "execution timeout"
        except Exception as e:
            result['error'] = f"执行失败: {str(e)}"
            result['traceback'] = traceback.format_exc()

        return result


# ============================================================================
# 便利函数
# ============================================================================

def execute_code(
    code: str,
    input_image: Optional[str] = None,
    timeout: int = EXEC_TIME_LIMIT,
    output_dir: str = "./temp_output",
    allowed_modules: Dict = None
) -> Dict:
    """
    快速执行代码的便利函数

    Args:
        code: Python代码
        input_image: 输入图像路径
        timeout: 超时时间
        output_dir: 输出目录
        allowed_modules: 额外模块

    Returns:
        dict: 执行结果
    """
    sandbox = CodeSandbox(timeout=timeout, temp_dir=output_dir)
    return sandbox.execute(code, input_image, allowed_modules)
