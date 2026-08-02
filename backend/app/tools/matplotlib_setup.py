"""Matplotlib 引导脚本，供本地/E2B 解释器在 kernel 启动时注入字体与绘图常量。"""

from __future__ import annotations

# 竞赛/学术向配色。键名与 visualization.md 技能文档严格一致（单一真源），
# 否则技能模板引用 COLORS['accent'] 等会抛 KeyError 导致丑图/放弃配色。
COLORS: dict[str, str] = {
    "primary": "#2E5B88",  # 主色：深蓝
    "secondary": "#E07B54",  # 次色：橙红
    "accent": "#4A9B7F",  # 强调：青绿
    "neutral": "#7F7F7F",  # 中性：灰
    "success": "#5FA55A",  # 正向：绿
    "warning": "#E0A33E",  # 警示：琥珀
    "danger": "#C0504D",  # 负向：砖红
    "light": "#B8D4E8",  # 浅色填充
}

# 多系列循环顺序（供 DEFAULT_COLORS 使用，排除 light/neutral 作为主循环）
_COLOR_CYCLE_KEYS = [
    "primary",
    "secondary",
    "accent",
    "success",
    "warning",
    "danger",
    "neutral",
]

# 图尺寸（英寸）。与 visualization.md 技能文档保持一致（单一真源）。
FIG_SINGLE = (6.5, 4.5)  # 单栏图
FIG_DOUBLE = (13, 4.5)  # 双栏并排
FIG_WIDE = (10, 4)  # 宽图（时序）
FIG_SQUARE = (5, 5)  # 方形图（热力图/网络）


# 注入到 kernel 命名空间的可复用绘图辅助函数（代码级强制质量/预算约束）。
# 说明：这些函数在 kernel 内定义，运行时引用 kernel 的 COLORS，因此配色与全局一致。
HELPERS_CODE = r'''
def save_fig(fig, name, dpi=300):
    """统一保存图片：300dpi + bbox tight + 自动 close，返回文件名。
    优先使用本函数而非手写 plt.savefig，避免遗漏 dpi/close 导致丑图或内存泄漏。
    """
    import matplotlib.pyplot as plt
    if not str(name).lower().endswith((".png", ".jpg", ".jpeg")):
        name = str(name) + ".png"
    fig.savefig(name, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return name


# 全文柱状图预算计数器（硬上限 3）。跨 execute 持久，因为 kernel 命名空间不重置。
_BAR_CHART_COUNT = {"n": 0}
FIG_BUDGET = 18  # 全文图片建议上限


def note_bar_chart(name=""):
    """登记一次柱状图使用；超过全文上限(3)时打印醒目告警（Agent 可见文本）。"""
    _BAR_CHART_COUNT["n"] += 1
    n = _BAR_CHART_COUNT["n"]
    if n > 3:
        print(f"[FIG-BUDGET][WARN] 柱状图已达 {n} 个，超过全文上限 3 个 (最新: {name})。"
              f"请改用折线/箱线/热力图，或与已有柱状图合并。")
    else:
        print(f"[FIG-BUDGET] 柱状图 {n}/3 (最新: {name})")
    return n


def barh_topn(ax, labels, values, top=15, highlight_max=True, fmt="{:.2f}", name=""):
    """水平条形图 Top-N：按值降序、只画前 top 项、标注数值、最大值高亮。
    专治"几十个类别标签重叠不可读"。自动登记柱状图预算 note_bar_chart。
    """
    import numpy as np
    labels = list(labels)
    values = list(values)
    order = list(np.argsort(values)[::-1][:top])
    sel_labels = [labels[i] for i in order]
    sel_values = [values[i] for i in order]
    if highlight_max and sel_values:
        vmax = max(sel_values)
        bar_colors = [COLORS["primary"] if v == vmax else COLORS["neutral"] for v in sel_values]
    else:
        bar_colors = COLORS["primary"]
    bars = ax.barh(sel_labels, sel_values, color=bar_colors, edgecolor="none", height=0.65)
    span = (max(sel_values) - min(sel_values)) if sel_values else 1
    if not span:
        span = max(sel_values) if sel_values else 1
    for bar, v in zip(bars, sel_values):
        ax.text(bar.get_width() + span * 0.01,
                bar.get_y() + bar.get_height() / 2,
                fmt.format(v), va="center", fontsize=8)
    ax.invert_yaxis()
    note_bar_chart(name)
    return ax


def annotate_stats(ax, text, loc=(0.05, 0.92)):
    """在轴内左上角统一标注统计量文本 (r/p/R²/RMSE 等)。"""
    ax.annotate(text, xy=loc, xycoords="axes fraction",
                fontsize=9, ha="left", va="top")
    return ax
'''


