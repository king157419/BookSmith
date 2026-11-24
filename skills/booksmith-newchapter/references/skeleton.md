# 新章节骨架（可直接复制后改）

以下均按已验收章节（第 3 章）的实际写法。把 `N` 换成章号，`xxx` 换成英文短名。

## chapters/chN/chapter.tex

```latex
% 第 N 章 <章名>
\input{chapters/chN/00-intro}
\input{chapters/chN/01-xxx}
\input{chapters/chN/02-yyy}
\input{chapters/chN/03-summary}
\input{chapters/chN/04-exercises}
\input{chapters/chN/05-references}
```

## 00-intro.tex

```latex
% ---------- 本章局部命令（需要时才写，命名带章号前缀） ----------

\chapter{章名}
\label{cN:ch}

第一段：从读者会问的一个问题或一个真实场景切入（不要从"随着……的发展"开始）。

第二段：说清本章回答什么，各节怎么接起来：第\,\ref{cN:sec:xxx}\,节讲……；第\,\ref{cN:sec:yyy}\,节讲……

第三段：与相邻章的分工：本章负责……；……由第 M 章详细讨论，本章只给出概述并指明出处。

\begin{guide}
\guideitem{学习目标}{学完本章，读者应能够：}
\begin{enumerate}[leftmargin=2em]
  \item 说明……（动词开头，可检验）；
  \item 计算……；
  \item 区分……与……；
\end{enumerate}
\end{guide}
```

## 节文件 01-xxx.tex

```latex
\section{节标题}
\label{cN:sec:xxx}

\begin{guide}
\noindent 这一节要回答的问题是什么？结论是什么？一段话，不列条目。
\end{guide}

\subsection{小节标题}
\label{cN:subsec:xxx}

先一句话讲机制，再分步展开。首次出现的术语：\term{中文名}（\en{English Full Name}，缩写）\cite{cN-key}。

\input{chapters/chN/figs/fig-xxx}

如图\,\ref{cN:fig:xxx}，……

\begin{pitfall}
A 和 B 常被混用。A 是……，B 是……；区别在于……
\end{pitfall}
```

图文件 `chapters/chN/figs/fig-xxx.tex` 的写法见 `booksmith-book/references/environments.md` 第 8 节。

## 小结 NN-summary.tex

```latex
\section{本章小结与展望}
\label{cN:sec:summary}

本章沿着“……”的主线（图\,\ref{cN:fig:map}），讨论了……回顾各节回答的问题：
\begin{itemize}
  \item \emph{xxx}回答“……”：结论一句话（第\,\ref{cN:sec:xxx}\,节）。
  \item \emph{yyy}回答“……”：结论一句话（第\,\ref{cN:sec:yyy}\,节）。
\end{itemize}

展望：一段话，写尚未解决的问题或下一章如何接上，不写空话。
```

## 习题 NN-exercises.tex

格式见 `booksmith-book/references/environments.md` 第 11 节。若大纲没要求写习题，这个文件写成只含 `\section*{习题}` 与一句"习题另行提供。"并在最终说明里提示人用习题技能补；**不要留空标题而无说明**。

## 术语与文献 NN-references.tex

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
RAG & Retrieval-Augmented Generation & 检索增强生成 \\
\end{longtable}
}

\begin{thebibliography}{99}
\bibitem{cN-key} 作者. 标题[类型]. 出处, 年份. \url{https://…}
\end{thebibliography}
```

如果新章节是整章，术语表和文献表的"分节命令与目录写法"照 `chapters/ch3/15-references.tex` 开头和结尾原样复制，不要改成别的写法（`bookbook.sty` 对 `\bibsection` 有特别处理）。

## main.tex 里加的那一行

```latex
\partthree
\chapterfile{5}
...
\chapterfile{8}
\input{chapters/chN/chapter}   % ← 新增，放在对应篇之内
```
