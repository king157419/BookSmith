# 环境与写法速查（均摘自真实源码）

## 1. 章首（`00-intro.tex`）

顺序固定：本章局部命令 → `\chapter` + `\label` → 2～3 段问题驱动的导语 → 学习目标框。

```latex
% ---------- 本章局部命令 ----------
\newenvironment{cthreecalc}{ ... }{ ... }      % 局部宏，名字带章号前缀

\chapter{大语言模型的预训练、微调与推理}
\label{c3:ch}

设想一所高校想为一门专业课程做一个问答模型……要回答一连串问题：要不要从零训练一个模型？……

这些问题恰好对应……本章沿着这条生命周期展开：第\,\ref{c3:sec:overview}\,节给出全景……

\begin{guide}
\guideitem{学习目标}{学完本章，读者应能够：}
\begin{enumerate}[leftmargin=2em]
  \item 说明预训练、指令微调、偏好对齐与推理部署各自解决什么问题，区分训练与推理；
  \item 用 $C\approx 6ND$ 和每参数字节数估算训练所需的算力、时间和显存，说出各项数字的前提；
\end{enumerate}
\end{guide}
```

学习目标 5～7 条，动词开头、可检验（"说明""计算""比较""判断"），不写"了解""掌握"。

## 2. 节首导读框

每个 `\section` 紧跟一个 `guide`，**一段话**：这节回答什么问题、结论是什么。不列条目，不贴"需求/方案/效果"标签。

```latex
\section{对齐：让回答符合人的期望}
\label{c3:sec:align}

\begin{guide}
\noindent 指令微调后的模型已经会按指令做事，但它只会模仿示范回答……怎样让它的回答更有帮助、更诚实、更无害？本节先说明……结论是：对齐方法的共同点是“让模型生成多个回答，再根据对这些回答的评价来学习”；它们的差别在于评价从哪里来、需要几个模型同时参与训练。
\end{guide}
```

## 3. 术语、强调、英文名

```latex
\term{基于人类反馈的强化学习}（\en{Reinforcement Learning from Human Feedback}，RLHF）
\emph{有帮助}（\en{Helpful}），理解用户的真实意图
```

- 首次出现：`\term{中文名}（\en{英文全称}，缩写）`。同一个术语全书只在**定义处**用一次 `\term`。
- 之后出现用普通文字，不再加黑体。

## 4. 例题 `example` 与五列计算表

```latex
\Needspace{18\baselineskip}
\begin{example}[奖励模型损失与梯度]{c3:ex:rmloss}
一个奖励模型对三组回答打分……求每组的 $\sigma(\Delta)$、损失和梯度大小，以及平均损失。
\begin{cthreecalc}
1 & 分数差 $\Delta$ & $2.0-0.5$；$0.3-0.1$ & 1.5；0.2；$-1.5$ & $\Delta=r_w-r_l$，C 组把较差的回答排在了前面 \\
2 & $\sigma(\Delta)$ & $1/(1+e^{-1.5})$ 等 & 0.818；0.550；0.182 & sigmoid；$e^{-1.5}\approx0.223$ \\
\end{cthreecalc}
\cthreeflow{两个分数 → 求差 $\Delta$ → $\sigma(\Delta)$ → 取 $-\ln$ 得损失，取 $1-\sigma$ 得梯度大小。}
A 组已经分对且差距大，梯度只有 0.182；C 组排错，梯度是 A 组的 4.5 倍……
\end{example}
```

- 例题编号自动生成（"例 3-5"），标签放第二个参数。
- 表头五列：**步骤 / 算什么 / 算式 / 结果 / 这个数从哪来**，之后一行"数据流向"。`cthreecalc`、`cthreeflow` 是第 3 章局部宏；别的章若没有同类宏，直接用 `tabularx`，列宽 `{@{}>{\raggedright\arraybackslash}p{8mm} >{\raggedright\arraybackslash}p{17mm} Y >{\raggedright\arraybackslash}p{17mm} >{\raggedright\arraybackslash}p{36mm}@{}}`（先看本章 `00-intro.tex` 有没有已定义的同类宏，有就用）。
- 每个数都要说明从哪来；除法和科学计数法加括号：`(7\times10^{9}\times2)\div(1024^{3})`；换算用 `\div`；单位用 `\SI{14}{GB}`。
- 同一个量先算精确值，再四舍五入，**用未四舍五入的值继续算**，并在"从哪来"里注明。
- 与课件/论文结果对不上时，两种都写，说明差在哪、考试时怎么写。

