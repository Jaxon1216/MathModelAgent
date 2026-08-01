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
