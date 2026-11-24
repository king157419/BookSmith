---
name: booksmith-slides
description: 根据书稿某一章（或某几节）做一讲课件。默认用 PPT Master 的 SVG→原生 PPTX 管线：先列页面清单，再逐页手写 1280×720 的 SVG（文字、形状、公式都会变成 PowerPoint 里可编辑的原生对象），可以套老师现有课件的模板背景，也可以用自带的简洁风格；备选 XeLaTeX Beamer（与书稿同一套字体与公式）。内容只取自书稿，不编造。任务是"给第 N 章做课件/PPT/幻灯片"时使用。使用前先读 booksmith-book 技能。
---

# 做课件

前提：已读 `booksmith-book`（书的结构、术语、符号）。本技能的产物是**独立于正文的课件源文件**，不修改任何章节 `.tex`。

任务提示里会给出：章号、范围（整章或某几节）、页数、风格 `style`、课件目录 `slides/chNN/<名字>/`、模板说明文件路径（套模板时）。没给的按默认：整章约 12 页，风格 `pptmaster-template`。

## 一、先读内容，再列页面清单

1. 读该章 `chapter.tex` 找到各节文件，读要讲的那几节，以及本章小结、术语表（第 4 章在 `ch4/`）。
2. 写页面清单 `slides/chNN/<名字>/outline.md`，每页一行，格式：

```markdown
| 页 | 类型 | 标题 | 要点（每条一句） | 出处 |
|---|---|---|---|---|
| 01 | cover | 第 5 章 检索增强生成 | 副标题：为什么大模型需要外部知识 | 5 章导语 |
| 02 | toc | 内容 | 一、为什么需要 RAG；二、…… | — |
| 03 | content | 大模型的三个知识缺口 | 知识过时；私有知识；幻觉 | 5.1 节 |
```

- 类型只用：`cover` 封面、`toc` 目录、`section` 节标题、`content` 内容、`ending` 结束。
- 一页讲一件事：标题是一句结论或一个问题，要点 3～5 条；机制类内容优先画成框图/流程（方框 + 箭头），比堆文字好。
- 公式、数字、术语全部照书里写（符号与 `front/notation.tex` 一致）；书里没有的事实不要加。例题可以挑一道书里的，用"已知 → 算式 → 结果"三行讲。
- 页数按任务要求，误差不超过 1 页。

## 二、PPT Master 风格（`pptmaster-template` / `pptmaster-default`）

每页一个 SVG 文件：`slides/chNN/<名字>/svg/01_cover.svg`、`02_toc.svg`、`03_why.svg`……（两位序号 + 下划线 + 英文短名）。流水线会把它们交给 PPT Master 检查并导出成 `.pptx`，每个 `<text>` 变成可编辑文本框，每个形状变成原生形状，公式变成可编辑的 Office 公式。

### 2.1 页面骨架（照抄后改内容）

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" lang="zh-CN"
     data-pptx-page-role="content" font-family="Microsoft YaHei">
  <g id="title" data-pptx-bounds="72 30 920 70">
    <text x="72" y="78" font-size="32" font-weight="bold" fill="#0058C4">大模型的三个知识缺口</text>
  </g>
  <g id="points" font-size="26" fill="#1F2937" data-pptx-bounds="72 140 1100 260">
    <text x="72" y="180"><tspan fill="#0058C4">■ </tspan>知识过时：训练数据有截止时间</text>
    <text x="72" y="240"><tspan fill="#0058C4">■ </tspan>私有知识：企业文档从没进过训练集，
      <tspan x="104" dy="38">模型不可能知道</tspan></text>
  </g>
  <g id="flow" data-pptx-bounds="72 430 1100 200">
    <rect x="72" y="460" width="240" height="96" rx="10" fill="#EAF2FC" stroke="#0058C4" stroke-width="2"/>
    <text x="192" y="518" text-anchor="middle" font-size="24" fill="#1F2937">用户问题</text>
    <line x1="312" y1="508" x2="392" y2="508" stroke="#0058C4" stroke-width="3"/>
    <polygon points="392,498 412,508 392,518" fill="#0058C4"/>
    <!-- …… -->
  </g>
