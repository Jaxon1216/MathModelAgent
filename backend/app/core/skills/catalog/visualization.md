---
name: visualization
description: 学术论文级 matplotlib/seaborn 绘图规范，含图类型选择、禁止项、必须项及各类图的代码模板。创建任何图表前必须加载。
---

# 可视化规范（学术论文标准）

## 执行环境预配置（禁止重复设置）

代码沙盒启动时已注入以下全局变量与函数，直接使用，**严禁**在代码中重新设置字体/`sns.set_theme()`：

```python
# 已注入的变量（直接用，不要重新定义）
CJK_FONT       # 中文字体名称
COLORS         # dict，键：'primary','secondary','accent','neutral','success','warning','danger','light'
DEFAULT_COLORS # list，多系列颜色循环（已设为 rcParams 默认 prop_cycle）
FIG_SINGLE     # (6.5, 4.5)  单栏图尺寸
FIG_DOUBLE     # (13, 4.5)   双栏并排尺寸
FIG_WIDE       # (10, 4)     宽图（时序）
FIG_SQUARE     # (5, 5)      方形图（热力图/网络）

# 已注入的函数（优先调用，不要手写等价逻辑）
save_fig(fig, name)                       # 统一 300dpi + bbox tight + 自动 close，返回文件名
barh_topn(ax, labels, values, top=15)     # 水平条形图 Top-N，自动排序/截断/标注/登记预算
annotate_stats(ax, "r=0.93, p<0.001")     # 轴内统一标注统计量
note_bar_chart(name)                      # 登记一次柱状图（barh_topn 已内部调用）
```

**严格禁止**：
- `sns.set_theme()` / `sns.set_style()` / `plt.style.use()`
- 修改 `rcParams['font.*']` / `axes.unicode_minus`（会覆盖中文字体导致方块）
- 手写 `COLORS['xxx']` 使用上表以外的键（会 KeyError）

---

## 绘图前必须先规划（迷你 figure contract）

**每张图开画前，先 `print` 三行规划**，避免"边试边画"产生一堆废图：

```python
print("【绘图规划】图: q1_特征重要性 | 核心结论: 前3特征贡献>60% | 类型: 水平条形(Top10) | 预算槽: 柱状图 1/3")
```

- 想清楚这张图要**支撑什么结论**、用**哪种图类型**、占用**哪个预算槽**，再写绘图代码。
- **禁止重画其它问题已经生成的图**（如在 ques4 里重画 ques3 的图）。

---

## 图类型选择

| 数据特征 | 推荐图表 | 避免使用 |
|---------|---------|---------|
| 趋势/时序 | 折线图 + 置信带（fill_between） | 纯折线无 CI |
| 分布比较 | 箱线图 / 小提琴图 | 柱状图+误差棒 |
| 相关性 | 散点图 + 回归线 + r 值标注 | 只有散点无拟合 |
| 分类对比 | 水平条形图（`barh_topn`，Top-N） | 全类别柱状图、3D 柱状图 |
| 参数敏感性 | 热力图 / 等高线 / 带阴影折线 | 多条折线堆叠 |
| 后验分布 | 密度图 / 直方图 + KDE | 只有点估计 |
| 长尾/重尾计数分布 | 秩-频图 或 互补累计分布 CCDF | log-x 直方图（bin 宽不均、锯齿丑） |

---

## 柱状图硬约束（必须遵守）

柱状图是本系统最容易被滥用、最丑的图类型。**强制限额**：

- **每个问题最多 1 张柱状图**；**全文最多 3 张柱状图**。超出请改用折线/箱线/热力图，或与已有柱状图合并。
- 分类对比**必须**用注入的 `barh_topn(ax, labels, values, top<=15)`，**严禁**一次性画几十个类别（如 40 个博主 ID）——标签重叠不可读。
- 需要展示"全体分布"时，用箱线图/小提琴图/CCDF，而不是把每个类别画成一根柱子。
- 每画一张柱状图都会经 `note_bar_chart` 登记；若看到 `[FIG-BUDGET][WARN]` 说明已超限，立即换图类型，不要继续加柱状图。