## 5. 辨析框 `pitfall`

```latex
\begin{pitfall}
AdamW 的优化器状态究竟是模型大小的“两倍”还是“三倍”？两种说法都出现过，差别在于是否把 FP32 主权重算进优化器状态。全 FP32 训练时……是\emph{两倍}；混合精度训练时……是\emph{三倍}。两种口径下模型状态的总和都是 16 字节/参数。
\end{pitfall}
```

写法：以问题开头，指出两个相近的东西各自是什么、差在哪、什么时候用哪个。放在容易混的概念刚讲完之后。

## 6. 节末要点 `keypoints`

两种写法，二选一：一段话，或列表。

```latex
\begin{keypoints}
\begin{itemize}[leftmargin=1.5em]
  \item 神经元先加权求和再经过激活函数；隐藏层把输入换到新的表示空间。
  \item 反向传播用链式法则从损失出发逐层传回梯度。
\end{itemize}
\end{keypoints}
```

## 7. 表格

```latex
\begin{table}[htbp]
\centering
\caption{GPU 的发展与主要供应商}
\label{c3:tab:gpuhist}
\small
\begin{tabularx}{\linewidth}{@{}l Y@{}}
\toprule
\heiti 时间 / 厂商 & \heiti 事件或定位 \\
\midrule
……
\bottomrule
\end{tabularx}
\end{table}
```

- 表题在表上方（`caption` 已设置）；三线表；表头用 `\heiti`；窄列用 `l`/`c`，宽列用 `Y`（自动换行）。
- 正文必须引用每张表：`表\,\ref{c3:tab:gpuhist} 列出……`。
- 每行左右成对，不要出现"左列空白、右列一大串"的半张表。
- 长表用 `longtable`；**无标题的 longtable 会占表号**，在它后面加 `\addtocounter{table}{-1}`。
- 可能跨页的表别用 `[htbp]` 以外的强制位置；确要原地放用 `[H]`。

## 8. 图

每张图一个文件 `chapters/chN/figs/fig-xxx.tex`，正文用 `\input{chapters/chN/figs/fig-xxx}` 引入。文件内容是完整的 figure 环境（网页转换会单独编译它，所以里面只放 tikzpicture 或 `\includegraphics`，不要放正文文字）：

```latex
\begin{figure}[htbp]
\centering
\begin{tikzpicture}[fig, x=1mm, y=1mm]
  \node[blkw, minimum width=36mm, minimum height=9mm] (a1) at (-46,0) {采样一批提示词};
  \node[blk,  minimum width=36mm, minimum height=9mm] (a3) at (-46,-28) {监督微调（SFT）};
  \node[blka, minimum width=36mm, minimum height=9mm] (a4) at (-46,-42) {SFT 模型};
  \draw[flow] (a1) -- (a3);
  \draw[flow, accent] (a4.east) -- ++(5,0) |- (b1.west);
  \node[note, text=accent] at (-23,-44) {生成\\候选};
\end{tikzpicture}
\caption{RLHF 的三个步骤\cite{c3-ouyang2022instructgpt}。蓝色箭头表示上一步的产物被下一步使用}
\label{c3:fig:rlhf}
\end{figure}
```

TikZ 样式（来自 `bookstyle.sty`，不要自造）：`fig`（整图样式，必须加）、`blk`（灰底框）、`blkw`（白底框）、`blka`（蓝色强调框，只标"当前讨论的对象"）、`grp`（分组虚线框）、`flow`（箭头）、`note`（小字标注）。

规则：
- 图中文字用正文字体，**不用无衬线、不用微软雅黑**；字号只有两级（`\small` 9pt 层名/标题、`\footnotesize` 7.5pt 标注），**图中不用数学模式**（`$...$`）。
- 颜色：黑灰为主；蓝色（`accent`）只标当前对象；红色（`warn`）只标瓶颈、硬性上限和易错点。
- 同层级框宽度一致、对齐；元素不挤、不贴边框；箭头要有含义（旁边标一个词）。
- 图注必须是完整的一句话，说明图在讲什么、颜色/箭头含义。
- 数据曲线用 `pgfplots`。不要加 `calc` 宏包。
- 图里文字出框、重叠是最常见的错。你看不到渲染结果，所以编译后检查日志里与该图相关的 Overfull/Underfull 警告，并在说明里写明“图未经目视检查，请审核重点看”。

