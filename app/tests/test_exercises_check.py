from app.pipelines.real.exercises import count_problems


def test_count_problems_ignores_comments_and_definitions():
    body = (
        "% 每题：\\problem{类型}{难度}{题干}{答案与解析}；答案只在教师版显示。\n"
        "\\newcommand{\\problem}[4]{#3}\n"
        "\\problem{选择}{中等}{题干 50\\% 的情况}{解析}\n"
        "\\problem{简答}{中等}{题干}{解析} % 行尾注释 \\problem{x}\n"
        "\\problem{计算}{中等}{题干}{解析}\n"
    )
    assert count_problems(body) == 3