def build_matplotlib_init_code(
    work_dir: str,
    *,
    font_dir: str | None = None,
    setup_chdir: bool = True,
) -> str:
    """生成在 Jupyter/E2B kernel 中执行的 matplotlib 初始化代码。

    Args:
        work_dir: 任务工作目录（本地解释器用于 chdir）。
        font_dir: 字体文件目录，默认与 work_dir 相同。
        setup_chdir: 是否切换到 work_dir。

    Returns:
        可在 kernel 中执行的 Python 代码字符串。
    """
    font_dir = font_dir or work_dir
    colors_repr = repr(COLORS)
    fig_single = repr(FIG_SINGLE)
    fig_double = repr(FIG_DOUBLE)
    fig_wide = repr(FIG_WIDE)
    fig_square = repr(FIG_SQUARE)

    # 必须先解析为绝对路径：相对 work_dir 在 os.chdir 之后会导致 listdir/addfont 失败
    chdir_block = (
        "import os\n"
        f"work_dir = os.path.abspath(r'{work_dir}')\n"
        f"_font_dir = os.path.abspath(r'{font_dir}')\n"
    )
    if setup_chdir:
        chdir_block += (
            "os.makedirs(work_dir, exist_ok=True)\n"
            "os.chdir(work_dir)\n"
            "print('[matplotlib_setup] 当前工作目录:', os.getcwd())\n"
        )

    return (
        chdir_block
        + "import matplotlib\n"
        + "import matplotlib.pyplot as plt\n"
        + "from matplotlib import font_manager\n"
        + "import glob as _glob, pathlib as _pl\n"
        + "_cache_dir = _pl.Path(matplotlib.get_cachedir())\n"
        + "for _cache_file in _glob.glob(str(_cache_dir / 'fontlist*.json')):\n"
        + "    _pl.Path(_cache_file).unlink(missing_ok=True)\n"
        + "font_manager.fontManager.__init__()\n"
        + "_cjk_fonts = []\n"
        + "for _f in os.listdir(_font_dir):\n"
        + "    if _f.lower().endswith(('.ttf', '.otf', '.ttc')):\n"
        + "        _fp = os.path.join(_font_dir, _f)\n"
        + "        font_manager.fontManager.addfont(_fp)\n"
        + "        _name = font_manager.FontProperties(fname=_fp).get_name()\n"
        + "        if _name not in _cjk_fonts:\n"
        + "            _cjk_fonts.append(_name)\n"
        + "if _cjk_fonts:\n"
        + "    CJK_FONT = _cjk_fonts[0]\n"
        + "    _fallback = ['Heiti SC', 'STHeiti', 'PingFang SC', 'Noto Sans CJK SC', 'Noto Sans SC', 'WenQuanYi Micro Hei', 'Microsoft YaHei', 'sans-serif']\n"
        + "    plt.rcParams['font.sans-serif'] = _cjk_fonts + [f for f in _fallback if f not in _cjk_fonts]\n"
        + "    plt.rcParams['axes.unicode_minus'] = False\n"
        + "    plt.rcParams['font.family'] = 'sans-serif'\n"
        + "    print(f'[matplotlib_setup] 中文字体已加载: {CJK_FONT} (共 {len(_cjk_fonts)} 个)')\n"
        + "else:\n"
        + "    CJK_FONT = None\n"
        + "    print('[matplotlib_setup] 警告: 未找到中文字体文件，中文标签可能显示为方框')\n"
        + "plt.rcParams.update({\n"
        + "    'font.size': 11,\n"
        + "    'axes.titlesize': 12,\n"
        + "    'axes.titleweight': 'bold',\n"
        + "    'axes.labelsize': 11,\n"
        + "    'axes.linewidth': 1.2,\n"
        + "    'axes.spines.top': False,\n"
        + "    'axes.spines.right': False,\n"
        + "    'xtick.labelsize': 10,\n"
        + "    'ytick.labelsize': 10,\n"
        + "    'legend.fontsize': 10,\n"
        + "    'legend.frameon': False,\n"
        + "    'figure.dpi': 300,\n"
        + "    'savefig.dpi': 300,\n"
        + "    'savefig.bbox': 'tight',\n"
        + "    'savefig.pad_inches': 0.1,\n"
        + "})\n"
        + f"COLORS = {colors_repr}\n"
        + f"DEFAULT_COLORS = [COLORS[_k] for _k in {_COLOR_CYCLE_KEYS!r} if _k in COLORS]\n"
        + "import matplotlib as _mpl\n"
        + "_mpl.rcParams['axes.prop_cycle'] = _mpl.cycler(color=DEFAULT_COLORS)\n"
        + f"FIG_SINGLE = {fig_single}\n"
        + f"FIG_DOUBLE = {fig_double}\n"
        + f"FIG_WIDE = {fig_wide}\n"
        + f"FIG_SQUARE = {fig_square}\n"
        + HELPERS_CODE
        + "print('[matplotlib_setup] 绘图环境就绪 (COLORS, FIG_*, save_fig, barh_topn, annotate_stats, note_bar_chart 已注入)')\n"
    )
