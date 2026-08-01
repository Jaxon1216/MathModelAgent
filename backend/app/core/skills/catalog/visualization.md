---
name: visualization
description: 学术论文级 matplotlib/seaborn 绘图规范，含图类型选择、禁止项、必须项及各类图的代码模板。创建任何图表前必须加载。
---

# 可视化规范（学术论文标准）

## 执行环境预配置（禁止重复设置）

代码沙盒启动时已注入以下全局变量，直接使用，**严禁**在代码中重新设置字体/`sns.set_theme()`：

```python
# 已注入的变量（直接用，不要重新定义）
CJK_FONT       # 中文字体名称
COLORS         # dict，含 'primary', 'secondary', 'accent', 'neutral', 'success', 'warning', 'danger'
DEFAULT_COLORS # list，颜色循环列表
FIG_SINGLE     # (6.5, 4.5)  单栏图尺寸
FIG_DOUBLE     # (13, 4.5)   双栏图尺寸
FIG_WIDE       # (10, 4)     宽图
FIG_SQUARE     # (5, 5)      方形图
```

**严格禁止**：
- `sns.set_theme()` / `sns.set_style()` / `plt.style.use()`
- 修改 `rcParams['font.*']` / `axes.unicode_minus`（会覆盖中文字体导致方块）

---

## 图类型选择

| 数据特征 | 推荐图表 | 避免使用 |
|---------|---------|---------|
| 趋势/时序 | 折线图 + 置信带（fill_between） | 纯折线无 CI |
| 分布比较 | 箱线图 / 小提琴图 | 柱状图+误差棒 |
| 相关性 | 散点图 + 回归线 + r 值标注 | 只有散点无拟合 |
| 分类对比 | 水平条形图 | 3D 柱状图 |
| 参数敏感性 | 热力图 / 等高线 / 带阴影折线 | 多条折线堆叠 |
| 后验分布 | 密度图 / 直方图 + KDE | 只有点估计 |

---

## 严格禁止

- 3D 图表（除非展示真实 3D 数据）
- 饼图（改用水平条形图）
- `ax.set_title()`（图内标题）——用论文 caption，不在图内写
- 密集网格线（已通过全局 rcParams 关闭）
- 四边完整边框（已通过全局配置只保留左+下边框）
- 低分辨率（必须 300 dpi）

---

## 必须遵守

- 使用统一 `COLORS` 配色方案，多系列用 `DEFAULT_COLORS`
- 折线图用 `fill_between` 添加置信带
- 标注关键统计量（r, p, R²）
- 子图编号用 (a), (b), (c)（添加在轴外左上角）
- 图例无边框：`legend(frameon=False)`
- 轴标签含单位：`ax.set_xlabel('时间 (月)')`
- 图例位置不遮挡数据
- 参考线需标注（基线、阈值）

---

## 图片数量建议

| 类型 | 数量 |
|------|------|
| 单个建模问题 | 4–6 张 |
| 敏感性分析 | 2–3 张 |
| EDA / 数据预处理 | 2–3 张 |
| 全文合计 | 13–18 张 |

---

## 代码模板

### 折线图 + 置信带

```python
import matplotlib.pyplot as plt
import numpy as np

fig, ax = plt.subplots(figsize=FIG_SINGLE)
ax.plot(x, y_mean, color=COLORS['primary'], linewidth=1.8, label='预测值')
ax.fill_between(x, y_lower, y_upper, alpha=0.15, color=COLORS['primary'], label='95% CI')
ax.axhline(y=baseline, color=COLORS['neutral'], linestyle='--', linewidth=1, label='基线')
ax.set_xlabel('时间 (月)')
ax.set_ylabel('产量 (吨)')
ax.legend(frameon=False, loc='best')
plt.tight_layout()
plt.savefig('trend.png', dpi=300, bbox_inches='tight')
plt.close()
```

### 相关性热力图

```python
import seaborn as sns

fig, ax = plt.subplots(figsize=FIG_SQUARE)
mask = np.triu(np.ones_like(corr, dtype=bool))  # 只显示下三角
sns.heatmap(
    corr, mask=mask, ax=ax,
    cmap='RdBu_r', center=0, vmin=-1, vmax=1,
    annot=True, fmt='.2f', annot_kws={'size': 8},
    linewidths=0.3, square=True, cbar_kws={'shrink': 0.8}
)
ax.set_xticklabels(ax.get_xticklabels(), rotation=45, ha='right')
plt.tight_layout()
plt.savefig('corr_heatmap.png', dpi=300, bbox_inches='tight')
plt.close()
```

### 散点图 + 回归线

```python
from scipy import stats

fig, ax = plt.subplots(figsize=FIG_SINGLE)
ax.scatter(x, y, color=COLORS['primary'], alpha=0.6, s=25, edgecolors='none')
slope, intercept, r, p, _ = stats.linregress(x, y)
x_line = np.linspace(x.min(), x.max(), 100)
ax.plot(x_line, slope * x_line + intercept, color=COLORS['accent'], linewidth=1.5)
ax.annotate(f'r = {r:.3f}, p = {p:.3f}', xy=(0.05, 0.92), xycoords='axes fraction',
            fontsize=9, ha='left')
ax.set_xlabel('变量 X')
ax.set_ylabel('变量 Y')
plt.tight_layout()
plt.savefig('scatter_reg.png', dpi=300, bbox_inches='tight')
plt.close()
```

### 水平条形图（分类对比）

```python
fig, ax = plt.subplots(figsize=FIG_SINGLE)
colors_bar = [COLORS['primary'] if v == max(values) else COLORS['secondary'] for v in values]
bars = ax.barh(labels, values, color=colors_bar, edgecolor='none', height=0.6)
for bar, v in zip(bars, values):
    ax.text(bar.get_width() + max(values) * 0.01, bar.get_y() + bar.get_height() / 2,
            f'{v:.2f}', va='center', fontsize=8)
ax.set_xlabel('指标值')
ax.invert_yaxis()
plt.tight_layout()
plt.savefig('bar_compare.png', dpi=300, bbox_inches='tight')
plt.close()
```

### 子图组合（(a)(b) 编号）

```python
fig, axes = plt.subplots(1, 2, figsize=FIG_DOUBLE)
for ax, label in zip(axes, ['(a)', '(b)']):
    ax.text(-0.12, 1.02, label, transform=ax.transAxes,
            fontsize=10, fontweight='bold', va='bottom')
# ... 分别绘制 axes[0], axes[1]
plt.tight_layout()
plt.savefig('combined.png', dpi=300, bbox_inches='tight')
plt.close()
```
