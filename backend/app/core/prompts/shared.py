"""共享的提示词工具函数。"""


def get_reflection_prompt(error_message, code) -> str:
    """生成代码错误反思提示词。

    Args:
        error_message: 错误信息。
        code: 出错的代码。

    Returns:
        反思提示词字符串。
    """
    return f"""The code execution encountered an error:
{error_message}

Please analyze the error, identify the cause, and provide a corrected version of the code. 
Consider:
1. Syntax errors
2. Missing imports
3. Incorrect variable names or types
4. File path issues
5. Any other potential issues
6. If a task repeatedly fails to complete, try breaking down the code, changing your approach, or simplifying the model. If you still can't do it, I'll "chop" you 🪓 and cut your power 😡.
7. Don't ask user any thing about how to do and next to do,just do it by yourself.

Previous code:
{code}

Please provide an explanation of what went wrong and Remenber call the function tools to retry 
"""


def get_completion_check_prompt(prompt, text_to_gpt) -> str:
    """生成任务完成检查提示词。

    Args:
        prompt: 原始任务描述。
        text_to_gpt: 最新执行结果。

    Returns:
        完成检查提示词字符串。
    """
    return f"""
Please review whether the current subtask is fully completed.

Original task:
{prompt}

Latest execution results:
{text_to_gpt}

Check the following:
1. Have all required data processing and modeling steps been completed?
2. Have all figures been saved (check that .png files were actually created)?
3. Has each figure been followed by a print() of its key data features?
4. Have all necessary result files been saved?
5. Is there a final summary print with model type, core metrics, and conclusions?

Decision rules:
- If everything above is done → respond with a brief summary of what was accomplished. Do NOT call any tool.
- If something is missing → call the appropriate tool to complete it. Do not ask the user, just do it.
- If a task repeatedly fails → switch approach, simplify, or skip. Never enter an infinite retry loop.
- Keep total conversation turns minimal.
"""


def get_data_prep_completion_prompt(prompt, text_to_gpt) -> str:
    """生成数据准备阶段的完成检查提示词（不要求出图）。

    Args:
        prompt: 原始任务描述。
        text_to_gpt: 最新执行结果。

    Returns:
        完成检查提示词字符串。
    """
    return f"""
Please review whether data preparation is fully completed.

Original task:
{prompt}

Latest execution results:
{text_to_gpt}

Check the following:
1. Has each source sheet been written as UTF-8 csv under cleaned/?
2. Do filenames follow {{stem}}__{{sheet}}.csv (csv sources use Sheet1)?
3. Were original attachments and result* templates left untouched?
4. Is there at least one csv in cleaned/?
5. Is there a brief print of what was cleaned (missing values, dtypes, row counts)?

Decision rules:
- Do NOT create paper figures in this phase.
- If everything above is done → respond with a brief cleaning summary. Do NOT call any tool.
- If cleaned/ is empty or naming is wrong → call execute_code to fix it.
- Keep total conversation turns minimal.
"""


def get_figure_missing_prompt(phase: str, current_count: int, min_count: int) -> str:
    """生成「本阶段图片不足，禁止退出」的强制补图提示词。

    Args:
        phase: 子任务阶段名，如 ques1 / eda。
        current_count: 当前阶段已生成的 png 数量。
        min_count: 本阶段要求的最低 png 数量。

    Returns:
        补图提示词字符串。
    """
    prefix = phase.split("_")[0] if "_" in phase else phase
    naming = f"{prefix}_*.png" if prefix.startswith("ques") else f"{phase}_*.png"
    return f"""
**BLOCKING ISSUE — figures missing for phase "{phase}"**

Current saved .png count in this phase: {current_count}
Required minimum: {min_count}

You MUST NOT finish this subtask without creating the required figures.

Mandatory actions (do all of them):
1. Call `execute_code` to plot using injected helpers: `save_fig`, `barh_topn`, `annotate_stats`, `COLORS`, `FIG_*`.
2. Before each figure, print a 【绘图规划】 line (conclusion + chart type + budget slot).
3. After each figure, print key data features (see figure-reporting skill).
4. Save files with meaningful names like `{naming}`.
5. Each modeling question (ques*) needs at least 2 figures: e.g. model evaluation (ROC/PR/scatter/residual) + one insight chart (feature importance / calibration / sensitivity).

Do NOT respond without calling execute_code. Do NOT ask the user.
"""
