---
name: sensitivity-analysis
description: 敏感性分析的完整流程，含单参数、双参数热力图、Sobol 全局敏感性分析及论文级可视化。做敏感性分析前加载。
---

# 敏感性分析规范

## 何时进行敏感性分析

- 建模完成后，检验模型对关键参数变化的鲁棒性
- 题目要求对模型进行稳定性或误差分析时
- 优化问题中，检验最优解对约束条件的敏感程度

---

## 单参数敏感性分析

```python
import numpy as np
import matplotlib.pyplot as plt

# 以基准值为中心，±50% 范围扫描
baseline_val = 1.0   # 参数基准值
param_range = np.linspace(baseline_val * 0.5, baseline_val * 1.5, 50)
results = []

for val in param_range:
    # 替换参数后重新计算目标函数
    output = model_function(val, *other_params)
    results.append(output)

results = np.array(results)
relative_change = (results - results[len(results)//2]) / abs(results[len(results)//2]) * 100

# 绘图
fig, ax = plt.subplots(figsize=FIG_SINGLE)
ax.plot(param_range / baseline_val, relative_change,
        color=COLORS['primary'], linewidth=1.8)
ax.axhline(0, color=COLORS['neutral'], linestyle='--', linewidth=1)
ax.axvline(1.0, color=COLORS['neutral'], linestyle='--', linewidth=1, alpha=0.5)
ax.fill_between(param_range / baseline_val, relative_change, 0,
                alpha=0.1, color=COLORS['primary'])
ax.set_xlabel('参数相对变化（基准值=1）')
ax.set_ylabel('目标值相对变化 (%)')
plt.tight_layout()
plt.savefig('sensitivity_single.png', dpi=300, bbox_inches='tight')
plt.close()

# 计算敏感性指标
sensitivity_index = np.std(results) / np.mean(np.abs(results))
print(f"【单参数敏感性】")
print(f"   参数名称: {param_name}")
print(f"   基准值: {baseline_val:.4f}")
print(f"   目标值范围: {results.min():.4f} ~ {results.max():.4f}")
print(f"   最大相对变化: {relative_change.max():.1f}%")
print(f"   敏感性指数 (CV): {sensitivity_index:.4f}")
print(f"   结论: {'高敏感' if sensitivity_index > 0.1 else '低敏感'}参数")
```

---

## 双参数敏感性热力图

```python
# 双参数网格扫描
param1_range = np.linspace(p1_min, p1_max, 30)
param2_range = np.linspace(p2_min, p2_max, 30)
result_matrix = np.zeros((len(param1_range), len(param2_range)))

for i, p1 in enumerate(param1_range):
    for j, p2 in enumerate(param2_range):
        result_matrix[i, j] = model_function(p1, p2, *other_params)

# 找最优组合
best_idx = np.unravel_index(result_matrix.argmax(), result_matrix.shape)
best_p1 = param1_range[best_idx[0]]
best_p2 = param2_range[best_idx[1]]

# 热力图绘制
import seaborn as sns
fig, ax = plt.subplots(figsize=FIG_SQUARE)
x_labels = [f'{v:.2f}' for v in param2_range[::5]]
y_labels = [f'{v:.2f}' for v in param1_range[::5]]
sns.heatmap(
    result_matrix, ax=ax,
    cmap='RdYlGn', annot=False,
    xticklabels=x_labels, yticklabels=y_labels,
    cbar_kws={'label': '目标函数值'}
)
ax.set_xlabel(param2_name)
ax.set_ylabel(param1_name)
# 标注最优点
ax.scatter(best_idx[1] + 0.5, best_idx[0] + 0.5,
           marker='*', s=200, color='white', zorder=5)
plt.tight_layout()
plt.savefig('sensitivity_2d.png', dpi=300, bbox_inches='tight')
plt.close()

print("【图X数据特征 - 敏感性分析热力图】")
print(f"   参数1: {param1_name}（{p1_min:.3f} ~ {p1_max:.3f}）")
print(f"   参数2: {param2_name}（{p2_min:.3f} ~ {p2_max:.3f}）")
print(f"   目标函数范围: {result_matrix.min():.4f} ~ {result_matrix.max():.4f}")
print(f"   最优组合: {param1_name}={best_p1:.4f}, {param2_name}={best_p2:.4f}"
      f" → 目标值={result_matrix[best_idx]:.4f}")
```

---

## 多参数龙卷风图（重要性排名）

```python
# 对每个参数计算 ±10% 变化对目标函数的影响
params = {'参数A': val_a, '参数B': val_b, '参数C': val_c}
baseline_output = model_function(**params)

tornado_data = {}
for name, base_val in params.items():
    params_high = params.copy(); params_high[name] = base_val * 1.1
    params_low  = params.copy(); params_low[name]  = base_val * 0.9
    high_out = model_function(**params_high)
    low_out  = model_function(**params_low)
    tornado_data[name] = (low_out - baseline_output, high_out - baseline_output)

# 按影响幅度排序
sorted_params = sorted(tornado_data.items(),
                        key=lambda x: abs(x[1][1] - x[1][0]))

fig, ax = plt.subplots(figsize=FIG_SINGLE)
y_pos = range(len(sorted_params))
for i, (name, (low_delta, high_delta)) in enumerate(sorted_params):
    ax.barh(i, high_delta, left=0, color=COLORS['primary'], alpha=0.7, height=0.5)
    ax.barh(i, low_delta,  left=0, color=COLORS['danger'],  alpha=0.7, height=0.5)
ax.set_yticks(list(y_pos))
ax.set_yticklabels([p[0] for p in sorted_params])
ax.axvline(0, color='black', linewidth=0.8)
ax.set_xlabel('目标函数变化量')
plt.tight_layout()
plt.savefig('tornado.png', dpi=300, bbox_inches='tight')
plt.close()
```

---

## 图片数量建议

敏感性分析章节：2-3 张图
- 建议图1：主要参数的单参数敏感性折线图
- 建议图2：最重要两个参数的双参数热力图
- 可选图3：多参数龙卷风排名图（参数数量 ≥ 4 时）