</svg>
```

### 2.2 硬规则（违反会导致导出失败）

1. 根元素必须有：`xmlns`、`viewBox="0 0 1280 720"`、`lang="zh-CN"`、`data-pptx-page-role`（cover/toc/section/content/ending 之一）、`font-family="Microsoft YaHei"`。不写 `width`/`height`。
2. **根元素下面只放 `<g>`**，每个 `<g>` 有唯一 `id` 和 `data-pptx-bounds="x y 宽 高"`（这一组内容占的矩形，根坐标）。一组 = 一个逻辑单元（标题、要点列表、一张图、一个公式）。
3. 一段文字一个 `<text>`；同一段换行用 `<tspan x="与首行相同" dy="行距">`（行距约 1.5 倍字号）；段内强调用不带坐标的 `<tspan fill=… font-weight="bold">`。不要用 `foreignObject`、`<style>`、`class`、CSS、`@font-face`、`<use>`、`<marker>`、`<filter>`、`<image>`（外部图片一律不用）。
4. 颜色一律大写 `#RRGGBB`；透明用 `fill-opacity`。字体只在根上写一次，别处不要再写 `font-family`。
5. 文本是 XML：`&` 写 `&amp;`，`<` 写 `&lt;`，`>` 写 `&gt;`；不要用 `&nbsp;`、`&mdash;` 这类 HTML 实体，直接写 Unicode 字符（—、→、×、≈）。
6. 箭头不要用 marker：画一条 `<line>`，箭头头部用小三角 `<polygon>`。
7. **公式**（PowerPoint 里可编辑）：
   - 行内：`<tspan data-pptx-inline-formula="LaTeX">预览文字</tspan>`，例如 `<tspan data-pptx-inline-formula="C\approx 6ND">C≈6ND</tspan>`；tspan 不能带 x/y/dy，前后不留空格。属性里的 LaTeX 反斜杠写一个。
   - 独立公式：
     ```xml
     <g id="eq-cos" data-pptx-replace-with="formula" data-pptx-x="240" data-pptx-y="380"
        data-pptx-width="800" data-pptx-height="120" data-pptx-bounds="240 380 800 120">
       <metadata type="application/json"><![CDATA[{"latex":"\\cos\\theta=\\frac{\\mathbf{q}\\cdot\\mathbf{d}}{\\lVert\\mathbf{q}\\rVert\\,\\lVert\\mathbf{d}\\rVert}","display":"block","font_size":36,"color":"#1F2937","align":"center"}]]></metadata>
       <text x="640" y="455" text-anchor="middle" font-size="36" fill="#1F2937">cosθ = q·d / (‖q‖‖d‖)</text>
     </g>
     ```
     **JSON 里每个反斜杠要写两个**（`\\frac`）；`data-pptx-x/y/width/height` 与 `data-pptx-bounds` 一致；里面的 `<text>` 是不支持公式的软件里显示的预览，用普通字符写出同一个式子。
   - 只用常见 LaTeX 命令（\frac \sqrt 上下标 \sum \log \exp \mathbf \text 希腊字母 \approx \times \cdot \lVert…），不要自定义宏，不要 `\SI`、`\cN…` 这些书里的宏（课件里直接写 `14 GB`）。
8. 文字不能出框：中文每字宽约等于字号，英文/数字约 0.55 倍字号。写每行前算一下：行宽 ≤ 所在区域宽度。放不下就拆行、精简或拆页，**不要把字号缩到 20px 以下**（页脚/图注可以 16～18px）。
9. 坐标都在画布里：0 ≤ x ≤ 1280，0 ≤ y ≤ 720；内容与画布边缘至少留 40px。

### 2.3 两种风格

- **`pptmaster-template`（套老师模板）**：先读任务里给的 `template.md`（模板说明）。背景、校徽、色带由流水线自动铺在最底层，**你不要画背景**，只在说明里写的内容区里排版，标题的位置、字号、颜色、项目符号照说明来。
- **`pptmaster-default`（PPT Master 自带的简洁风格）**：白底，主色 `#1E40AF`，强调色 `#F59E0B`，正文 `#1F2937`，次要 `#64748B`，浅底 `#EFF6FF`。可以在最底层自己画背景：`<rect id="bg" data-pptx-role="background" x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>`（只有背景这一个元素可以不放进 `<g>`）。封面可用一条主色色块做标题底；内容页标题 `x=72 y=80`、34px 粗体主色，标题下方画一条 2px 主色细线（`<g id="title-rule" data-pptx-bounds="72 100 1136 4">`里放 `<line>`）。

### 2.4 讲稿（建议写）

`slides/chNN/<名字>/notes.md`：每页一段老师讲稿（口语化、100～200 字，讲清这一页"为什么、怎么来的"）：

```markdown
# Slide 1
……
---
# Slide 2
……
```

`# Slide N` 的 N 与 SVG 序号一致，页与页之间用 `---` 分隔。流水线会把它放进 PPTX 的备注里。

## 三、Beamer 风格（`beamer`，备选）

写一个文件 `slides/chNN/<名字>/slides.tex`：

```latex
\documentclass[aspectratio=169,UTF8,fontset=none,11pt]{ctexbeamer}
\input{fonts}            % 书稿的开源字体配置（编译脚本会把仓库根目录加进搜索路径）
\usepackage{amsmath,amssymb,booktabs,tikz}
\usetheme{Madrid}\usecolortheme{beaver}   % 或其它 Beamer 自带主题
\title{第 5 章\quad 检索增强生成}\author{}\date{}
\begin{document}
\begin{frame}\titlepage\end{frame}
\begin{frame}{大模型的三个知识缺口}
  \begin{itemize}\item ……\end{itemize}
\end{frame}
\end{document}
```

- 不 `\usepackage{bookstyle}`（那是书的版式）；书里的自定义宏（`\SI`、`\term`、`\cN…`）不要用。
- 编译：`sh tools/build_linux.sh --file slides/chNN/<名字>/slides.tex`，返回 0 才算过；有错就修（最多 3 次）。
- 每页内容不要溢出（Overfull 大于 10pt 要改）；TikZ 框图可以用。

## 四、提交与最终说明

- `git add slides/chNN/<名字>/` 下你写的源文件（outline.md、svg/*.svg、notes.md 或 slides.tex），不要加 `.pptx`、`.pdf`、`_work/`。然后 `git commit`（中文提交信息）。
- 不要改任何章节正文、`main.tex`、`*.sty`、`fonts.tex`、`tools/`。
- 最后一条消息输出：

```markdown
## 任务
<章、范围、页数、风格>

## 页面清单
<outline.md 的表格>

## 内容取舍
<哪些节/例题/图放进来了，哪些没放、为什么>

## 待人确认
<拿不准的表述、需要老师补的内容（比如课程信息、讲授人）>
```

## 五、质量红线

1. 每一页的内容都能在书里找到出处；数字、公式与书一致。
2. 文字不出框、不重叠；一页不超过 6 条要点。
3. 公式用公式标记（可编辑），不要把复杂公式写成一串普通字符了事。
4. 不出现"课件由 AI 生成"、检索者口吻、"第几讲"以外的无关信息；讲授人、学校信息没给就留空或写在"待人确认"。