## 9. 公式

```latex
\begin{equation}
  \mathcal{L}_{\mathrm{RM}}(\phi)=-\mathbb{E}\Bigl[\log\sigma\bigl(r_\phi(x,y_w)-r_\phi(x,y_l)\bigr)\Bigr],
  \label{c3:eq:rm}
\end{equation}
$\sigma(z)=1/(1+e^{-z})$ 是 sigmoid 函数……
```

- 公式后的"其中"说明紧接着写，逐个符号解释。不要编号的公式用 `equation*`。
- 数学里的粗体向量用 `\boldsymbol{m}`（合订本已重定义为 `\symbfit`）。
- 下标里的文字用 `\mathrm{}`：`M_{\mathrm{weight}}`。
- 不要在正文先用 `\textsubscript`（会弄坏第 4 章的 pgfplots 图），写下标用数学模式。

## 10. 节、小节与小结

```latex
\section{标题}
\label{c3:sec:xxx}
\subsection{标题}               % 需要被引用时加 \label{c3:subsec:xxx}
\paragraph{段首小标题} 正文接在同一行……
```

`NN-summary.tex`：`\section{本章小结与展望}`，先一段话回顾主线（引用本章知识地图），再用 `itemize` 逐条写"某节回答了什么问题：结论（第\,\ref{…}\,节）"，最后一段展望；可附一张"本章主要公式/结论"汇总表。

## 11. 习题与部分习题提示（`NN-exercises.tex`）

```latex
\section*{习题}
\addcontentsline{toc}{section}{习题}
\markright{习题}

\begin{enumerate}[label=\thechapter.\arabic*,leftmargin=2.6em,itemsep=4pt]
  \item （概念）预训练、指令微调……分别解决什么问题？
  \item （计算）某模型 $L=40$，$d=5120$ ……用式\,\eqref{c3:eq:params} 估算参数量。
  \item （辨析）……
  \item （设计）参照第\,\ref{c3:sec:case}\,节，为……设计一个完整方案。
\end{enumerate}

\subsection*{部分习题提示}
\begin{small}
\begin{description}[leftmargin=2.6em, labelwidth=2.2em, font=\normalfont\heiti]
  \item[3.5] $12\times40\times5120^{2}=1.26\times10^{10}$；……合计约 $1.29\times10^{10}$，即约 13B。
\end{description}
\end{small}
```

类型标签：（概念）（计算）（辨析）（设计）（估算）。计算题的提示必须给出完整算式与结果。**提示里的题号要与题目顺序一致**（在中间插题会让后面的题号全部顺延，提示和正文里"习题 3.x"的引用要同步改）。

## 12. 本章术语表与参考文献（`NN-references.tex`）

```latex
\section*{本章术语}
\addcontentsline{toc}{section}{本章术语}
\markright{本章术语}

{\small
\begin{longtable}{@{}p{30mm} p{62mm} p{38mm}@{}}
\toprule
\heiti 缩写/术语 & \heiti 英文全称 & \heiti 中文 \\
\midrule
\endhead
\bottomrule
\endfoot
LLM & Large Language Model & 大语言模型 \\
MFU & Model FLOPs Utilization & 模型算力利用率 \\
\end{longtable}
}
```

之后接 `\begin{thebibliography}{99} … \bibitem{…} … \end{thebibliography}`（参照该章现有文件的开头结尾写法，不要改分节命令）。

## 13. 代码块

```latex
\begin{lstlisting}
{"goal": "…", "constraints": ["…"]}
\end{lstlisting}
```

代码块里不用 `fvextra`；中文放在代码块里时先看现有章节有没有同样用法。

## 14. 常见排版坑

- 盒子（`guide`/`pitfall`/`keypoints`）不要放在会被浮动体截断的位置；例题前可加 `\Needspace{18\baselineskip}` 避免被拆到两页。
- 引用 `\ref` 前后的空格：中文里写 `第\,\ref{x}\,节`，`图\,\ref{x}` 后面接中文不加空格。
- 英文与中文之间不手动加空格，除非已有章节那样写（"7B 模型""KV 缓存"）；全书惯例是数字/英文与汉字之间留一个普通空格。
- 百分号写 `\%`，下划线写 `\_`（代码块与 `\texttt` 外）。
- 引号用中文全角“ ”。
