---
name: eda
description: 数据驱动题的 EDA 流程规范，含缺失值/异常值处理、数据泄露防范、特征工程指引。物理机理题请勿加载此技能。
---

# EDA 规范（数据驱动题）

## 何时使用本技能

- **数据驱动题**：存在真实数据集，有多个样本/分布，需要统计分析。
- **物理/力学机理题**（参数为题目给定的确定常量，如 H=200mm, m=3kg）：**不要使用本技能**，不要画直方图/箱线图或提"异常值清洗""缺失值"——评委会认为在套数据分析模板。物理题的 EDA 聚焦于：打印关键参数表格 → 几何关系计算 → 量纲验证 → 物理一致性检查。

---

## 数据驱动题 EDA 必须覆盖的六步

```python
import pandas as pd
import numpy as np

df = pd.read_csv("data.csv")  # 或 pd.read_excel

# 步骤 1：数据结构
print(df.info())
print(df.head())
print(df.describe())

# 步骤 2：缺失值报告
missing = df.isnull().sum()
missing_rate = missing / len(df)
missing_report = pd.DataFrame({"缺失数": missing, "缺失率": missing_rate})
print(missing_report[missing_report["缺失数"] > 0])
# 填充策略：数值列用中位数，分类列用众数，时序数据用前向填充

# 步骤 3：异常值检测（IQR 法）
for col in df.select_dtypes(include=[np.number]).columns:
    Q1, Q3 = df[col].quantile(0.25), df[col].quantile(0.75)
    IQR = Q3 - Q1
    outliers = ((df[col] < Q1 - 1.5 * IQR) | (df[col] > Q3 + 1.5 * IQR)).sum()
    if outliers > 0:
        print(f"{col}: {outliers} 个异常值（{outliers/len(df):.1%}）")

# 步骤 4：分布可视化 → 加载 visualization 技能后绘制直方图/箱线图

# 步骤 5：相关性分析
corr = df.select_dtypes(include=[np.number]).corr()
# 找最强相关对
corr_pairs = (corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
                  .stack()
                  .sort_values(ascending=False))
print("最强正相关：", corr_pairs.head(3))
print("最强负相关：", corr_pairs.tail(3))

# 步骤 6：分组对比分析（如果有分类变量）
# df.groupby("category")["target"].describe()
```

---

## 数据泄露防范（关键！）

| 场景 | 错误写法 | 正确写法 |
|------|---------|---------|
| 时序滞后特征 | `df["lag"] = df["y"].shift(-1)` | `df["lag"] = df["y"].shift(1)` |
| 滚动均值 | `df["roll"] = df["y"].rolling(3).mean()` | `df["roll"] = df["y"].rolling(3).mean().shift(1)` |
| 标准化 | 用全量数据 fit | 只用训练集 fit，测试集 transform |
| 目标编码 | 用全量计算统计值 | 只用训练集计算统计值 |

---

## 特征工程指引

```python
# 滞后特征（避免泄露）
df["lag_1"] = df["y"].shift(1)
df["lag_7"] = df["y"].shift(7)

# 滚动特征（带 shift）
df["roll_7_mean"] = df["y"].rolling(7).mean().shift(1)
df["roll_7_std"]  = df["y"].rolling(7).std().shift(1)

# 分类变量编码
df = pd.get_dummies(df, columns=["category"], drop_first=True)  # One-Hot
# 或 LabelEncoder for tree-based models

# 右偏分布对数变换
df["log_y"] = np.log1p(df["y"])
```

---

## 参数记录要求

所有关键参数必须有来源说明（三选一），在代码注释或 print 中标注：
1. 数据统计（如均值、IQR 倍数阈值）
2. 文献引用（如 "参考 Box-Cox 变换标准"）
3. 网格搜索（如超参数调优结果）
