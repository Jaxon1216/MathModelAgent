# 数据交互节点优化

> **2026-08-25**：方案 1（开局注入结构画像）已由「建模前清洗落盘 + `data_contract.json` 限长渲染」替代，见 [`data_contract.py`](../../backend/app/utils/data_contract.py)。下文方案 1 仅作历史。

## 现状

LLM 与数据的交互完全通过文本往返，信息密度低：

```
LLM 输入: "数据集文件['附件1.xlsx', '附件2.csv']"   ← 只有文件名
LLM 输出: tool_call execute_code("pd.read_csv(...).head()")
解释器执行 → stdout 文本返回
LLM 读 stdout → 猜测数据结构 → 继续写代码
```

三个瓶颈：
1. **开局盲写**：LLM 不知道列名、类型、缺失情况，第一轮必定试探 `df.head()` / `df.info()`
2. **图片看不到**：解释器返回 `[execute_result_png 图片已生成，内容为 base64，未展示]`，LLM 靠猜
3. **stdout 一刀切截断**：`_truncate_text` 固定 1000 字符，`df.describe()` 可能被切在关键行

## 优化方案

### 1. 开局注入数据画像（ROI 最高）

在 CoderAgent 首次对话前，用 pandas 预读所有 csv/xlsx，拼结构化摘要注入到第一条 user 消息。

**实现位置**：`coder_agent.py` 的 `run()` 方法，在 `append_chat_history(system_prompt)` 之后。

**摘要格式**：

```
数据集 '附件1.xlsx' (Sheet1):
  1201 行 × 8 列
  列: 地块编号(int64), 面积(float64), 类型(str, 4种), 适合作物(str)
  缺失: 面积 0%, 类型 0%
  sample (前3行):
    地块1 | 23.5 | 平旱地 | 小麦 | ...
    地块2 | 18.2 | 梯田 | 玉米 | ...
  分类列: 类型=[平旱地, 梯田, 山坡地, 水浇地]

数据集 '附件2.csv':
  45000 行 × 6 列
  列: 用户ID(str), 行为(int64, 值: 1/2/3/4), 博主ID(str), 时间(datetime)
  缺失: 0%
  时间范围: 2024-07-11 00:00:00 ~ 2024-07-20 23:59:59
  sample (前3行):
    U001 | 3 | B012 | 2024-07-11 08:30:00 | ...
```

**新增工具函数**：`app/utils/data_profiler.py`

```python
def profile_data_files(work_dir: str) -> str:
    """扫描 work_dir 下的 csv/xlsx，返回结构化数据摘要文本。"""
```

### 2. 图片结果补文字描述

解释器层扫描新生成的 png，检查 stdout 中是否有对应的 figure-reporting print 输出。没有则注入提示。

**实现位置**：`local_interpreter.py` 的 `execute_code()` 方法。

### 3. stdout 智能截断

对 pandas 常见输出格式做感知截断：`.info()` 保留全量，`.describe()` / `.head()` / `.tail()` 保留前 20 行。

**实现位置**：`base_interpreter.py` 的 `_truncate_text()` 方法。

## 优先级

1 → 2 → 3。1 改动最小、ROI 最高，约 50 行新代码 + 1 行调用。