---

## 严格禁止

- **log-x 直方图**（`plt.hist` + `set_xscale('log')`）→ bin 宽度不均产生锯齿，改用秩-频图或 CCDF
- 一张图超过 15 个柱子 / 类别标签（改 `barh_topn` Top-N）
- 3D 图表（除非展示真实 3D 数据）
- 饼图（改用水平条形图 `barh_topn`）
- `ax.set_title()`（图内标题）——用论文 caption，不在图内写
- 密集网格线（已通过全局 rcParams 关闭）
- 四边完整边框（已通过全局配置只保留左+下边框）
- 低分辨率（必须 300 dpi，用 `save_fig` 自动保证）

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

## 图片数量预算（硬约束）

| 类型 | 数量 |
|------|------|
| 单个建模问题 | 4–6 张（其中柱状图 ≤1） |
| 敏感性分析 | 2–3 张 |
| EDA / 数据预处理 | 2–3 张 |
| **全文合计** | **13–18 张，柱状图全文 ≤3 张** |

超预算会稀释重点、拉低观感。宁可少而精，也不要为凑数反复试画。

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
save_fig(fig, 'trend')   # 统一 300dpi + close，返回 'trend.png'
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
save_fig(fig, 'corr_heatmap')
```

### 散点图 + 回归线

```python
from scipy import stats

fig, ax = plt.subplots(figsize=FIG_SINGLE)
ax.scatter(x, y, color=COLORS['primary'], alpha=0.6, s=25, edgecolors='none')
slope, intercept, r, p, _ = stats.linregress(x, y)
x_line = np.linspace(x.min(), x.max(), 100)
ax.plot(x_line, slope * x_line + intercept, color=COLORS['accent'], linewidth=1.5)
annotate_stats(ax, f'r = {r:.3f}, p = {p:.3f}')   # 统一统计量标注
ax.set_xlabel('变量 X')
ax.set_ylabel('变量 Y')
save_fig(fig, 'scatter_reg')
```

### 水平条形图（分类对比，Top-N，全文≤3）

**必须**用注入的 `barh_topn`，自动排序/截断/标注/登记预算，杜绝几十个类别重叠：

```python
fig, ax = plt.subplots(figsize=FIG_SINGLE)
# labels/values 可传全量，barh_topn 自动取 Top-N（默认 15）
barh_topn(ax, labels, values, top=10, fmt='{:,.0f}', name='q1_特征重要性')
ax.set_xlabel('指标值')
save_fig(fig, 'q1_feature_importance')
```

### 长尾计数分布（用 CCDF，禁 log-x 直方图）

```python
fig, ax = plt.subplots(figsize=FIG_SINGLE)
vals = np.sort(counts)                       # 例如每用户行为次数
ccdf = 1.0 - np.arange(len(vals)) / len(vals)
ax.plot(vals, ccdf, color=COLORS['primary'], linewidth=1.8)
ax.set_xscale('log'); ax.set_yscale('log')   # 双对数看重尾，不用 hist
ax.set_xlabel('行为次数')
ax.set_ylabel('P(X ≥ x)')
for q in [0.5, 0.8, 0.95]:
    xq = np.quantile(counts, q)
    ax.axvline(xq, color=COLORS['neutral'], linestyle='--', linewidth=0.8)
    annotate_stats(ax, f'{int(q*100)}%分位={xq:.0f}', loc=(0.05, 0.2 + q*0.2))
save_fig(fig, 'ccdf_counts')
```

### 子图组合（(a)(b) 编号）

```python
fig, axes = plt.subplots(1, 2, figsize=FIG_DOUBLE)
for ax, label in zip(axes, ['(a)', '(b)']):
    ax.text(-0.12, 1.02, label, transform=ax.transAxes,
            fontsize=10, fontweight='bold', va='bottom')
# ... 分别绘制 axes[0], axes[1]
save_fig(fig, 'combined')
```
