---
name: figure-reporting
description: 每张图绘制后必须 print 的数据特征模板，以及子任务完成后的结果汇总格式。有图表输出时必须加载。
---

# 数据特征输出规范

Agent 无法"看到"生成的图片，只能看到代码的文本输出。**每张图的绘图代码后，必须紧跟 `print()` 输出该图的关键数据特征**，否则后续写作手只能猜测图片内容，导致论文描述与图片不符。

---

## 各类图的输出模板

### 时间序列 / 趋势图

```python
print("【图X数据特征 - 时间序列】")
print(f"   时间范围: {df['date'].min()} 至 {df['date'].max()}")
print(f"   起点值: {y.iloc[0]:,.2f}，终点值: {y.iloc[-1]:,.2f}")
print(f"   整体趋势: {'上升' if y.iloc[-1] > y.iloc[0] else '下降'}"
      f"（变化幅度 {(y.iloc[-1]/y.iloc[0]-1)*100:.1f}%）")
print(f"   峰值: {y.max():,.2f}（时间点: {y.idxmax()}）")
print(f"   谷值: {y.min():,.2f}（时间点: {y.idxmin()}）")
print(f"   均值: {y.mean():,.2f}，标准差: {y.std():,.2f}")
```

### 模型拟合 / 预测评估图

```python
print("【图X数据特征 - 模型拟合】")
print(f"   模型: {model_name}")
print(f"   R²: {r2:.4f}")
print(f"   MAE: {mae:.4f}，RMSE: {rmse:.4f}，MAPE: {mape:.2f}%")
print(f"   拟合质量: {'优秀' if r2 > 0.9 else '良好' if r2 > 0.7 else '一般'}")
print(f"   样本数: 训练集 {n_train}，测试集 {n_test}")
```

### 相关性热力图

```python
print("【图X数据特征 - 相关性】")
# 找最强正负相关对
upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
max_corr_idx = upper.stack().idxmax()
min_corr_idx = upper.stack().idxmin()
print(f"   最强正相关: {max_corr_idx[0]} vs {max_corr_idx[1]}"
      f" (r={upper.stack().max():.3f})")
print(f"   最强负相关: {min_corr_idx[0]} vs {min_corr_idx[1]}"
      f" (r={upper.stack().min():.3f})")
print(f"   |r|>0.7 的变量对数量: {(upper.abs().stack() > 0.7).sum()}")
```

### 特征重要性图

```python
print("【图X数据特征 - 特征重要性】")
print(f"   Top 5 特征：")
for i, (feat, imp) in enumerate(importance_df.head(5).values):
    print(f"   {i+1}. {feat}: {imp:.4f} ({imp/importance_df['importance'].sum()*100:.1f}%)")
print(f"   Top 5 累计重要性: {importance_df.head(5)['importance'].sum():.3f}")
```

### 预测图（含置信区间）

```python
print("【图X数据特征 - 预测结果】")
print(f"   预测目标时间点: {pred_date}")
print(f"   点预测值: {prediction:,.2f}")
print(f"   95% 置信区间: [{ci_lower:,.2f}, {ci_upper:,.2f}]")
print(f"   置信区间宽度: {ci_upper - ci_lower:,.2f}")
```

### 混淆矩阵

```python
print("【图X数据特征 - 混淆矩阵】")
print(f"   类别数: {cm.shape[0]}")
print(f"   总样本数: {cm.sum()}")
print(f"   总体准确率: {accuracy:.1%}")
for i, cls in enumerate(class_names):
    tp = cm[i, i]
    print(f"   {cls}: 精确率={cm[i,i]/cm[:,i].sum():.1%}，召回率={cm[i,i]/cm[i,:].sum():.1%}")
```

### 分布图（直方图 / 箱线图 / 小提琴图）

```python
print("【图X数据特征 - 数据分布】")
print(f"   变量: {col_name}")
print(f"   样本量: {len(data)}")
print(f"   均值: {data.mean():.4f}，中位数: {data.median():.4f}")
print(f"   标准差: {data.std():.4f}，变异系数: {data.std()/data.mean():.3f}")
print(f"   偏度: {data.skew():.3f}，峰度: {data.kurtosis():.3f}")
print(f"   范围: [{data.min():.4f}, {data.max():.4f}]")
```

### 敏感性分析图（热力图 / 等高线）

```python
print("【图X数据特征 - 敏感性分析】")
print(f"   分析参数: {param1_name}（范围 {param1_range[0]}~{param1_range[-1]}）"
      f" × {param2_name}（范围 {param2_range[0]}~{param2_range[-1]}）")
print(f"   目标函数范围: {result_matrix.min():.4f} ~ {result_matrix.max():.4f}")
print(f"   最优组合: {param1_name}={best_p1:.4f}, {param2_name}={best_p2:.4f}"
      f" → 目标值={best_val:.4f}")
print(f"   对 {param1_name} 的敏感度（相对变化范围）:"
      f" {(result_matrix.max(axis=1) - result_matrix.min(axis=1)).mean():.4f}")
```

---

## 结果汇总（每个子任务完成后必须输出）

```python
print("=" * 60)
print("【本问题建模结果汇总】")
print(f"   模型类型: {model_name}")
print(f"   核心指标: R²={r2:.4f}, MAE={mae:.4f}, RMSE={rmse:.4f}")
print(f"   核心结论: {conclusion_text}")
print(f"   生成图片: {', '.join(image_files)}")
print(f"   关键数值: {key_numbers}")
print("=" * 60)
```

---

## 注意事项

- 图片文件名要有意义，反映内容（如 `pred_vs_actual_q1.png` 而非 `figure1.png`）
- 每张图都要 `plt.close()` 防止内存泄漏
- 保存路径使用相对路径，不要硬编码绝对路径
