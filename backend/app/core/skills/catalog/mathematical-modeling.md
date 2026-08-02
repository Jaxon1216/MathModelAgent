---
name: mathematical-modeling
description: 常见建模任务的 scipy/sklearn 代码规范，含时序预测、优化、分类/聚类、工程约束检查。开始建模前加载。
---

# 数学建模规范

## 开始建模前（强制）

每个 ques 子任务**必须先有** visualization + figure-reporting 技能（系统通常已预注入）。
建模完成后**必须**产出至少 2 张论文级 `.png`（用 `save_fig` 保存），典型组合：

| 图 | 类型 | 示例 |
|----|------|------|
| 模型评估 | ROC/PR、预测 vs 实际散点、残差分布 | `{prefix}_roc.png`, `{prefix}_pred_scatter.png` |
| 模型洞察 | 特征重要性 Top-N（`barh_topn`）、校准曲线、SHAP | `{prefix}_importance.png` |

禁止只输出 CSV/数值 summary 而不画图。每张图后必须 `print()` 关键指标（见 figure-reporting）。

---

## Docker 环境可用库

```
pandas  numpy  scipy  statsmodels  scikit-learn  xgboost  shap  matplotlib  seaborn
```

**不可用**：MATLAB、R、网络爬取、GPU（无 CUDA）。

---

## 按任务类型选择建模路线

### 时序预测

```python
# 优先尝试路线：ARIMA → Prophet（如无）→ LSTM（数据量>500）→ 简单回归作 baseline
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.stattools import adfuller

# 1. 平稳性检验
adf_result = adfuller(series)
print(f"ADF p值: {adf_result[1]:.4f} → {'平稳' if adf_result[1] < 0.05 else '非平稳，需差分'}")

# 2. ARIMA 建模
model = ARIMA(train, order=(p, d, q))
result = model.fit()
print(result.summary())

# 3. 预测并计算指标
pred = result.forecast(steps=n_steps)
from sklearn.metrics import mean_absolute_error, mean_squared_error
mae = mean_absolute_error(test, pred)
rmse = mean_squared_error(test, pred, squared=False)
mape = (abs((test - pred) / test)).mean() * 100
```

### 回归 / 预测（非时序）

```python
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.linear_model import Ridge, Lasso
from sklearn.model_selection import cross_val_score, GridSearchCV
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import r2_score, mean_absolute_error, mean_squared_error

# 规范化流程
scaler = StandardScaler()
X_train_s = scaler.fit_transform(X_train)   # 只用训练集 fit
X_test_s  = scaler.transform(X_test)

# 多模型对比
models = {
    'Ridge':   Ridge(alpha=1.0),
    'RF':      RandomForestRegressor(n_estimators=100, random_state=42),
    'XGBoost': xgb.XGBRegressor(n_estimators=100, random_state=42),
}
for name, m in models.items():
    scores = cross_val_score(m, X_train_s, y_train, cv=5, scoring='r2')
    print(f"{name}: CV R² = {scores.mean():.4f} ± {scores.std():.4f}")
```

### 分类

```python
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
import xgboost as xgb

clf = xgb.XGBClassifier(n_estimators=200, max_depth=4, random_state=42,
                         use_label_encoder=False, eval_metric='logloss')
clf.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

y_pred = clf.predict(X_test)
print(classification_report(y_test, y_pred, target_names=class_names))
```

### 聚类

```python
from sklearn.cluster import KMeans, AgglomerativeClustering
from sklearn.metrics import silhouette_score

# 确定最优 K（肘部法则 + 轮廓系数）
inertias, silhouettes = [], []
K_range = range(2, 10)
for k in K_range:
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = km.fit_predict(X_scaled)
    inertias.append(km.inertia_)
    silhouettes.append(silhouette_score(X_scaled, labels))
best_k = K_range[silhouettes.index(max(silhouettes))]
print(f"最优 K = {best_k}（轮廓系数 = {max(silhouettes):.4f}）")
```

### 优化（scipy.optimize）

```python
from scipy.optimize import minimize, differential_evolution

# 必须设置物理上下界，防止数学极值违反物理约束
bounds = [(lb1, ub1), (lb2, ub2)]   # 每个变量都要有上下界，并注释来源
# 例：[(0.1, 0.5),   # 绳长 L (m)，受模型离地高度限制 H_max=0.5m
#      (0.3, 5.0)]   # 转速 n (r/s)，设备正常运行下限 0.3 r/s

result = differential_evolution(objective, bounds, seed=42,
                                 maxiter=1000, tol=1e-7)
print(f"优化结果: {result.x}")
print(f"目标函数值: {result.fun:.6f}")
print(f"是否收敛: {result.success}, 迭代次数: {result.nit}")

# 工程约束检查（必须！）
if any(x < lb or x > ub for x, (lb, ub) in zip(result.x, bounds)):
    print("警告：优化结果超出物理约束范围，需重新设定约束")
else:
    print("✓ 优化结果满足所有物理约束")
```

---

## 优化类问题工程约束（关键！）

**每个优化变量必须有上界和下界**，在 print 中标注约束来源。常见致命错误：桌面缩尺模型（高度仅几百 mm）的优化结果给出数米长的构件。

| 约束来源 | 示例 |
|---------|------|
| 几何限制 | 绳长 `L ≤ H_model（模型离地高度）` |
| 物理限制 | 转速 `n ≥ 0.3 r/s（设备正常运行下限）` |
| 题目要求 | 质量 `m ≤ 5kg（题目明确限制）` |

如果无约束解违反物理限制，**大方 print 对比**：

```python
print(f"无约束最优解: x={unconstrained_result:.4f}，但该解物理不可行（超出几何限制）")
print(f"引入约束 x ≤ {ub:.3f}（{constraint_reason}）后，约束最优解: x={constrained_result:.4f}")
```

评委看到这种工程思维分析会给高分。

---

## 参数记录规范

```python
# 所有超参数必须在注释或 print 中说明来源
MODEL_PARAMS = {
    "n_estimators": 100,    # 网格搜索最优（CV R²: 0.892）
    "max_depth": 4,         # 经验值，防止过拟合
    "learning_rate": 0.05,  # 参考 XGBoost 官方文档建议范围
}
print(f"模型参数: {MODEL_PARAMS}")
print("参数来源: 网格搜索 + 交叉验证（5折），最优 CV R² = 0.892")
```

---

## 随机种子与可复现性

```python
import numpy as np
np.random.seed(42)
# scikit-learn 的随机模型均传 random_state=42
# xgboost 传 seed=42
```
